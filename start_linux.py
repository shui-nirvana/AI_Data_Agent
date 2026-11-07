"""Linux foreground launcher for the single-machine demo."""

import argparse
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time

import start as common


def configure_linux():
    if not sys.platform.startswith("linux"):
        raise common.LaunchError("Environment", "This launcher requires Linux.")
    common.check_python_version(sys.version_info)
    machine = platform.machine().lower()
    architectures = {"x86_64": "linux/amd64", "amd64": "linux/amd64",
                     "aarch64": "linux/arm64", "arm64": "linux/arm64"}
    if machine not in architectures:
        raise common.LaunchError("Environment", "Only x86_64 and aarch64 are supported.")
    common.VENV_PYTHON = common.VENV / "bin" / "python"
    return architectures[machine]


def check_docker(env):
    if not shutil.which("docker"):
        raise common.LaunchError("Docker", "Install Docker Engine and its CLI on the Linux host.")
    system = common.run(
        ["docker", "info", "--format", "{{.OSType}}"], "Docker",
        "Cannot connect to the local Docker socket; check the daemon and permissions.",
        env=env, timeout=25, quiet=True,
    )
    if system != "linux":
        raise common.LaunchError("Docker", "The Docker Engine must run Linux containers.")


def launch(port, mode, allow_external):
    target_platform = configure_linux()
    if mode is None:
        if not sys.stdin.isatty():
            raise common.LaunchError("Model", "Select --mode preserve or --mode vultr explicitly.")
        print("Vultr inference may incur charges and sends approved summaries;")
        print("repair also sends the complete source executed by the failed job.")
        choice = input("Select 1 for Vultr inference, 2 for no model [2]: ").strip() or "2"
        if choice not in ("1", "2"):
            raise common.LaunchError("Model", "Select 1 or 2.")
        mode = "vultr" if choice == "1" else "preserve"
        allow_external = choice == "1"
    if mode == "vultr" and not allow_external:
        raise common.LaunchError("Model", "Vultr inference requires --allow-external.")
    env = dict(os.environ)
    secret_names = ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY", "VULTR_API_KEY")
    inherited = {name: env.pop(name) for name in secret_names if name in env}
    env.pop("AI_AGENT_DEMO_TOKEN", None)
    env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    env.pop("DOCKER_CONTEXT", None)
    env.pop("DOCKER_TLS_VERIFY", None)
    env.pop("DOCKER_CERT_PATH", None)
    env["AI_AGENT_EXECUTION_BACKEND"] = "disabled"
    env["AI_AGENT_DEPLOYMENT_PLATFORM"] = "vultr"
    check_docker(env)
    if not common.port_available(port):
        raise common.LaunchError("Port", f"127.0.0.1:{port} is already in use.")
    common.prepare_dependencies(env)
    common.prepare_images(target_platform, env, target="vultr")
    for name in ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY"):
        if name in inherited:
            env[name] = inherited[name]
    common.choose_model(mode, allow_external, env)
    env["AI_AGENT_DEMO_TOKEN"] = secrets.token_urlsafe(24)
    command = [str(common.VENV_PYTHON), "-m", "backend.local_runtime", "start",
               "--data-dir", str(common.RUNTIME), "--images", str(common.IMAGES),
               "--port", str(port)]
    started = time.time()
    try:
        process = subprocess.Popen(command, cwd=common.ROOT, env=env, start_new_session=True)
    except OSError as exc:
        raise common.LaunchError("Start", "Could not start the supervisor.") from exc
    try:
        common.wait_ready(process, common.RUNTIME, port, env["AI_AGENT_DEMO_TOKEN"], started)
        print(f"Local API: http://127.0.0.1:{port}/")
        print(f"Demo token (keep private): {env['AI_AGENT_DEMO_TOKEN']}", flush=True)
        print("Keep this terminal open. Use Ctrl+C or start_linux.py stop to stop.")
        if process.wait():
            raise common.LaunchError("Runtime", "Supervisor exited with an error; check recovery.")
    except (KeyboardInterrupt, common.LaunchError) as original:
        if process.poll() is None:
            (common.RUNTIME / "stop.request").touch(exist_ok=True)
            try:
                process.wait(timeout=110)
            except subprocess.TimeoutExpired as exc:
                raise common.LaunchError("Stop", "Supervisor did not exit; check Docker recovery.") from exc
        if process.returncode:
            raise common.LaunchError("Stop", "Supervisor or container recovery failed.") from original
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("start", "stop", "status"), default="start")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", choices=("vultr", "preserve"))
    parser.add_argument("--allow-external", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        if args.command == "start":
            launch(args.port, args.mode, args.allow_external)
        else:
            configure_linux()
            if args.command == "stop":
                common.request_stop(common.RUNTIME)
                print("Supervisor stopped and recovery completed.")
            else:
                common.show_status(common.RUNTIME, args.port)
    except KeyboardInterrupt:
        print("Interrupted; check supervisor and container recovery.")
    except common.LaunchError as exc:
        print(f"[Failed: {exc.stage}] {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("[Failed: filesystem] Check permissions and disk space.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
