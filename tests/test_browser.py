"""Opt-in complete browser flow with real DeepSeek and independent Docker worker."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_AGENT_RUN_BROWSER_TESTS") != "1"
    or os.environ.get("AI_AGENT_RUN_LIVE_LLM_TESTS") != "1"
    or os.environ.get("AI_AGENT_LLM_ALLOW_EXTERNAL") != "true",
    reason="Browser + real Docker + model test requires explicit cost opt-in",
)


def test_upload_approve_publish_download_and_reload(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    import httpx
    source = tmp_path / "synthetic.csv"
    source.write_bytes(b"id,date,amount\n1,2026/10/31,-3\n2,2026/02/30,5\n3,,5\n")
    root = tmp_path / "runtime"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    environment = dict(os.environ, AI_AGENT_DEMO_TOKEN="browser-test-only",
                       AI_AGENT_LLM_PROVIDER="deepseek", AI_AGENT_LLM_ALLOW_EXTERNAL="true")
    images = Path(os.environ.get("AI_AGENT_DOCKER_IMAGES", ".runtime/docker/images.json")).resolve()
    process = subprocess.Popen([sys.executable, "-m", "backend.local_runtime", "start",
                                "--data-dir", str(root), "--images", str(images), "--port", str(port)],
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(url + "/ready", headers={"X-Demo-Token": "browser-test-only"}).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert process.poll() is None, "Supervised service exited"
            time.sleep(0.2)
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(channel=os.environ.get("AI_AGENT_BROWSER_CHANNEL"),
                                        downloads_path=str(tmp_path / "downloads"),
                                        args=["--disable-gpu", "--force-color-profile=srgb"])
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            page.locator("#token").fill("browser-test-only")
            page.locator("#goal").fill("Only normalize approved dates")
            page.locator("#file").set_input_files(source)
            page.get_by_role("button", name="Upload and register").click()
            playwright.expect(page.locator("#status")).to_contain_text("UPLOADED", timeout=10000)
            page.get_by_role("button", name="Start fixed detection").click()
            playwright.expect(page.locator("#status")).to_contain_text("AWAITING_DECISIONS", timeout=30000)
            page.get_by_role("combobox", name="date cleaning action").select_option("normalize_date")
            page.locator("#consent").check()
            page.get_by_role("button", name="Submit and freeze approval").click()
            playwright.expect(page.locator("#status")).to_contain_text("SUCCEEDED", timeout=300000)
            assert "llm_generate" in page.locator("#evidence").inner_text()
            with page.expect_download() as download:
                page.get_by_role("button", name="Download output").click()
            result = download.value
            assert Path(result.path()).read_bytes().replace(b"\r\n", b"\n") == (
                b"id,date,amount\n1,2026-10-31,-3\n2,2026/02/30,5\n3,,5\n")
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.reload()
            assert page.locator("#token").input_value() == ""
            page.locator("#token").fill("browser-test-only")
            page.get_by_role("button", name="Load task").click()
            playwright.expect(page.locator("#status")).to_contain_text("SUCCEEDED", timeout=10000)
            assert not errors
            browser.close()
    finally:
        if process.poll() is None:
            subprocess.run([sys.executable, "-m", "backend.local_runtime", "stop",
                            "--data-dir", str(root)], check=True, timeout=10)
            process.wait(timeout=100)
        assert process.returncode == 0
