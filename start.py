"""Windows local demo launcher. It never executes uploaded data or model code."""

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
IMAGES = RUNTIME / "docker" / "images.json"
DEPENDENCY_MARKER = VENV / ".ai-agent-dependencies.json"
IMAGE_MARKER = RUNTIME / "docker" / "launcher-image.json"
WINDOWS_DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
MIN_PYTHON = (3, 11)
LOCK_TESTED_PYTHON = (3, 12)


class LaunchError(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def step(name, message):
    print(f"[{name}] {message}", flush=True)


def run(command, stage, hint, *, env=None, timeout=None, quiet=False):
    try:
        result = subprocess.run(
            [str(part) for part in command], cwd=ROOT, env=env, timeout=timeout,
            stdout=subprocess.PIPE if quiet else None,
            stderr=subprocess.PIPE if quiet else None,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchError(stage, hint) from exc
    if result.returncode:
        raise LaunchError(stage, hint)
    return result.stdout.strip() if quiet else ""


def digest_files(paths):
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix()):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def dependency_fingerprint():
    return digest_files([ROOT / "deploy/control-windows.lock", ROOT / "pyproject.toml"])


def image_fingerprint():
    paths = [
        ROOT / ".dockerignore", ROOT / "deploy/Dockerfile.job",
        ROOT / "deploy/requirements-job.lock", ROOT / "backend/__init__.py",
        ROOT / "workers/sandbox_manager/build_images.py",
    ]
    for directory in (ROOT / "backend/domain", ROOT / "images"):
        paths.extend(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)
    return digest_files(paths)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def check_python_version(version, stage="Python"):
    if tuple(version[:2]) < MIN_PYTHON:
        current = ".".join(str(part) for part in version[:2])
        raise LaunchError(stage, f"Python {current} is below the minimum version 3.11; upgrade Python and rerun the launcher.")


def check_platform():
    if os.name != "nt":
        raise LaunchError("Environment", "This entry point supports Windows only; use start_linux.py on Linux.")
    check_python_version(sys.version_info)
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "linux/amd64"
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    raise LaunchError("Environment", "Unsupported CPU architecture; use AMD64 or ARM64.")


def check_docker(env):
    if not shutil.which("docker"):
        raise LaunchError("Docker", "Docker was not found. Install Docker Desktop and reopen the terminal.")
    system = run(
        ["docker", "info", "--format", "{{.OSType}}"], "Docker",
        "Cannot connect to the Docker Linux engine. Start Docker Desktop and enable Linux containers.",
        env=env, timeout=25, quiet=True,
    )
    if system != "linux":
        raise LaunchError("Docker", "Docker is not using a Linux engine; switch to Linux containers.")


def prepare_dependencies(env):
    if not VENV_PYTHON.exists():
        if VENV.exists() and any(VENV.iterdir()):
            raise LaunchError("Python environment", ".venv exists but has no Python executable; inspect it. The launcher will not delete it.")
        step("Python environment", "Creating the project .venv.")
        run([sys.executable, "-m", "venv", VENV], "Python environment", "Could not create the virtual environment.", env=env)
    version = run(
        [VENV_PYTHON, "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
        "Python environment", "Could not read the Python version in .venv.", env=env, quiet=True,
    )
    try:
        venv_version = tuple(int(part) for part in version.split("."))
        if len(venv_version) != 2 or any(part < 0 for part in venv_version):
            raise ValueError("invalid Python version")
    except ValueError as exc:
        raise LaunchError("Python environment", "Invalid Python version in .venv; inspect the existing environment.") from exc
    if venv_version < MIN_PYTHON:
        raise LaunchError("Python environment", f".venv uses Python {version}, below the minimum 3.11; update it. The launcher will not delete it.")
    if venv_version != LOCK_TESTED_PYTHON:
        step("Python environment", f".venv uses Python {version}; the dependency lock was validated on 3.12 and compatibility with this version is unverified.")
    fingerprint = dependency_fingerprint()
    marker = read_json(DEPENDENCY_MARKER)
    if marker != {"fingerprint": fingerprint, "python": version}:
        step("Dependencies", "Installing locked dependencies after initial setup or a dependency change; network access may be required.")
        run([VENV_PYTHON, "-m", "pip", "install", "--require-hashes", "-r",
             ROOT / "deploy/control-windows.lock"], "Dependencies", "Locked dependency installation failed; check Python compatibility, network access and disk space. The lock was validated on Python 3.12.", env=env)
        run([VENV_PYTHON, "-m", "pip", "install", "--no-deps", "-e", ROOT],
            "Dependencies", "Project installation failed; check Python build tools and network access.", env=env)
        run([VENV_PYTHON, "-m", "pip", "check"], "Dependencies", "Dependency check failed; inspect .venv.", env=env)
        write_json(DEPENDENCY_MARKER, {"fingerprint": fingerprint, "python": version})
    else:
        run([VENV_PYTHON, "-m", "pip", "check"], "Dependencies",
            "Installed dependencies are inconsistent; inspect .venv. The launcher will not delete it.", env=env, quiet=True)
    run([VENV_PYTHON, "-c",
         "import fastapi,uvicorn,pydantic,docker,httpx,langgraph.checkpoint.sqlite"],
        "Dependencies", "Required dependencies could not be imported; inspect .venv.", env=env, quiet=True)
    step("Dependencies", "Project virtual environment is ready.")


def image_is_current(fingerprint, target_platform, env):
    marker, manifest = read_json(IMAGE_MARKER), read_json(IMAGES)
    if not isinstance(marker, dict) or not isinstance(manifest, dict):
        return False
    images = manifest.get("images")
    if (marker.get("fingerprint") != fingerprint or marker.get("platform") != target_platform
            or marker.get("images") != images or manifest.get("platform") != target_platform
            or not isinstance(images, dict) or set(images) != {"PROFILE_A", "CLEAN_C", "VERIFY_D"}):
        return False
    if any(not isinstance(image, str) or not image.startswith("sha256:") for image in images.values()):
        return False
    for image in set(images.values()):
        try:
            run(["docker", "image", "inspect", image], "Images", "Image does not exist.",
                env=env, timeout=25, quiet=True)
        except LaunchError:
            return False
    return True


def prepare_images(target_platform, env, target="local"):
    fingerprint = image_fingerprint()
    if not image_is_current(fingerprint, target_platform, env):
        step("Images", "Building the isolated image because it is missing or sources changed; network access may be required.")
        run([VENV_PYTHON, "-m", "workers.sandbox_manager.build_images",
             "--target", target, "--platform", target_platform],
            "Images", "Image build failed; check Docker, network access, disk space and build output.", env=env)
        manifest = read_json(IMAGES)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("images"), dict):
            raise LaunchError("Images", "No valid image manifest was found after the build.")
        write_json(IMAGE_MARKER, {"fingerprint": fingerprint, "platform": target_platform,
                                  "images": manifest["images"]})
        if not image_is_current(fingerprint, target_platform, env):
            raise LaunchError("Images", "Image manifest or local image validation failed.")
    step("Images", f"Isolated image is ready ({target_platform}).")


def choose_model(mode, allow_external, env):
    if mode is None:
        print("\nSelect a validation mode:", flush=True)
        print("  1. DeepSeek: charges may apply.")
        print("  2. No model: only the all-preserve A/D path can be tested.")
        print("  3. Vultr Serverless Inference: charges may apply.")
        print("  The model receives anonymized rules and statistics; repair also sends the failed job's executed source.")
        try:
            choice = input("Select 1, 2 or 3 [2]: ").strip() or "2"
        except EOFError as exc:
            raise LaunchError("Model configuration", "Choose a mode interactively or specify --mode preserve.") from exc
        if choice not in ("1", "2", "3"):
            raise LaunchError("Model configuration", "Select 1, 2 or 3.")
        mode = {"1": "deepseek", "2": "preserve", "3": "vultr"}[choice]
        allow_external = choice != "2"
    if mode in ("deepseek", "vultr") and not allow_external:
        raise LaunchError("Model configuration", "External models require explicit consent to data transfer and potential charges; choose interactively or use --allow-external.")
    if mode == "preserve":
        env["AI_AGENT_LLM_PROVIDER"] = "disabled"
        env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "false"
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("VULTR_SERVERLESS_INFERENCE_API_KEY", None)
        step("Model configuration", "Model calls are disabled; approved modifications will not run automatically.")
        return
    if mode == "vultr":
        env.pop("DEEPSEEK_API_KEY", None)
        if not env.get("AI_AGENT_LLM_MODEL"):
            if not sys.stdin.isatty():
                raise LaunchError("Model configuration", "Specify a Vultr model ID using AI_AGENT_LLM_MODEL.")
            env["AI_AGENT_LLM_MODEL"] = input("Enter the Vultr model ID: ").strip()
        key_name = "VULTR_SERVERLESS_INFERENCE_API_KEY"
        if key_name not in env and not (ROOT / ".env.vultr").is_file():
            if not sys.stdin.isatty():
                raise LaunchError("Model configuration", "Missing key: configure a private .env.vultr or enter it interactively.")
            key = getpass.getpass("Enter the Vultr inference API key (hidden and not saved; leave empty to cancel): ")
            if not key:
                raise LaunchError("Model configuration", "No key provided; startup cancelled.")
            env[key_name] = key
        env["AI_AGENT_LLM_PROVIDER"] = "vultr"
        env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "true"
        run([VENV_PYTHON, "-c",
             "import os; from backend.llm.vultr import VultrInferenceClient; "
             "VultrInferenceClient.from_environment(os.environ['AI_AGENT_LLM_MODEL'])"],
            "Model configuration", "Invalid key or model configuration; check environment variables and .env.vultr. Keys are not printed.",
            env=env, quiet=True)
        step("Model configuration", "Key format checked; no model connection was made. Real tasks may incur charges.")
        return
    env.pop("VULTR_SERVERLESS_INFERENCE_API_KEY", None)
    if "DEEPSEEK_API_KEY" not in env and not (ROOT / ".env.deepseek").is_file():
        if not sys.stdin.isatty():
            raise LaunchError("Model configuration", "Missing key: configure a private .env.deepseek or enter it interactively.")
        key = getpass.getpass("Enter the DeepSeek key (hidden and not saved; leave empty to cancel): ")
        if not key:
            raise LaunchError("Model configuration", "No key provided; startup cancelled.")
        env["DEEPSEEK_API_KEY"] = key
    env["AI_AGENT_LLM_PROVIDER"] = "deepseek"
    env["AI_AGENT_LLM_MODEL"] = env.get("AI_AGENT_LLM_MODEL") or "deepseek-flash"
    env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "true"
    run([VENV_PYTHON, "-c",
         "import os; from backend.llm.deepseek import DeepSeekClient; "
         "DeepSeekClient.from_environment(os.environ['AI_AGENT_LLM_MODEL'])"],
        "Model configuration", "Invalid key or model configuration; check environment variables and .env.deepseek. Keys are not printed.",
        env=env, quiet=True)
    step("Model configuration", "Key format checked; no model connection was made. Real tasks may incur charges.")


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def fresh_services(root, started):
    for name in ("api", "runner", "worker"):
        data = read_json(root / "services" / f"{name}.json")
        if not isinstance(data, dict) or not isinstance(data.get("seen"), (int, float)):
            return False
        if data["seen"] < started:
            return False
    return True


def wait_ready(process, root, port, token, started, timeout=60):
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchError("Service startup", "A service exited early; check the process output above.")
        try:
            request = Request(f"http://127.0.0.1:{port}/ready", headers={"X-Demo-Token": token})
            with opener.open(request, timeout=2) as response:
                data = json.load(response)
            if isinstance(data, dict) and data.get("ready") is True and fresh_services(root, started):
                return
        except (HTTPError, URLError, OSError, ValueError, TimeoutError):
            pass
        time.sleep(0.25)
    raise LaunchError("Service startup", "Timed out waiting for API, Runner and worker; check the process output above.")


def show_status(root, port):
    if not VENV_PYTHON.exists():
        raise LaunchError("Status", "The project virtual environment does not exist; start the launcher first.")
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("VULTR_SERVERLESS_INFERENCE_API_KEY", None)
    env.pop("VULTR_API_KEY", None)
    env.pop("AI_AGENT_DEMO_TOKEN", None)
    raw = run([VENV_PYTHON, "-m", "backend.local_runtime", "status", "--data-dir", root],
              "Status", "Cannot query service heartbeats; check the virtual environment and data directory.",
              env=env, timeout=15, quiet=True)
    try:
        state = json.loads(raw)
        services = state["services"]
        if (not isinstance(services, dict) or type(state["ready"]) is not bool
                or any(not isinstance(services.get(name), dict)
                       or type(services[name].get("ready")) is not bool
                       for name in ("api", "runner", "worker"))):
            raise ValueError("invalid status")
    except (ValueError, TypeError, KeyError):
        raise LaunchError("Status", "Invalid heartbeat result; check the startup terminal.") from None

    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            data = response.read(4097)
        health = json.loads(data) if len(data) <= 4096 else None
        api_reachable = (isinstance(health, dict) and health.get("status") == "scaffold"
                         and health.get("execution_enabled") is False)
    except (HTTPError, URLError, OSError, ValueError, TimeoutError):
        api_reachable = False

    step("API HTTP", f"127.0.0.1:{port}/health {'reachable' if api_reachable else 'unavailable or invalid'} (unauthenticated).")
    for name in ("api", "runner", "worker"):
        item = services[name]
        age = item.get("age_seconds")
        age_text = f", {age} seconds ago" if isinstance(age, (int, float)) else ""
        step(name, f"Latest heartbeat {'valid' if item['ready'] else 'missing or expired'}{age_text}.")
    print("Status uses HTTP and recent heartbeats; it does not prove that processes, Docker or the model are currently available. Startup separately checks authenticated /ready.",
          flush=True)
    if not api_reachable or not state["ready"] or not all(
        services[name]["ready"] for name in ("api", "runner", "worker")
    ):
        raise LaunchError("Status", "Services are not all ready; check the startup terminal and specify --port if using a custom port.")
    step("Status", "API is reachable and all three recent heartbeats are valid.")


def request_stop(root, timeout=110):
    services = root / "services"
    if not root.is_dir() or not any((services / f"{name}.json").exists()
                                    for name in ("api", "runner", "worker")):
        raise LaunchError("Stop", "No service heartbeat found; no stop requested. Check whether the supervisor is still running.")
    marker = root / "stop.request"
    marker.touch(exist_ok=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not marker.exists() and not any((services / f"{name}.json").exists()
                                           for name in ("api", "runner", "worker")):
            return
        time.sleep(0.25)
    raise LaunchError("Stop", "Service shutdown and container recovery are unconfirmed; check the startup terminal and Docker. No forced termination was attempted.")


def launch(port, mode, allow_external, open_browser=True):
    step("Environment", "Checking Windows, Python and Docker.")
    target_platform = check_platform()
    env = dict(os.environ)
    secret_names = ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY", "VULTR_API_KEY")
    inherited_keys = {name: env.pop(name) for name in secret_names if name in env}
    env.setdefault("DOCKER_HOST", WINDOWS_DOCKER_HOST)
    env["AI_AGENT_EXECUTION_BACKEND"] = "disabled"
    check_docker(env)
    if not port_available(port):
        raise LaunchError("Port", f"127.0.0.1:{port} is already in use; stop the other process or use --port.")
    prepare_dependencies(env)
    prepare_images(target_platform, env)
    for name in ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY"):
        if name in inherited_keys:
            env[name] = inherited_keys[name]
    choose_model(mode, allow_external, env)
    token = secrets.token_urlsafe(24)
    env["AI_AGENT_DEMO_TOKEN"] = token
    command = [str(VENV_PYTHON), "-m", "backend.local_runtime", "start",
               "--data-dir", str(RUNTIME), "--images", str(IMAGES), "--port", str(port)]
    step("Service startup", "Starting API, Runner and Docker worker; keep this window open.")
    started = time.time()
    try:
        process = subprocess.Popen(command, cwd=ROOT, env=env)
    except OSError as exc:
        raise LaunchError("Service startup", "Could not start the local supervisor.") from exc
    try:
        wait_ready(process, RUNTIME, port, token, started)
        url = f"http://127.0.0.1:{port}/"
        step("Ready", "All three process heartbeats are ready. Model connectivity and business tasks have not been verified.")
        print(f"Page: {url}")
        print(f"Demo token (enter on the page; keep private): {token}", flush=True)
        print("Upload, start detection, approve and inspect the evidence manually; press Ctrl+C to stop.", flush=True)
        if open_browser:
            try:
                if not webbrowser.open(url):
                    print("Could not open a browser; visit the address above manually.", flush=True)
            except Exception:
                print("Could not open a browser; visit the address above manually.", flush=True)
        result = process.wait()
        if result:
            raise LaunchError("Runtime", "Supervisor exited with an error; check the output and Docker recovery status.")
    except (KeyboardInterrupt, LaunchError) as original:
        if process.poll() is None:
            (RUNTIME / "stop.request").touch(exist_ok=True)
            try:
                process.wait(timeout=110)
            except subprocess.TimeoutExpired as exc:
                raise LaunchError("Stop", "Supervisor did not exit in time; check Docker. No forced termination was attempted.") from exc
        if process.returncode:
            if isinstance(original, LaunchError):
                raise LaunchError(original.stage, f"{original} Supervisor returned a nonzero status; check recovery.") from original
            raise LaunchError("Stop", "Supervisor or container recovery failed; check the output and Docker.")
        raise


def main():
    parser = argparse.ArgumentParser(description="AI Data Agent Windows local demo launcher")
    parser.add_argument("command", nargs="?", choices=("start", "stop", "status"), default="start")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", choices=("deepseek", "vultr", "preserve"))
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        if args.command == "start":
            launch(args.port, args.mode, args.allow_external, not args.no_browser)
        elif args.command == "stop":
            request_stop(RUNTIME)
            step("Stop", "The supervisor exited and completed recovery.")
        else:
            show_status(RUNTIME, args.port)
    except KeyboardInterrupt:
        step("Interrupted", "Operation interrupted; if services started, check supervisor and Docker recovery.")
    except LaunchError as exc:
        print(f"[Failed: {exc.stage}] {exc}", file=sys.stderr, flush=True)
        return 1
    except OSError:
        print("[Failed: filesystem] Cannot read or write project files; check completeness, permissions and disk space.",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
