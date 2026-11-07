# Operations

The Windows local demonstration uses `start.py` and Docker Desktop. `start_linux.py` is a separate foreground launcher for Linux; Vultr VM deployment and the Linux launcher have not yet been accepted. Python 3.11 or newer is required. The control-plane dependency lock was validated on Windows with Python 3.12 and must still be verified on Linux.

## Start And Stop

Run `python start.py` from the repository root. The launcher installs locked dependencies, builds or reuses the job image, asks for model consent, and starts the API, Runner and worker. Enter the displayed demo token in the local page. Uploading does not start profiling automatically; explicitly start it and approve the proposed actions. Keep the terminal open; use Ctrl+C or `python start.py stop` to stop.

`python start.py status` checks HTTP health and recent process heartbeats, not the complete workflow. Local task data is stored under `.runtime/`. The generated source package is an offline development bundle, not a complete cloud installer:

```powershell
python -m workers.sandbox_manager.build_images --target vultr --platform linux/amd64 --export --output .runtime/vultr
```

Use the correct target CPU architecture. The `vultr` label alone does not deploy or validate anything.

## Linux Launcher

On a trusted Linux host, install Python with `venv`, Docker Engine with a local Unix socket, and Git. The user starting the launcher must have access to the Docker socket; that access is effectively host administrator access. The launcher uses the local Docker daemon, installs the existing hash-locked control dependencies, builds or reuses the job image and starts API, Runner and worker. Dependency installation and image builds require network access. Linux compatibility of the Windows-generated control lock still requires target validation.

```bash
python3 start_linux.py --mode preserve
python3 start_linux.py --mode vultr --allow-external
python3 start_linux.py status
python3 start_linux.py stop
```

Run one start command at a time, from the project directory. Preserve mode makes no model calls and can only exercise the all-preserve path. Vultr mode requires an inference key and `AI_AGENT_LLM_MODEL`, as described below; external calls may incur costs. Without a mode, an interactive prompt offers the same choices. The launcher displays a freshly generated demo token; do not put it in logs, screenshots or chat. API binds to `127.0.0.1`; the launcher does not configure Nginx, DNS, certificates, a firewall or systemd.

The launcher runs in the foreground and starts the supervisor in a separate session so terminal Ctrl+C does not directly kill its worker or watchdog. Use Ctrl+C or the stop command and check recovery before assuming containers are gone. If the launcher is terminated or the terminal disconnects, the supervisor may remain running and the displayed token may be lost; check status and stop before restarting. No automatic service restart is configured. Any future service manager configuration must preserve watchdog recovery rather than terminating the whole process group.

## Optional Nginx Reverse Proxy

`deploy/nginx.conf.example` is a starting configuration, not an installer. It has not yet been accepted on a Vultr VM. It terminates HTTPS, redirects HTTP to the configured HTTPS domain, forwards to `127.0.0.1:8000`, and adds per-IP request and connection limits. It does not replace authentication or isolate shared-token users. Do not use sensitive data or make the current shared-token demo unrestricted on the public internet.

1. Install Nginx on the VM and arrange a real domain pointing to it. Obtain a trusted certificate and private key for that domain, and arrange automatic renewal and an Nginx reload after renewal. Certificate provisioning is not included. Never put private keys in the repository or job containers.
2. Copy the example into a file included in Nginx's `http` context, such as `/etc/nginx/conf.d/ai-data-agent.conf`. Replace every `agent.example.com` occurrence and both certificate paths; adjust the upstream port if necessary. Review existing Nginx sites and port usage before enabling it.
3. Keep the API bound to `127.0.0.1` and restrict direct access to the API and Docker socket. Allow the required HTTPS and, if used for redirects or certificate validation, HTTP ports through the firewall. Protect SSH separately.
4. Check `sudo nginx -t`, then reload Nginx through the host service manager. Verify the certificate, HTTPS access, token authentication, upload, and 429/413 responses before allowing demo access. DNS, certificate and firewall settings must also be tested on the actual VM.

Always use the HTTPS URL from the start: a redirect cannot protect credentials or data already sent over HTTP. The template disables access logs to avoid recording task URLs, but operators must still protect and review error logs. It overwrites incoming forwarding headers and assumes Nginx receives clients directly; adding a CDN or load balancer requires an explicit trusted-proxy configuration. Rate limits are per client IP, not per user, and shared networks can hit them together. The 10 MiB proxy body limit matches the application's default; adjust both when changing the application limit. No model key or Docker socket is needed by Nginx.

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
