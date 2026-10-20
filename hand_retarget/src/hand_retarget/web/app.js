"use strict";
const $ = id => document.getElementById(id);
let inputImage = null, inputB64 = null, editedB64 = null, currentJob = null;
let pollTimer = null, stream = null, trajectory = null, drawing = false, previous = null;
const mask = document.createElement("canvas");
const statusNames = {queued:"排队中", running:"运行中", completed:"已完成", failed:"失败", interrupted:"已中断"};
const stages = ["mask", "edit", "uplift", "generate", "retarget"];

function error(message) { $("form-error").textContent = message; $("form-error").hidden = !message; }
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
function imageFromURL(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("无法读取图片"));
    image.src = url;
  });
}
async function fileImage(file) {
  if (!file || file.size > 16*1024*1024) throw new Error("请选择小于 16 MiB 的图片");
  const url = URL.createObjectURL(file);
  try { return await imageFromURL(url); } finally { URL.revokeObjectURL(url); }
}
function encodeImage(image) {
  const canvas = document.createElement("canvas");
  canvas.width = image.naturalWidth || image.width;
  canvas.height = image.naturalHeight || image.height;
  if (canvas.width * canvas.height > 16000000) throw new Error("图片不能超过 16 MP");
  canvas.getContext("2d").drawImage(image, 0, 0);
  return canvas.toDataURL("image/png").split(",")[1];
}
async function setInput(image) {
  inputB64 = encodeImage(image); inputImage = image;
  $("input-preview").width = mask.width = image.naturalWidth || image.width;
  $("input-preview").height = mask.height = image.naturalHeight || image.height;
  $("input-preview").hidden = false; $("upload-prompt").hidden = true;
  $("image-size").textContent = `${mask.width} × ${mask.height}`;
  $("mask-file").value = ""; $("edited-file").value = ""; editedB64 = null;
  drawInput(); error("");
}
function drawInput() {
  if (!inputImage) return;
  const canvas = $("input-preview"), ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(inputImage, 0, 0, canvas.width, canvas.height);
  if ($("manual-mask").checked) { ctx.globalAlpha = .55; ctx.drawImage(mask, 0, 0); ctx.globalAlpha = 1; }
}
function paint(event) {
  if (!drawing || !inputImage) return;
  const rect = $("input-preview").getBoundingClientRect();
  const p = [(event.clientX-rect.left)*mask.width/rect.width, (event.clientY-rect.top)*mask.height/rect.height];
  const ctx = mask.getContext("2d"); ctx.strokeStyle = "white"; ctx.fillStyle = "white";
  ctx.lineWidth = Number($("brush").value)*mask.width/rect.width; ctx.lineCap = "round";
  ctx.beginPath(); ctx.moveTo(...(previous || p)); ctx.lineTo(...p); ctx.stroke();
  ctx.beginPath(); ctx.arc(...p, ctx.lineWidth/2, 0, Math.PI*2); ctx.fill();
  previous = p; drawInput();
}
$("input-preview").addEventListener("pointerdown", event => {
  if (!$("manual-mask").checked) return;
  drawing = true; previous = null; $("input-preview").setPointerCapture(event.pointerId); paint(event);
});
$("input-preview").addEventListener("pointermove", paint);
for (const name of ["pointerup", "pointercancel"]) $("input-preview").addEventListener(name, () => {drawing=false;previous=null;});
$("manual-mask").onchange = () => { $("mask-tools").hidden = !$("manual-mask").checked; drawInput(); };
$("clear-mask").onclick = () => {mask.getContext("2d").clearRect(0,0,mask.width,mask.height);drawInput();};
$("choose-image").onclick = () => $("image-file").click();
$("dropzone").onclick = () => { if (!inputImage) $("image-file").click(); };
$("image-file").onchange = async event => {try {await setInput(await fileImage(event.target.files[0]));} catch(e){error(e.message);}};
$("dropzone").ondragover = event => {event.preventDefault();$("dropzone").classList.add("drag");};
$("dropzone").ondragleave = () => $("dropzone").classList.remove("drag");
$("dropzone").ondrop = async event => {
  event.preventDefault();$("dropzone").classList.remove("drag");
  try {await setInput(await fileImage(event.dataTransfer.files[0]));} catch(e){error(e.message);}
};
$("mask-file").onchange = async event => {
  try {
    if (!inputImage) throw new Error("请先上传机器人图片");
    const image = await fileImage(event.target.files[0]);
    if (image.naturalWidth !== mask.width || image.naturalHeight !== mask.height) throw new Error("mask 尺寸必须与原图相同");
    const ctx = mask.getContext("2d");ctx.clearRect(0,0,mask.width,mask.height);ctx.drawImage(image,0,0);
    const data = ctx.getImageData(0,0,mask.width,mask.height);
    for(let i=0;i<data.data.length;i+=4){const v=data.data[i]>=128;data.data[i]=data.data[i+1]=data.data[i+2]=255;data.data[i+3]=v?255:0;}
    ctx.putImageData(data,0,0);drawInput();error("");
  } catch(e){error(e.message);}
};
$("edited-file").onchange = async event => {
  editedB64 = null;
  if (!event.target.files.length) return;
  try {
    if (!inputImage) throw new Error("请先上传机器人图片");
    const image = await fileImage(event.target.files[0]);
    if(image.naturalWidth!==mask.width || image.naturalHeight!==mask.height) throw new Error("编辑图尺寸必须与原图相同");
    editedB64=encodeImage(image);error("");
  } catch(e){event.target.value="";error(e.message);}
};
function stopCamera() {if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;$("camera-area").hidden=true;}
$("open-camera").onclick = async () => {
  try {
    stopCamera();
    if (!navigator.mediaDevices) throw new Error("相机需要 localhost 或 HTTPS；请通过 SSH 隧道访问本页");
    stream = await navigator.mediaDevices.getUserMedia({video:{facingMode:"environment"},audio:false});
    $("camera").srcObject=stream;$("camera-area").hidden=false;
  } catch(e){error(e.message);}
};
$("close-camera").onclick=stopCamera;
$("capture").onclick=async()=>{
  const video=$("camera");
  if(!video.videoWidth)return;
  const canvas=document.createElement("canvas");canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  canvas.getContext("2d").drawImage(video,0,0);
  try{await setInput(await imageFromURL(canvas.toDataURL("image/png")));stopCamera();}catch(e){error(e.message);}
};
window.addEventListener("pagehide",stopCamera);
$("camera-transform").onchange=()=>{const has=$("camera-transform").files.length>0;$("pitch").disabled=has;$("pitch").required=!has;};
async function jsonFile(id){const file=$(id).files[0];return file?JSON.parse(await file.text()):null;}
function encodeMask(){
  const canvas=document.createElement("canvas");canvas.width=mask.width;canvas.height=mask.height;
  const ctx=canvas.getContext("2d");ctx.fillStyle="black";ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(mask,0,0);
  const data=ctx.getImageData(0,0,canvas.width,canvas.height);let edited=0;
  for(let i=0;i<data.data.length;i+=4){const v=data.data[i]>=128?255:0;edited+=v>0;data.data[i]=data.data[i+1]=data.data[i+2]=v;data.data[i+3]=255;}
  if(!edited || edited===canvas.width*canvas.height)throw new Error("请涂抹机械手区域，并保留未编辑区域");
  ctx.putImageData(data,0,0);return canvas.toDataURL("image/png").split(",")[1];
}
$("generate-form").onsubmit=async event=>{
  event.preventDefault();error("");$("generate").disabled=true;
  try {
    if(!inputB64)throw new Error("请先上传或拍摄机器人图片");
    const camera=await jsonFile("camera-transform");
    const request={image_b64:inputB64,instruction:$("instruction").value,hfov:Number($("hfov").value),
      seed:Number($("seed").value),fixed_camera_pitch:camera?null:Number($("pitch").value),
      camera_to_demo:camera,robot_from_demo:await jsonFile("placement"),
      mask_b64:$("manual-mask").checked?encodeMask():null,edited_image_b64:editedB64};
    const job=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json","X-Retarget-Client":"1"},body:JSON.stringify(request)});
    localStorage.setItem("retarget-camera",JSON.stringify({hfov:$("hfov").value,pitch:$("pitch").value}));
    await selectJob(job.id);await history();
  }catch(e){error(e.message);}finally{$("generate").disabled=false;}
};
async function health(){
  $("health").textContent="● 正在检查服务";
  try{
    const s=await api("/api/status");$("health").className=`pill ${s.ready?"ready":"offline"}`;
    $("health").textContent=s.ready?"● 服务已就绪":"● 服务尚未就绪";
    $("health-detail").textContent=s.errors.join("\n");$("health-detail").hidden=s.ready;
  }catch(e){$("health").textContent="● 无法连接后端";$("health-detail").textContent=e.message;$("health-detail").hidden=false;}
}
$("health").onclick=health;
function resetResult(){
  trajectory=null;$("result").hidden=false;$("empty-state").hidden=true;
  for(const id of ["edited-image","output-video","trajectory","execution-note","mask-result"])$(id).hidden=true;
  $("output-video").pause();$("output-video").removeAttribute("src");$("output-video").load();
  $("edit-pending").hidden=false;$("metrics").replaceChildren();$("downloads").replaceChildren();$("job-error").hidden=true;
  $("import-notice").hidden=true;$("steps").hidden=false;
}
async function selectJob(id){
  clearTimeout(pollTimer);currentJob=id;localStorage.setItem("retarget-last-job",id);resetResult();
  await poll(id);
}
async function poll(id){
  try{
    const job=await api(`/api/jobs/${id}`);if(currentJob!==id)return;
    renderJob(job);
    if(["queued","running"].includes(job.status))pollTimer=setTimeout(()=>poll(id),1800);
    else await history();
  }catch(e){if(currentJob===id){$("job-error").textContent=e.message;$("job-error").hidden=false;}}
}
function metric(label,value){const div=document.createElement("div");div.className="metric";const title=document.createElement("small");title.textContent=label;div.append(title,document.createTextNode(value));$("metrics").append(div);}
function renderJob(job){
  $("job-status").textContent=statusNames[job.status]||job.status;$("job-instruction").textContent=job.instruction;
  $("source-image").closest(".previews").hidden=!job.input_url;
  if(job.input_url)$("source-image").src=job.input_url;
  $("import-notice").hidden=job.mode!=="imported_result";
  $("steps").hidden=job.mode==="imported_result";
  const active=stages.indexOf(job.progress?.stage);
  document.querySelectorAll("#steps li").forEach((el,i)=>{el.className=job.status==="completed"||i<active?"done":i===active?"active":"";});
  $("job-error").textContent=job.error||"";$("job-error").hidden=!job.error;
  const files=job.artifacts||{};
  if(files["edited.png"]){$("edited-image").src=files["edited.png"];$("edited-image").hidden=false;$("edit-pending").hidden=true;}
  if(files["edit_mask.png"]){$("mask-image").src=files["edit_mask.png"];$("mask-result").hidden=false;}
  if(job.status!=="completed")return;
  if(files["video.mp4"]){$("output-video").src=files["video.mp4"];$("output-video").hidden=false;}
  $("metrics").replaceChildren();metric("轨迹时长","4.00 s");metric("关节采样",`${job.metrics?.exports?.samples??"—"} × 26`);
  if(job.metrics?.mean_fingertip_error_m!=null)metric("求解器指尖误差",`${(job.metrics.mean_fingertip_error_m*1000).toFixed(2)} mm`);
  metric("空间对齐",job.metrics?.placement_mode==="calibrated"?"已提供标定":"初始手位 · 实验");
  $("downloads").replaceChildren();
  const labels={"bundle.zip":"下载全部结果 ↓","robot_actions.npz":"机器人 NPZ","robot_actions.csv":"CSV","trajectory.json":"轨迹 JSON","controller_commands.json":"控制器消息","manifest.json":"运行记录"};
  for(const [name,label]of Object.entries(labels)){if(files[name]){const a=document.createElement("a");a.href=files[name];a.download=name;a.textContent=label;$("downloads").append(a);}}
  $("execution-note").hidden=false;
  if(files["trajectory.json"]){const id=job.id;api(files["trajectory.json"]).then(data=>{if(currentJob===id){trajectory=data;$("trajectory").hidden=false;drawChart();}}).catch(e=>error(e.message));}
}
function drawChart(){
  if(!trajectory)return;
  const canvas=$("chart"),ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height,pad=38;
  ctx.clearRect(0,0,w,h);const arm=$("joint-group").value==="arm",start=arm?0:6,end=arm?6:26;
  let lo=Infinity,hi=-Infinity;for(const p of trajectory.points)for(let j=start;j<end;j++){lo=Math.min(lo,p.positions[j]);hi=Math.max(hi,p.positions[j]);}
  if(hi-lo<.1){lo-=.05;hi+=.05;}
  ctx.font="12px system-ui";ctx.fillStyle="#807b72";ctx.strokeStyle="#eeeae3";
  for(let i=0;i<5;i++){const y=pad+(h-2*pad)*i/4;ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w-pad,y);ctx.stroke();ctx.fillText((hi-(hi-lo)*i/4).toFixed(1),2,y+4);ctx.fillText(`${i}s`,pad+(w-2*pad)*i/4-7,h-8);}
  for(let j=start;j<end;j++){ctx.strokeStyle=`hsl(${(j-start)*137.5%360},45%,45%)`;ctx.lineWidth=1.5;ctx.beginPath();
    trajectory.points.forEach((p,i)=>{const x=pad+p.time_from_start/trajectory.duration_seconds*(w-2*pad),y=pad+(hi-p.positions[j])/(hi-lo)*(h-2*pad);if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);});ctx.stroke();}
}
$("joint-group").onchange=drawChart;
async function history(){
  try{
    const jobs=await api("/api/jobs");$("history-list").replaceChildren();
    if(!jobs.length){const p=document.createElement("p");p.className="muted";p.textContent="还没有任务。";$("history-list").append(p);}
    for(const job of jobs){const b=document.createElement("button");b.className="history-item";b.type="button";
      const prompt=document.createElement("span");prompt.className="prompt";prompt.textContent=job.instruction;
      const date=document.createElement("small");date.textContent=new Date(job.created_at*1000).toLocaleString();
      const status=document.createElement("span");status.textContent=statusNames[job.status]||job.status;b.append(prompt,date,status);b.onclick=()=>selectJob(job.id);$("history-list").append(b);}
  }catch(e){error(e.message);}
}
$("refresh-history").onclick=history;
try{const saved=JSON.parse(localStorage.getItem("retarget-camera")||"{}");$("hfov").value=saved.hfov||"";$("pitch").value=saved.pitch||"";}catch{}
health();history();
const last=localStorage.getItem("retarget-last-job");if(last)selectJob(last);
