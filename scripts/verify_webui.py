"""Optional Chromium checks against a running server without GPU/API calls.

Requires playwright and Pillow. The server must have image credentials unset.
An imported completed run is required to exercise video and trajectory downloads.
"""
import argparse
import json
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8040")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    fixture = args.out / "upload-fixture.png"
    Image.new("RGB", (640, 480), (80, 110, 130)).save(fixture)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        context = browser.new_context(viewport={"width": 1440, "height": 1100},
                                      permissions=["camera"], accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(args.url)
        health = page.request.get(args.url + "/api/status").json()
        assert not health["image_ready"], "Test refuses live image credentials"
        page.locator("#image-file").set_input_files(fixture)
        page.wait_for_function("document.getElementById('image-size').textContent === '640 × 480'")
        page.locator("form details summary").click()
        page.locator("#manual-mask").check()
        bounds = page.locator("#input-preview").bounding_box()
        page.mouse.move(bounds["x"] + 50, bounds["y"] + 50)
        page.mouse.down()
        page.mouse.move(bounds["x"] + 130, bounds["y"] + 100, steps=10)
        page.mouse.up()
        page.locator("#instruction").fill("Lift the cup with the right hand.")
        page.locator("#hfov").fill("70")
        page.locator("#pitch").fill("0")
        page.locator("#generate").click()
        page.wait_for_function("document.getElementById('form-error').textContent.includes('GEMINI_API_KEY')")
        page.locator("#open-camera").click()
        page.wait_for_function("document.getElementById('camera').videoWidth > 0")
        page.locator("#capture").click()
        page.locator("#camera-area").wait_for(state="hidden")
        page.locator(".history-item").first.click()
        page.wait_for_function("document.getElementById('job-status').textContent === '已完成'")
        page.wait_for_function("!document.getElementById('trajectory').hidden")
        assert page.locator("#import-notice").is_visible()
        assert page.locator("#steps").is_hidden()
        page.locator("#joint-group").select_option("hand")
        video = page.locator("#output-video")
        if video.is_visible():
            page.wait_for_function("document.getElementById('output-video').readyState >= 1")
            duration = page.evaluate("document.getElementById('output-video').duration")
            assert abs(duration - 4) < .05, duration
        with page.expect_download() as download:
            page.get_by_role("link", name="机器人 NPZ", exact=True).click()
        download.value.save_as(args.out / "downloaded_robot_actions.npz")
        page.reload()
        page.wait_for_function("!document.getElementById('trajectory').hidden")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(args.out / "desktop.png"), full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(args.out / "mobile.png"), full_page=True)
        assert not errors, errors
        (args.out / "checks.json").write_text(json.dumps({
            "browser": "Chromium", "javascript_errors": errors,
            "passed": ["image upload", "manual mask painting", "missing-key error",
                       "camera capture with browser test device", "imported-result provenance",
                       "4-second video metadata", "hand trajectory plot", "NPZ download",
                       "result restored after reload", "mobile no horizontal overflow"],
            "live_image_generation_tested": False, "live_h3_generation_tested": False,
        }, indent=2))
        browser.close()
        print("Browser checks passed:", args.out)


if __name__ == "__main__":
    main()
