"""Launcher tests do not start Docker or make model requests."""

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_launcher", ROOT / "start.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.mark.parametrize("version", [(3, 11), (3, 12), (3, 13), (3, 14)])
def test_python_version_accepts_minimum_and_newer(version):
    launcher.check_python_version(version)


def test_python_version_rejects_old_interpreter():
    with pytest.raises(launcher.LaunchError, match=r"Python 3\.10.*minimum version 3\.11.*upgrade"):
        launcher.check_python_version((3, 10))


@pytest.mark.parametrize("version", ["3.11", "3.12", "3.13"])
def test_virtualenv_version_is_checked_and_recorded(tmp_path, monkeypatch, capsys, version):
    executable = tmp_path / "python.exe"
    executable.touch()
    marker = tmp_path / "marker.json"
    monkeypatch.setattr(launcher, "VENV_PYTHON", executable)
    monkeypatch.setattr(launcher, "DEPENDENCY_MARKER", marker)
    monkeypatch.setattr(launcher, "dependency_fingerprint", lambda: "test-fingerprint")
    commands = []

    def fake_run(command, *args, **kwargs):
        commands.append(command)
        return version if "version_info" in str(command) else ""

    monkeypatch.setattr(launcher, "run", fake_run)
    launcher.prepare_dependencies({})
    assert json.loads(marker.read_text()) == {"fingerprint": "test-fingerprint", "python": version}
    assert ("compatibility with this version is unverified" in capsys.readouterr().out) == (version != "3.12")
    launcher.prepare_dependencies({})
    assert sum("install" in command for command in commands) == 2


