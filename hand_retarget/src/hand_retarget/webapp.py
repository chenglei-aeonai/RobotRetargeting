"""Loopback-only inference UI and persistent job API; no Codex runtime dependency."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image, ImageOps

from .inference import DEFAULT_PROFILE
from .h3_actions import rigid_transforms
from .inference_clients import H3Client

STATIC = Path(__file__).with_name("web")
ARTIFACTS = {"edited.png", "edit_mask.png", "video.mp4", "robot_actions.npz",
             "robot_actions.csv", "trajectory.json", "controller_commands.json",
             "human_actions.npz", "manifest.json", "bundle.zip"}
MAX_BODY = 48 * 1024 * 1024


@dataclass(frozen=True)
class Config:
    runs: Path
    h3_url: str = "http://127.0.0.1:30121"
    uplift_python: str = ""
    uplift_script: str = ""
    image_model: str = "gemini-2.5-flash-image"
    segmentation_model: str = "gemini-2.5-flash"
    profile: str = DEFAULT_PROFILE


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    image_b64: str = Field(min_length=1, max_length=24 * 1024 * 1024)
    instruction: str = Field(min_length=1, max_length=4000)
    mask_b64: str | None = Field(default=None, max_length=24 * 1024 * 1024)
    edited_image_b64: str | None = Field(default=None, max_length=24 * 1024 * 1024)
    hfov: float = Field(ge=20, le=160)
    fixed_camera_pitch: float | None = Field(default=None, gt=-89, lt=89)
    camera_to_demo: list | None = None
    robot_from_demo: list | None = None
    seed: int = Field(default=0, ge=0, le=2**31 - 1)


def write_json(path: Path, value: dict):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
    temp.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def package_result(result: Path):
    with zipfile.ZipFile(result / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in sorted(ARTIFACTS - {"bundle.zip"}):
            path = result / name
            if path.is_file():
                bundle.write(path, name)


def import_result(config: Config, source: Path, label: str):
    """Expose an existing completed run, explicitly marked as an import."""
    report = read_json(source / "manifest.json")
    if report.get("status") != "completed" or not (source / "robot_actions.npz").is_file():
        raise ValueError("Only completed pipeline results can be imported")
    job = config.runs / uuid.uuid4().hex
    (job / "result").mkdir(parents=True)
    for name in ARTIFACTS - {"bundle.zip"}:
        if (source / name).is_file():
            shutil.copyfile(source / name, job / "result" / name)
    from .trajectory import export_trajectory
    report["robot"]["exports"] = export_trajectory(job / "result", report["robot"])
    write_json(job / "result/manifest.json", report)
    package_result(job / "result")
    write_json(job / "status.json", {
        "id": job.name, "status": "completed", "created_at": time.time(),
        "instruction": label, "mode": "imported_result", "hardware_executed": False,
        "source_result": str(source.resolve()),
    })
    return job.name


def save_image(encoded: str, path: Path, *, mask=False) -> tuple[int, int]:
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > 16_000_000 or min(source.size) < 32:
                raise ValueError("image must have at least 32 pixels per side and at most 16 megapixels")
            image = ImageOps.exif_transpose(source).convert("L" if mask else "RGB")
            if mask and not np.isin(np.asarray(image), [0, 255]).all():
                raise ValueError("mask must be binary: 0 or 255")
            if mask and len(np.unique(image)) != 2:
                raise ValueError("mask must contain both editable and protected regions")
            image.save(path, format="PNG")
            return image.size
    except Exception as exc:
        raise ValueError(f"invalid {'mask' if mask else 'image'}: {exc}") from exc


def build_command(config: Config, job: Path, request: JobInput) -> list[str]:
    args = [sys.executable, "-m", "hand_retarget.inference", "run",
            "--image", str(job / "input.png"), "--instruction", request.instruction.strip(),
            "--h3-url", config.h3_url, "--profile", config.profile,
            "--uplift-python", config.uplift_python, "--uplift-script", config.uplift_script,
            "--image-model", config.image_model, "--segmentation-model", config.segmentation_model,
            "--hfov", str(request.hfov), "--seed", str(request.seed),
            "--seconds", "4", "--out", str(job / "result")]
    for filename, flag in (("mask.png", "--mask"), ("edited_input.png", "--edited-image"),
                           ("camera.npy", "--camera-transforms"),
                           ("placement.npy", "--robot-from-demo")):
        if (job / filename).exists():
            args.extend([flag, str(job / filename)])
    if request.fixed_camera_pitch is not None:
        args.extend(["--fixed-camera-pitch", str(request.fixed_camera_pitch)])
    return args


class Jobs:
    def __init__(self, config: Config):
        self.config = config
        config.runs.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retarget")
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.process = None
        self.pending = 0
        for status in config.runs.glob("*/status.json"):
            value = read_json(status)
            if value.get("status") in ("queued", "running"):
                value.update(status="interrupted", error="Server restarted; job was not resubmitted.")
                write_json(status, value)

    def locate(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(404, "Unknown job")
        path = self.config.runs / job_id
        if not (path / "status.json").is_file():
            raise HTTPException(404, "Unknown job")
        return path

    def snapshot(self, job: Path):
        value = read_json(job / "status.json")
        value["progress"] = read_json(job / "result/progress.json")
        # Only publish final downloadable commands after the complete pipeline succeeds.
        names = {"edited.png", "edit_mask.png"}
        if value["status"] == "completed":
            names = ARTIFACTS
            report = read_json(job / "result/manifest.json")
            robot = report.get("robot", {})
            value["metrics"] = {key: robot.get(key) for key in (
                "mean_fingertip_error_m", "placement_mode", "exports", "duration_seconds")}
        value["artifacts"] = {name: f"/api/jobs/{job.name}/files/{name}"
                              for name in sorted(names) if (job / "result" / name).is_file()}
        value["input_url"] = f"/api/jobs/{job.name}/input" if (job / "input.png").exists() else None
        return value

    def execute(self, job: Path, command: list[str]):
        state = read_json(job / "status.json")
        try:
            state.update(status="running", started_at=time.time())
            write_json(job / "status.json", state)
            with (job / "pipeline.log").open("w") as log:
                with self.lock:
                    if self.stopping.is_set():
                        raise RuntimeError("Server stopped; job was not resubmitted.")
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                    self.process = process
                try:
                    code = process.wait(timeout=3600)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise TimeoutError("Pipeline exceeded one hour; remote H3 job may still exist.")
            if self.stopping.is_set():
                raise RuntimeError("Server stopped; remote H3 job may still exist.")
            report = read_json(job / "result/manifest.json")
            if code != 0 or report.get("status") != "completed":
                raise RuntimeError(report.get("error", f"Pipeline exited with status {code}"))
            package_result(job / "result")
            state.update(status="completed", finished_at=time.time())
        except Exception as exc:
            state.update(status="interrupted" if self.stopping.is_set() else "failed",
                         error=str(exc), finished_at=time.time())
        finally:
            write_json(job / "status.json", state)
            with self.lock:
                self.process = None
                self.pending -= 1

    def close(self):
        """Stop local process groups and drain queued jobs without launching them."""
        with self.lock:
            self.stopping.set()
            if self.process is not None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.executor.shutdown(wait=True)

    def submit(self, request: JobInput) -> dict:
        if not request.instruction.strip():
            raise ValueError("instruction cannot be blank")
        if (request.fixed_camera_pitch is None) == (request.camera_to_demo is None):
            raise ValueError("choose fixed-camera pitch or camera-to-demo transforms")
        if request.camera_to_demo is not None:
            a = np.asarray(request.camera_to_demo, dtype=float)
            rigid_transforms(a, 1 if a.ndim == 2 else len(a))
        if request.robot_from_demo is not None:
            rigid_transforms(np.asarray(request.robot_from_demo), 1)
        if not request.edited_image_b64 and not os.environ.get("GEMINI_API_KEY"):
            raise ValueError("Image editing is not configured: set GEMINI_API_KEY on the server")
        if not request.mask_b64 and not os.environ.get("GEMINI_API_KEY"):
            raise ValueError("Automatic masking is not configured; upload a mask")
        if not all(Path(p).is_file() for p in (self.config.uplift_python, self.config.uplift_script)):
            raise ValueError("WiLoR interpreter and script must be configured on the server")
        with self.lock:
            if self.stopping.is_set():
                raise HTTPException(503, "Server is stopping")
            if self.pending >= 8:
                raise HTTPException(429, "Queue full; wait for an existing job")
            self.pending += 1
        job = self.config.runs / uuid.uuid4().hex
        try:
            job.mkdir()
            size = save_image(request.image_b64, job / "input.png")
            for value, filename, is_mask in ((request.mask_b64, "mask.png", True),
                                           (request.edited_image_b64, "edited_input.png", False)):
                if value and save_image(value, job / filename, mask=is_mask) != size:
                    raise ValueError(f"{filename} dimensions must match the oriented input")
            for value, name in ((request.camera_to_demo, "camera.npy"),
                                (request.robot_from_demo, "placement.npy")):
                if value is not None:
                    np.save(job / name, np.asarray(value, dtype=float))
            state = {"id": job.name, "status": "queued", "created_at": time.time(),
                     "instruction": request.instruction.strip(), "mode": "image_to_robot",
                     "checkpoint": self.config.profile, "duration_seconds": 4,
                     "hardware_executed": False}
            write_json(job / "request.json", request.model_dump(
                exclude={"image_b64", "mask_b64", "edited_image_b64"}))
            write_json(job / "status.json", state)
            self.executor.submit(self.execute, job, build_command(self.config, job, request))
            return state
        except Exception:
            with self.lock:
                self.pending -= 1
            shutil.rmtree(job, ignore_errors=True)
            raise


def create_app(config: Config) -> FastAPI:
    jobs = Jobs(config)

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()

    app = FastAPI(title="Robot Demonstration Studio", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.jobs = jobs

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # Same-origin custom header prevents cross-site job submissions to localhost.
        if request.method == "POST":
            if request.headers.get("x-retarget-client") != "1":
                return JSONResponse({"detail": "Missing X-Retarget-Client: 1"}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    return JSONResponse({"detail": "Upload too large (48 MiB maximum)"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    def javascript():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    def css():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get("/i18n.js")
    def translations():
        return FileResponse(STATIC / "i18n.js", media_type="text/javascript")

    @app.get("/aeonlabs-wordmark.png")
    def wordmark():
        return FileResponse(STATIC / "aeonlabs-wordmark.png", media_type="image/png")

    @app.get("/api/status")
    def status():
        errors = []
        image_ready = bool(os.environ.get("GEMINI_API_KEY"))
        if not image_ready:
            errors.append("图片编辑／自动 mask 尚未配置 GEMINI_API_KEY")
        uplift_ready = True
        try:
            if not Path(config.uplift_script).is_file():
                raise ValueError("WiLoR script is missing")
            subprocess.run([config.uplift_python, "--version"], capture_output=True,
                           timeout=5, check=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            uplift_ready = False
            errors.append("WiLoR 环境不可执行或尚未配置")
        client = H3Client(config.h3_url, config.profile, client=httpx.Client(timeout=3))
        try:
            client.preflight()
            h3_ready = True
        except Exception:
            h3_ready = False
            errors.append("H3 未就绪：需要 mirrored-12983 及自定义首帧接口")
        finally:
            client.close()
        return {"ready": image_ready and uplift_ready and h3_ready,
                "image_ready": image_ready, "uplift_ready": uplift_ready, "h3_ready": h3_ready,
                "checkpoint": config.profile, "errors": errors, "codex_required": False}

    @app.post("/api/jobs", status_code=202)
    def submit(request: JobInput):
        try:
            return jobs.submit(request)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/jobs")
    def history():
        paths = sorted(config.runs.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [jobs.snapshot(path.parent) for path in paths[:50]]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return jobs.snapshot(jobs.locate(job_id))

    @app.get("/api/jobs/{job_id}/input")
    def get_input(job_id: str):
        path = jobs.locate(job_id) / "input.png"
        if not path.is_file():
            raise HTTPException(404, "No input image for imported run")
        return FileResponse(path)

    @app.get("/api/jobs/{job_id}/files/{filename}")
    def file(job_id: str, filename: str):
        job = jobs.locate(job_id)
        if filename not in jobs.snapshot(job)["artifacts"]:
            raise HTTPException(404, "Artifact not available")
        inline = filename.endswith((".png", ".mp4"))
        return FileResponse(job / "result" / filename,
                            filename=None if inline else filename)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    parser.add_argument("--runs", type=Path, default=Path("out/web-runs"))
    parser.add_argument("--h3-url", default=os.environ.get("H3_URL", "http://127.0.0.1:30121"))
    parser.add_argument("--uplift-python", default=os.environ.get("UPLIFT_PYTHON", ""))
    parser.add_argument("--uplift-script", default=os.environ.get("UPLIFT_SCRIPT", ""))
    parser.add_argument("--image-model", default=os.environ.get("IMAGE_MODEL", "gemini-2.5-flash-image"))
    parser.add_argument("--segmentation-model", default=os.environ.get("SEGMENTATION_MODEL", "gemini-2.5-flash"))
    parser.add_argument("--import-result", type=Path, help="Import a completed run, then exit")
    parser.add_argument("--import-label", default="已保存的 H3 输出 · Retarget 回归结果")
    args = parser.parse_args()
    import uvicorn
    config = Config(args.runs.resolve(), args.h3_url, args.uplift_python, args.uplift_script,
                    args.image_model, args.segmentation_model)
    if args.import_result:
        print(import_result(config, args.import_result, args.import_label))
        return
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
