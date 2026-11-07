# Testing And Evidence

## Offline Regression

Install the optional dependencies and run `python -m pytest -q`. Tests requiring Docker, browser automation, or live model calls are separately gated and skipped unless enabled. A skip is not a pass. Docker tests need a running Linux Docker engine and a current `.runtime/docker/images.json` built from the current source:

```powershell
python -m workers.sandbox_manager.build_images --target local --platform linux/amd64
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
python -m pytest tests/test_local_safety_docker.py tests/test_workflow_docker.py tests/test_llm_docker.py -q
```

The default suite does not require a paid model call. Tests can use synthetic inputs and test generators; those are not evidence of live model behavior.

## Docker Isolation And Recovery

The following Linux commands use fixed test source, not a model. They create test containers and temporary evidence. Run them on a trusted host when no other tasks are active: stress checks consume resources, and the recovery tests intentionally kill their own test worker processes. They do not need to restart Docker or stop the application service.

```bash
env -u VULTR_SERVERLESS_INFERENCE_API_KEY -u DEEPSEEK_API_KEY \
  -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
  DOCKER_HOST=unix:///var/run/docker.sock \
  .venv/bin/python -m workers.sandbox_manager.isolation_test \
  --images .runtime/docker/images.json \
  --output /tmp/ai-data-agent-isolation --stress

env -u VULTR_SERVERLESS_INFERENCE_API_KEY -u DEEPSEEK_API_KEY \
  -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
  DOCKER_HOST=unix:///var/run/docker.sock \
  AI_AGENT_RUN_DOCKER_TESTS=1 \
  AI_AGENT_DOCKER_IMAGES="$PWD/.runtime/docker/images.json" \
  .venv/bin/python -m pytest tests/test_local_safety_docker.py -v
```

The isolation script checks selected denied operations, temporary storage quotas, log truncation, wall timeout and a memory-stress failure. A denied-operation result does not independently validate every kernel control; the memory check accepts several failure modes and does not by itself prove a kernel OOM kill. The pytest suite exercises PID pressure, aggregate CPU budget, cancellation, worker crash and watchdog recovery. Its PID and CPU fault driver bypasses the Python audit layer for that test while keeping Docker policy. Docker API timeout and lost create response are simulated; the lost-response test creates a real container and then simulates the missing response.

After the tests, check for project-labeled containers and pending markers in the application's worker directory. The tests use separate temporary directories; this marker search does not inspect them. Never clear a `creation.pending` marker merely because the container listing is empty; follow [operations](operations.md) if one is found in the application runtime.

```bash
docker ps -a --filter label=ai-data-agent.task
find .runtime/worker_jobs -name creation.pending -print
```

## Reverse Proxy

The optional Nginx integration test starts disposable Nginx and mock upstream containers, creates a short-lived self-signed test certificate, and exercises TLS, redirects, forwarding, upload limits, rate limits and upstream failure. That automated test does not by itself validate a deployed VM, its certificate, public firewall, or the complete application workflow. Install the Docker optional dependency and make the `nginx:1.27-alpine` and `python:3.12-slim` images available locally before opting in:

```powershell
$env:AI_AGENT_RUN_PROXY_TESTS = "1"
python -m pytest tests/test_reverse_proxy.py -q
```

This test does not call a model. The corresponding API authentication and task tests run separately.

## Observed Runs

On November 6, 2026, local Docker with Vultr Serverless Inference and `deepseek-v4.1-flash-1017` completed one normal and one controlled repair workflow using a 50,000-row synthetic CSV. Each ended successfully; independent D reported 20,000 changed cells and zero mismatches. In the controlled repair run, the first generated source was copied and a fixed exception injected; the production implementation handled diagnosis, repair, execution and D. The repair request's previous source matched the source actually executed by the failed job.

On November 7, 2026:

- An Ubuntu 24.04 x86-64 Vultr VM with Python 3.12 and Docker Engine completed a browser-driven 50,000-row synthetic CSV workflow using Vultr Serverless Inference and `deepseek-v4.1-flash-1017`. The user approved date normalization and filling empty email cells with `UNKNOWN`. D reported 20,000 changed cells and zero mismatches. The downloaded CSV hash matched the recorded output hash, and a separate local comparison checked all rows and columns against the approved changes.
- A later browser-driven run through the VM's HTTPS reverse proxy used the same input, selected `YYYY/MM/DD` date normalization and filled empty emails with lowercase `unknown`. D passed with 20,000 changed cells and zero mismatches. The downloaded output hash matched the published artifact; a separate case-sensitive comparison checked all 50,000 rows and 12 columns against those rules. Compared with the earlier output, only the 10,000 filled email cells differed: `UNKNOWN` versus `unknown`.
- The temporary HTTPS setup used Nginx, a trusted certificate, UFW and an HTTP-01 webroot challenge. The browser reported no certificate warning, and external unauthenticated task-read and task-create requests returned HTTP 401. A Certbot renewal dry run succeeded, the Nginx reload hook was run manually, and a Certbot timer was listed. These checks did not observe an actual scheduled renewal or independently exercise the deployed proxy's 429 and 413 limits.
- On Windows local Docker, a separate 50,000-row controlled repair test used the same Vultr model. A fixed exception was inserted into a copy of the first generated source; the application handled failure diagnosis, repair and D. The test checked that the repair request used the actual failed execution snapshot. This was an API-driven test with simulated approval, not a browser run.
- On the VM at revision `bd9ecf6`, the isolation script with `--stress` passed its selected checks, and `tests/test_local_safety_docker.py` reported 11 passed. Afterwards, no project-labeled containers or `creation.pending` markers in the application's worker directory were found.

These observations are examples, not statistical reliability claims or certification. The normal VM run did not request the optional model-assisted validation explanation. Local and VM test evidence may contain source or other private data and is not included in this repository.

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
