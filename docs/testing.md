# Testing And Evidence

## Offline Regression

Install the optional dependencies and run `python -m pytest -q`. Tests requiring Docker, browser automation, or live model calls are separately gated and skipped unless enabled. A skip is not a pass. Docker tests need a running Linux Docker engine and a current `.runtime/docker/images.json` built from the current source:

```powershell
python -m workers.sandbox_manager.build_images --target local --platform linux/amd64
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
python -m pytest tests/test_local_safety_docker.py tests/test_workflow_docker.py tests/test_llm_docker.py -q
```

The default suite does not require a paid model call. Tests can use synthetic inputs and test generators; those are not evidence of live model behavior.

## Reverse Proxy

The optional Nginx integration test starts disposable Nginx and mock upstream containers, creates a short-lived self-signed test certificate, and exercises TLS, redirects, forwarding, upload limits, rate limits and upstream failure. It does not validate the deployed VM, production certificate, public firewall, or the complete application workflow. Install the Docker optional dependency and make the `nginx:1.27-alpine` and `python:3.12-slim` images available locally before opting in:

```powershell
$env:AI_AGENT_RUN_PROXY_TESTS = "1"
python -m pytest tests/test_reverse_proxy.py -q
```

This test does not call a model. The corresponding API authentication and task tests run separately.

## Observed Live Runs

On November 6, 2026, local Docker with Vultr Serverless Inference and `deepseek-v4.1-flash-1017` completed one normal and one controlled repair workflow using a 50,000-row synthetic CSV. Each ended successfully; independent D reported 20,000 changed cells and zero mismatches. In the controlled repair run, the first generated source was copied and a fixed exception injected; the production implementation handled diagnosis, repair, execution and D. The repair request's previous source matched the source actually executed by the failed job.

These are two examples, not a statistical reliability claim. They do not validate deployment on a Vultr VM or prove resistance to all host failures. Local evidence may contain full source and model requests and is not included in this repository.

## Opt-In Large Live Tests

These tests require explicit permission, a working Docker image and Vultr credentials. They make real, potentially billable calls and may write sensitive request/response evidence to pytest's temporary directory. Model-assisted explanation calls incur additional cost.

The original 50,000-row sample is not distributed in this repository. Both large tests require that exact sample, identified by SHA-256 `c98ed8d51c9ae8aa0c71e8f72841d8255914339b9d9b3094841d45bed63b4892`; setting `AI_AGENT_LARGE_SAMPLE` to a different synthetic CSV does not make it interchangeable. No sample generator is currently included.

```powershell
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
$env:AI_AGENT_RUN_LIVE_LLM_TESTS = "1"
$env:AI_AGENT_RUN_LARGE_LIVE_LLM_TESTS = "1"
$env:AI_AGENT_LLM_ALLOW_EXTERNAL = "true"
$env:AI_AGENT_LLM_MODEL = "deepseek-v4.1-flash-1017"
python -m pytest tests/test_vultr_large_live.py -q -s
$env:AI_AGENT_TEST_LIVE_PROVIDER = "vultr"
python -m pytest tests/test_controlled_large_repair.py -q -s
```

Review and unset the test flags after use. Never publish unreviewed evidence or credentials.