def test_old_virtualenv_is_rejected_without_install(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    executable.touch()
    monkeypatch.setattr(launcher, "VENV_PYTHON", executable)
    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        return "3.10"

    monkeypatch.setattr(launcher, "run", fake_run)
    with pytest.raises(launcher.LaunchError, match=r"\.venv.*3\.10.*minimum 3\.11"):
        launcher.prepare_dependencies({})
    assert len(calls) == 1


def test_preserve_mode_never_passes_model_key(monkeypatch):
    env = {"DEEPSEEK_API_KEY": "private-test-key", "AI_AGENT_LLM_PROVIDER": "deepseek"}
    monkeypatch.setattr(launcher, "run", lambda *args, **kwargs: pytest.fail("unexpected model check"))
    launcher.choose_model("preserve", False, env)
    assert env["AI_AGENT_LLM_PROVIDER"] == "disabled"
    assert env["AI_AGENT_LLM_ALLOW_EXTERNAL"] == "false"
    assert "DEEPSEEK_API_KEY" not in env


def test_real_model_needs_explicit_permission(monkeypatch):
    env = {"DEEPSEEK_API_KEY": "private-test-key"}
    with pytest.raises(launcher.LaunchError, match="explicit consent"):
        launcher.choose_model("deepseek", False, env)
    assert env == {"DEEPSEEK_API_KEY": "private-test-key"}
    calls = []
    monkeypatch.setattr(launcher, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    launcher.choose_model("deepseek", True, env)
    assert env["AI_AGENT_LLM_PROVIDER"] == "deepseek"
    assert env["AI_AGENT_LLM_ALLOW_EXTERNAL"] == "true"
    assert "private-test-key" not in str(calls[0][0])


def test_vultr_launcher_requires_consent_and_model(monkeypatch):
    env = {"VULTR_SERVERLESS_INFERENCE_API_KEY": "private-vultr-key"}
    with pytest.raises(launcher.LaunchError, match="explicit consent"):
        launcher.choose_model("vultr", False, env)
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)
    with pytest.raises(launcher.LaunchError, match="model ID"):
        launcher.choose_model("vultr", True, env)
    calls = []
    monkeypatch.setattr(launcher, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    env["AI_AGENT_LLM_MODEL"] = "example-model"
    env["DEEPSEEK_API_KEY"] = "other-private-key"
    launcher.choose_model("vultr", True, env)
    assert env["AI_AGENT_LLM_PROVIDER"] == "vultr"
    assert env["AI_AGENT_LLM_ALLOW_EXTERNAL"] == "true"
    assert "DEEPSEEK_API_KEY" not in env
    assert "private-vultr-key" not in str(calls[0][0])


def test_docker_failure_is_reported_without_secret(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _: None)
    with pytest.raises(launcher.LaunchError, match="Docker") as exc:
        launcher.check_docker({"DEEPSEEK_API_KEY": "secret"})
    assert "secret" not in str(exc.value)
    assert exc.value.stage == "Docker"


def test_launcher_keeps_key_out_of_prepare_processes(monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    monkeypatch.setattr(launcher, "check_platform", lambda: "linux/amd64")
    monkeypatch.setattr(launcher, "port_available", lambda _: True)
    preparation = []
    monkeypatch.setattr(launcher, "check_docker", lambda env: preparation.append(dict(env)))
    monkeypatch.setattr(launcher, "prepare_dependencies", lambda env: preparation.append(dict(env)))
    monkeypatch.setattr(launcher, "prepare_images", lambda _, env: preparation.append(dict(env)))
    monkeypatch.setattr(launcher, "run", lambda *args, **kwargs: "")
    monkeypatch.setattr(launcher, "wait_ready", lambda *args: None)
    started = []

    class Process:
        returncode = 0

        def wait(self, timeout=None):
            return 0

    def popen(command, **kwargs):
        started.append((command, kwargs))
        return Process()

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    launcher.launch(8000, "deepseek", True, open_browser=False)
    assert all("DEEPSEEK_API_KEY" not in env for env in preparation)
    assert started[0][1]["env"]["DEEPSEEK_API_KEY"] == "private-test-key"
    assert "private-test-key" not in str(started[0][0])
    assert "private-test-key" not in capsys.readouterr().out


def test_image_cache_requires_matching_sources_manifest_and_local_images(tmp_path, monkeypatch):
    marker = tmp_path / "marker.json"
    manifest = tmp_path / "images.json"
    image = "sha256:" + "a" * 64
    images = {stage: image for stage in ("PROFILE_A", "CLEAN_C", "VERIFY_D")}
    marker.write_text(json.dumps({"fingerprint": "abc", "platform": "linux/amd64", "images": images}))
    manifest.write_text(json.dumps({"platform": "linux/amd64", "images": images}))
    monkeypatch.setattr(launcher, "IMAGE_MARKER", marker)
    monkeypatch.setattr(launcher, "IMAGES", manifest)
    monkeypatch.setattr(launcher, "run", lambda *args, **kwargs: "")
    assert launcher.image_is_current("abc", "linux/amd64", {})
    assert not launcher.image_is_current("changed", "linux/amd64", {})
    assert not launcher.image_is_current("abc", "linux/arm64", {})
    monkeypatch.setattr(launcher, "run", lambda *args, **kwargs: (_ for _ in ()).throw(
        launcher.LaunchError("Images", "not found")))
    assert not launcher.image_is_current("abc", "linux/amd64", {})


def test_readiness_rejects_old_heartbeats(tmp_path):
    directory = tmp_path / "services"
    directory.mkdir()
    for name in ("api", "runner", "worker"):
        (directory / f"{name}.json").write_text(json.dumps({"seen": 101}))
    assert launcher.fresh_services(tmp_path, 100)
    assert not launcher.fresh_services(tmp_path, 102)
    (directory / "worker.json").unlink()
    assert not launcher.fresh_services(tmp_path, 100)


def test_status_checks_http_and_heartbeats_without_token(tmp_path, monkeypatch, capsys):
    from backend.local_runtime import heartbeat

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("X-Demo-Token")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"scaffold","execution_enabled":false}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(launcher, "VENV_PYTHON", Path(sys.executable))
    monkeypatch.setenv("AI_AGENT_DEMO_TOKEN", "private-demo-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-model-key")
    original_run = launcher.run

    def checked_run(command, *args, **kwargs):
        assert "AI_AGENT_DEMO_TOKEN" not in kwargs["env"]
        assert "DEEPSEEK_API_KEY" not in kwargs["env"]
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(launcher, "run", checked_run)
    try:
        for name in ("api", "runner", "worker"):
            heartbeat(tmp_path, name)
        launcher.show_status(tmp_path, server.server_port)
        output = capsys.readouterr().out
        assert "API is reachable and all three recent heartbeats are valid" in output
        assert "does not prove" in output
        assert "private-demo-token" not in output
        assert received == [("/health", None)]
        (tmp_path / "services" / "runner.json").unlink()
        with pytest.raises(launcher.LaunchError, match="not all ready"):
            launcher.show_status(tmp_path, server.server_port)
        assert "runner] Latest heartbeat missing or expired" in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_reports_api_unavailable(tmp_path, monkeypatch, capsys):
    from backend.local_runtime import heartbeat

    for name in ("api", "runner", "worker"):
        heartbeat(tmp_path, name)
    monkeypatch.setattr(launcher, "VENV_PYTHON", Path(sys.executable))

    class FailingOpener:
        def open(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr(launcher, "build_opener", lambda *_: FailingOpener())
    with pytest.raises(launcher.LaunchError, match="not all ready"):
        launcher.show_status(tmp_path, 8000)
    assert "unavailable or invalid" in capsys.readouterr().out


def test_stop_waits_for_recovery_before_success(tmp_path, monkeypatch):
    services = tmp_path / "services"
    services.mkdir()
    paths = [services / f"{name}.json" for name in ("api", "runner", "worker")]
    for path in paths:
        path.write_text("{}")

    def complete_stop(_):
        assert (tmp_path / "stop.request").exists()
        for path in paths:
            path.unlink()
        (tmp_path / "stop.request").unlink()

    monkeypatch.setattr(launcher.time, "sleep", complete_stop)
    launcher.request_stop(tmp_path, timeout=1)
    assert not (tmp_path / "stop.request").exists()
    with pytest.raises(launcher.LaunchError, match="No service heartbeat"):
        launcher.request_stop(tmp_path, timeout=1)


def test_stop_timeout_does_not_delete_request_or_claim_success(tmp_path):
    services = tmp_path / "services"
    services.mkdir()
    (services / "worker.json").write_text("{}")
    with pytest.raises(launcher.LaunchError, match="unconfirmed"):
        launcher.request_stop(tmp_path, timeout=0)
    assert (tmp_path / "stop.request").exists()


def test_supervisor_names_failed_service_and_keeps_key_from_other_children(tmp_path, monkeypatch):
    import backend.local_runtime as runtime

    monkeypatch.setenv("AI_AGENT_DEMO_TOKEN", "test-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    monkeypatch.setenv("VULTR_SERVERLESS_INFERENCE_API_KEY", "private-vultr-key")
    monkeypatch.setenv("VULTR_API_KEY", "private-account-key")
    processes = []
    recovery = []

    class Process:
        def __init__(self, index):
            self.returncode = 7 if index == 1 else None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    def popen(command, env):
        processes.append((command, env, Process(len(processes))))
        return processes[-1][2]

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kwargs: recovery.append((command, kwargs)))
    with pytest.raises(RuntimeError, match="Service runner exited with code 7"):
        runtime.supervise(tmp_path, tmp_path / "images.json", 8000)
    assert "DEEPSEEK_API_KEY" not in processes[0][1]
    assert processes[1][1]["DEEPSEEK_API_KEY"] == "private-test-key"
    assert "DEEPSEEK_API_KEY" not in processes[2][1]
    assert "DEEPSEEK_API_KEY" not in recovery[0][1]["env"]
    assert "VULTR_SERVERLESS_INFERENCE_API_KEY" not in processes[0][1]
    assert processes[1][1]["VULTR_SERVERLESS_INFERENCE_API_KEY"] == "private-vultr-key"
    assert "VULTR_SERVERLESS_INFERENCE_API_KEY" not in processes[2][1]
    assert "VULTR_SERVERLESS_INFERENCE_API_KEY" not in recovery[0][1]["env"]
    assert all("VULTR_API_KEY" not in environment for _, environment, _ in processes)
    assert "VULTR_API_KEY" not in recovery[0][1]["env"]
    assert recovery[0][0][-1] == "--recover-only"


def test_bundle_includes_launcher_and_excludes_user_csv(tmp_path):
    from workers.sandbox_manager.build_images import bundle_sources

    bundle = tmp_path / "application.zip"
    bundle_sources(ROOT, bundle, {"tested_on_target": False})
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        archive.extractall(tmp_path / "unpacked")
    assert "start.py" in names
    assert "start_linux.py" in names
    assert ".dockerignore" in names
    assert "web/progress.js" in names
    assert "backend/llm/validation_explanation.py" in names
    assert "workers/sandbox_manager/watchdog.py" in names
    assert "LICENSE" in names
    assert "docs/security.md" in names
    assert "docs/submission.md" not in names
    assert "deploy/Dockerfile.job" in names
    assert "deploy/nginx.conf.example" in names
    assert "deploy/README.md" not in names
    assert "本地闭环手动验证手册.md" not in names
    assert ".env.deepseek" not in names
    assert ".env.vultr" not in names
    assert not any(name.endswith(".csv") for name in names)
    unpacked = tmp_path / "unpacked"
    packaged_spec = importlib.util.spec_from_file_location("packaged_launcher", unpacked / "start.py")
    packaged = importlib.util.module_from_spec(packaged_spec)
    packaged_spec.loader.exec_module(packaged)
    assert packaged.image_fingerprint() == launcher.image_fingerprint()
    assert packaged.dependency_fingerprint() == launcher.dependency_fingerprint()


def test_progress_summaries_from_recorded_evidence():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional")
    script = """
const assert = require('node:assert/strict');
const { summarizeProgress } = require('./web/progress.js');
const entries = (kinds) => kinds.map(kind => ({kind}));
const byId = (steps, id) => steps.find(step => step.id === id);
let steps = summarizeProgress({state:'AWAITING_DECISIONS'}, entries(['UPLOADED','PROFILING','AWAITING_DECISIONS']), {attempts:[],codes:[]});
assert.equal(byId(steps,'profile').status,'Complete');
assert.equal(byId(steps,'approval').status,'Awaiting your approval');
steps = summarizeProgress({state:'QUEUED'}, entries(['UPLOADED','QUEUED']), {attempts:[],codes:[]});
assert.equal(byId(steps,'profile').status,'Queued');
steps = summarizeProgress({state:'SUCCEEDED'}, entries(['SPEC_FROZEN','VALIDATING','VALIDATED','SUCCEEDED']), {attempts:[{stage:'VERIFY_D'}],codes:[],validation:{verdict:'passed',changed_cells:0,mismatch_count:0}});
assert.equal(byId(steps,'generate').status,'Skipped');
assert.equal(byId(steps,'execute').status,'Skipped');
assert.equal(byId(steps,'publish').status,'Complete');
steps = summarizeProgress({state:'FAILED'}, entries(['GENERATING','EXECUTING','VALIDATING','FAILED','ERROR:ValueError']), {attempts:[{stage:'CLEAN_C',state:'finished'},{stage:'VERIFY_D',state:'failed'}],codes:[{}],validation:{verdict:'rejected'}});
assert.equal(byId(steps,'execute').status,'Complete');
assert.equal(byId(steps,'validate').status,'Failed');
assert.match(byId(steps,'validate').detail,/ERROR:ValueError/);
steps = summarizeProgress({state:'FAILED'}, entries(['AWAITING_DECISIONS','SPEC_FROZEN','FAILED']), {attempts:[],codes:[]});
assert.equal(byId(steps,'approval').status,'Complete');
assert.equal(byId(steps,'generate').status,'A subsequent step failed');
steps = summarizeProgress({state:'FAILED'}, entries(['UPLOADED','PROFILING']), {attempts:[],codes:[]});
assert.equal(byId(steps,'diagnosis').status,'Unconfirmed');
assert.notEqual(byId(steps,'profile').status,'Failed');
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_upload_and_approval_use_english_validation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional")
    script = """
const assert = require('node:assert/strict');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {
    value: '', files: [], checked: false, textContent: '', hidden: false,
    handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; },
    focus() { global.focused = id; }
  });
  return elements.get(id);
};
global.document = {getElementById: element, hidden: false};
global.location = {href: 'http://localhost/'};
global.setInterval = () => {};
let requests = 0;
global.fetch = async () => { requests++; throw Error('blocked test request'); };
require('./web/app.js');
const submit = id => element(id).handlers.submit({preventDefault() {}});
submit('upload-form');
assert.equal(element('upload-error').textContent, 'Enter the demo access token.');
assert.equal(global.focused, 'token');
element('token').value = 'test';
submit('upload-form');
assert.equal(element('upload-error').textContent, 'Enter a business goal.');
assert.equal(global.focused, 'goal');
element('goal').value = 'test';
submit('upload-form');
assert.equal(element('upload-error').textContent, 'Choose a CSV file.');
assert.equal(global.focused, 'file');
element('file').files = [{name: '<example>.csv'}];
element('file').handlers.change();
assert.equal(element('file-name').textContent, '<example>.csv');
element('file').files = [];
element('file').handlers.change();
assert.equal(element('file-name').textContent, 'No file selected');
submit('review-form');
assert.equal(element('review-error').textContent, 'Confirm the decisions and model data-sharing notice.');
assert.equal(global.focused, 'consent');
assert.equal(requests, 0);
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_pending_buttons_and_review_refresh_do_not_interrupt_user():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional")
    script = """
const assert = require('node:assert/strict');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {
    value: '', files: [], checked: false, disabled: false, textContent: '',
    hidden: false, handlers: {},
    addEventListener(name, fn) { this.handlers[name] = fn; },
    replaceChildren() {}, append() {}, focus() {}
  });
  return elements.get(id);
};
global.document = {getElementById: element, hidden: false, createElement: () => element('generated')};
global.location = {href: 'http://localhost/?task=task-1'};
global.history = {replaceState() {}};
global.window = {summarizeProgress: () => []};
global.setInterval = () => {};
element('token').value = 'test';
element('start').textContent = 'Start fixed detection';
let releaseStart, releaseReview, starts = 0, reviews = 0, uploads = 0, approvals = 0;
const response = body => ({ok: true, json: async () => body});
const task = {task_id: 'task-1', state: 'AWAITING_DECISIONS'};
const review = {task_version: 1, spec_frozen: false, profile: {}, issues: []};
global.fetch = async (path, options) => {
  if (path.endsWith('/start')) {
    starts++;
    if (starts === 2) throw Error('network unavailable');
    return new Promise(resolve => { releaseStart = () => resolve(response({})); });
  }
  if (path === '/api/tasks') {
    uploads++;
    return new Promise(() => {});
  }
  if (path.endsWith('/decisions')) {
    approvals++;
    return new Promise(() => {});
  }
  if (path.endsWith('/review')) {
    reviews++;
    if (reviews > 1) return new Promise(resolve => { releaseReview = () => resolve(response(review)); });
    return response(review);
  }
  if (path.includes('/events')) return response({events: []});
  if (path.endsWith('/evidence')) return response({artifacts: []});
  return response(task);
};
const flush = () => new Promise(resolve => setImmediate(resolve));
require('./web/app.js');
(async () => {
  element('start').handlers.click();
  element('start').handlers.click();
  assert.equal(starts, 1);
  assert.equal(element('start').textContent, 'Starting...');
  assert.equal(element('start').disabled, true);
  releaseStart();
  await flush();
  assert.equal(element('start').disabled, false);
  assert.equal(element('start').textContent, 'Start fixed detection');
  assert.equal(element('review-section').hidden, false);
  element('consent').checked = true;
  element('refresh').handlers.click();
  await flush();
  assert.equal(reviews, 2);
  assert.equal(element('review-section').hidden, false);
  assert.equal(element('consent').checked, true);
  releaseReview();
  await flush();
  assert.equal(element('review-section').hidden, false);
  assert.equal(element('consent').checked, true);
  element('start').handlers.click();
  await flush();
  assert.equal(element('start').disabled, false);
  assert.equal(element('start').textContent, 'Start fixed detection');
  assert.equal(element('status').textContent, 'network unavailable');
  element('goal').value = 'test';
  element('file').files = [{name: 'example.csv'}];
  const event = {preventDefault() {}};
  element('upload-form').handlers.submit(event);
  element('upload-form').handlers.submit(event);
  assert.equal(uploads, 1);
  assert.equal(element('upload').textContent, 'Uploading...');
  element('review-form').handlers.submit(event);
  element('review-form').handlers.submit(event);
  assert.equal(approvals, 1);
  assert.equal(element('approve').textContent, 'Submitting approval...');
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
