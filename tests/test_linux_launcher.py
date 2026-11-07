"""Linux launcher unit tests do not access Docker or a model provider."""

import pytest

import start as common
import start_linux as launcher


def test_linux_platform_selects_venv_and_architecture(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(common, "VENV_PYTHON", common.VENV_PYTHON)
    assert launcher.configure_linux() == "linux/amd64"
    assert common.VENV_PYTHON == common.VENV / "bin" / "python"
    monkeypatch.setattr(launcher.platform, "machine", lambda: "aarch64")
    assert launcher.configure_linux() == "linux/arm64"
    monkeypatch.setattr(launcher.platform, "machine", lambda: "unsupported")
    with pytest.raises(common.LaunchError, match="x86_64"):
        launcher.configure_linux()


def test_linux_rejects_other_platforms(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    with pytest.raises(common.LaunchError, match="requires Linux"):
        launcher.configure_linux()


def test_linux_requires_consent_before_preparation(monkeypatch):
    monkeypatch.setattr(launcher, "configure_linux", lambda: "linux/amd64")
    monkeypatch.setattr(launcher, "check_docker", lambda _: pytest.fail("must not prepare"))
    with pytest.raises(common.LaunchError, match="allow-external"):
        launcher.launch(8000, "vultr", False)


def test_linux_docker_check_requires_linux_engine(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(common, "run", lambda *args, **kwargs: "linux")
    launcher.check_docker({})
    monkeypatch.setattr(common, "run", lambda *args, **kwargs: "windows")
    with pytest.raises(common.LaunchError, match="Linux containers"):
        launcher.check_docker({})


def test_linux_launch_keeps_secrets_out_of_preparation(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "configure_linux", lambda: "linux/amd64")
    monkeypatch.setenv("VULTR_SERVERLESS_INFERENCE_API_KEY", "private-model-key")
    monkeypatch.setenv("VULTR_API_KEY", "private-account-key")
    monkeypatch.setenv("AI_AGENT_DEMO_TOKEN", "old-demo-token")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.example:2375")
    monkeypatch.setattr(common, "port_available", lambda _: True)
    preparations = []
    monkeypatch.setattr(launcher, "check_docker", lambda env: preparations.append(dict(env)))
    monkeypatch.setattr(common, "prepare_dependencies", lambda env: preparations.append(dict(env)))
    monkeypatch.setattr(common, "prepare_images",
                        lambda platform, env, target: preparations.append(dict(env, target=target)))
    monkeypatch.setattr(common, "choose_model", lambda mode, consent, env: None)
    monkeypatch.setattr(common, "wait_ready", lambda *args: None)
    started = []

    class Process:
        returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda command, **kwargs: started.append((command, kwargs)) or Process())
    launcher.launch(8000, "vultr", True)
    for environment in preparations:
        assert "VULTR_SERVERLESS_INFERENCE_API_KEY" not in environment
        assert "VULTR_API_KEY" not in environment
        assert "AI_AGENT_DEMO_TOKEN" not in environment
        assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert preparations[-1]["target"] == "vultr"
    assert started[0][1]["start_new_session"] is True
    environment = started[0][1]["env"]
    assert environment["VULTR_SERVERLESS_INFERENCE_API_KEY"] == "private-model-key"
    assert "VULTR_API_KEY" not in environment
    assert environment["AI_AGENT_DEMO_TOKEN"] != "old-demo-token"
    assert "private-model-key" not in str(started[0][0])
    assert "private-model-key" not in capsys.readouterr().out


def test_noninteractive_mode_requires_explicit_choice(monkeypatch):
    monkeypatch.setattr(launcher, "configure_linux", lambda: "linux/amd64")
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)
    with pytest.raises(common.LaunchError, match="Select --mode"):
        launcher.launch(8000, None, False)
