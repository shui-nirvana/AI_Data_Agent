"""Offline browser coverage for polling and delayed button requests."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading

import pytest


WEB = Path(__file__).resolve().parents[1] / "web"


def test_review_stays_visible_during_poll_and_start_has_feedback():
    playwright = pytest.importorskip("playwright.sync_api")
    candidates = (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    )
    browser_path = next((str(path) for path in candidates if path.is_file()), None)
    browser_path = browser_path or shutil.which("chromium") or shutil.which("google-chrome")
    if not browser_path:
        pytest.skip("A local Chromium browser is optional")

    start_seen = threading.Event()
    release_start = threading.Event()
    review_seen = threading.Event()
    release_review = threading.Event()
    state = {"task": "UPLOADED", "starts": 0, "delay_review": False}

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/api/tasks/test-task/start":
                self.send_error(404)
                return
            state["starts"] += 1
            start_seen.set()
            release_start.wait(5)
            state["task"] = "AWAITING_DECISIONS"
            self.send_json({}, 202)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/tasks/test-task":
                return self.send_json({"task_id": "test-task", "state": state["task"]})
            if path == "/api/tasks/test-task/review":
                if state["delay_review"]:
                    review_seen.set()
                    release_review.wait(5)
                return self.send_json({
                    "task_version": 1,
                    "spec_frozen": False,
                    "profile": {},
                    "issues": [{
                        "column": "example",
                        "issue_id": "example",
                        "findings": [],
                        "actions": ["preserve"],
                    }],
                })
            if path == "/api/tasks/test-task/events":
                return self.send_json({"events": []})
            if path == "/api/tasks/test-task/evidence":
                return self.send_json({"artifacts": [], "attempts": [], "codes": []})
            return super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=browser_path)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 500})
                page.goto(f"http://127.0.0.1:{server.server_port}/?task=test-task")
                page.locator("#token").fill("test-token")
                page.locator("#restore").click()
                page.get_by_text("Task UPLOADED", exact=True).wait_for()
                page.locator("#start").click()
                assert start_seen.wait(3)
                assert page.locator("#start").inner_text() == "Starting..."
                assert page.locator("#start").is_disabled()
                page.locator("#start").evaluate("button => button.click()")
                assert state["starts"] == 1
                release_start.set()
                page.locator("#review-form").wait_for()
                page.locator("#consent").check()
                page.locator("#consent").scroll_into_view_if_needed()
                scroll_before = page.evaluate("window.scrollY")
                assert scroll_before > 0
                state["delay_review"] = True
                assert review_seen.wait(5)
                assert page.locator("#review-section").is_visible()
                assert page.locator("#consent").is_checked()
                assert abs(page.evaluate("window.scrollY") - scroll_before) < 3
                release_review.set()
                page.wait_for_timeout(300)
                assert page.locator("#consent").is_checked()
                assert abs(page.evaluate("window.scrollY") - scroll_before) < 3
            finally:
                release_start.set()
                release_review.set()
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
