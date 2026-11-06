# Operations

These instructions describe the Windows local demonstration. Linux and Vultr VM deployment have not been accepted, and `start.py` is a Windows launcher. Use a running Docker Desktop Linux engine, Python 3.11 or newer, and a trusted local machine. The Windows control-plane dependency lock was validated with Python 3.12.

## Start And Stop

Run `python start.py` from the repository root. The launcher installs locked dependencies, builds or reuses the job image, asks for model consent, and starts the API, Runner and worker. Enter the displayed demo token in the local page. Uploading does not start profiling automatically; explicitly start it and approve the proposed actions. Keep the terminal open; use Ctrl+C or `python start.py stop` to stop.

`python start.py status` checks HTTP health and recent process heartbeats, not the complete workflow. Local task data is stored under `.runtime/`. The generated source package is an offline development bundle, not a complete cloud installer:

```powershell
python -m workers.sandbox_manager.build_images --target vultr --platform linux/amd64 --export --output .runtime/vultr
```

Use the correct target CPU architecture. The `vultr` label alone does not deploy or validate anything.

## Vultr Inference

Obtain a Vultr Serverless Inference key, not an account-management API key. Set `VULTR_SERVERLESS_INFERENCE_API_KEY` in the Runner environment or place the single key assignment in the ignored root file `.env.vultr`. Do not commit or disclose it. Environment variables take precedence; an explicitly empty value does not fall back to the file. The example configuration is not automatically loaded.

```powershell
$env:AI_AGENT_LLM_MODEL = "deepseek-v4.1-flash-1017"
python start.py --mode vultr --allow-external
```

This explicitly permits external model requests and associated costs. The adapter requests `reasoning.enabled=false`; unsupported models may fail. No automatic provider switch or implicit retry is promised. See [security](security.md) for what is sent.

System proxy use is off by default. When direct access fails, `AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED=true` opts in to a trusted local HTTP proxy without credentials. TLS verification remains enabled, but a trusted intercepting proxy can read requests and keys.

## Uncertain Container Creation

After interruption, the worker reconciles old running jobs without automatically rerunning old C attempts. When creation remains uncertain, it stops processing new work and preserves the pending state. Do not clear the marker based on an empty container listing.

Stop this project's services, assess the impact on other workloads, restart the Docker daemon to terminate old create requests, and verify the attempt's container has been reclaimed. Only after that confirmation, run:

```powershell
.\.venv\Scripts\python.exe -m workers.sandbox_manager.queue_worker --data-dir .runtime --images .runtime/docker/images.json --resolve-uncertain <attempt-id> --confirm-daemon-restarted
```

The flag is an operator assertion; the program does not prove the daemon was restarted. Errors require further investigation, not bypassing recovery. Cloud service management must also avoid killing the independent watchdog when a worker exits.

## Capacity And Maintenance

Configure upload size, active tasks and minimum free disk using `AI_AGENT_MAX_UPLOAD_BYTES`, `AI_AGENT_MAX_ACTIVE_TASKS`, and `AI_AGENT_MIN_FREE_DISK_BYTES`. There is no automatic account-level spending cap. See [security](security.md).

Offline maintenance is available via `python -m backend.local_runtime backup`, `restore`, and `cleanup`; inspect `--help` and stop all services first. Backups contain private task data and source. Restore requires the original absolute runtime path. Cleanup defaults to a dry run; `--apply` removes eligible terminal-task files.
