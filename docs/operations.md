# Operations

The Windows local demonstration uses `start.py` and Docker Desktop. `start_linux.py` is a separate foreground launcher for Linux. Python 3.11 or newer is required. The Linux launcher and locked dependencies have been exercised on an Ubuntu 24.04 x86-64 Vultr VM with Python 3.12 and Docker Engine; this does not establish support for every Linux distribution or architecture.

## Start And Stop

Run `python start.py` from the repository root. The launcher installs locked dependencies, builds or reuses the job image, asks for model consent, and starts the API, Runner and worker. Enter the displayed demo token in the local page. Uploading does not start profiling automatically; explicitly start it and approve the proposed actions. Keep the terminal open; use Ctrl+C or `python start.py stop` to stop.

`python start.py status` checks HTTP health and recent process heartbeats, not the complete workflow. Local task data is stored under `.runtime/`. The generated source package is an offline development bundle, not a complete cloud installer:

```powershell
python -m workers.sandbox_manager.build_images --target vultr --platform linux/amd64 --export --output .runtime/vultr
```

Use the correct target CPU architecture. The `vultr` label alone does not deploy or validate anything.

## Linux Launcher

On a trusted Linux host, install Python with `venv`, Docker Engine with a local Unix socket, and Git. The user starting the launcher must have access to the Docker socket; that access is effectively host administrator access. The launcher uses the local Docker daemon, installs the hash-locked control dependencies, builds or reuses the job image and starts API, Runner and worker. Dependency installation and image builds require network access. It supports `x86_64` and `aarch64`; the recorded VM run used `x86_64`.

```bash
python3 start_linux.py --mode preserve
python3 start_linux.py --mode vultr --allow-external
python3 start_linux.py status
python3 start_linux.py stop
```

Run one start command at a time, from the project directory. Preserve mode makes no model calls and can only complete the all-preserve path. Vultr mode requires an inference key and model ID; interactive startup can prompt for both, or they can be configured as described below. External calls may incur costs. Without a mode, an interactive prompt offers the same choices. The launcher displays a freshly generated demo token on each start; do not put it in logs, screenshots or chat. The API binds to `127.0.0.1`; the launcher does not configure Nginx, DNS, certificates, a firewall or systemd.

The launcher runs in the foreground and starts the supervisor in a separate session so terminal Ctrl+C does not directly kill its worker or watchdog. Use Ctrl+C or the stop command and check recovery before assuming containers are gone. If the launcher is terminated or the terminal disconnects, the supervisor may remain running and the displayed token may be lost; check status and stop before restarting. No automatic service restart is configured. Any future service manager configuration must preserve watchdog recovery rather than terminating the whole process group.

## Remote Access Through SSH

For a trusted operator, keep the VM API bound to loopback and open a separate SSH tunnel from the client machine. Replace `user@vm-host` with the SSH login and host; select another local port if `18001` is occupied:

```bash
ssh -N -L 127.0.0.1:18001:127.0.0.1:8000 user@vm-host
```

Keep the tunnel open and visit `http://127.0.0.1:18001/` on that client. If the API uses a different port, update the tunnel's destination port. A listening local port alone does not prove the tunnel reaches the application. Protect SSH credentials and the demo token; users sharing the token access the same tasks. SSH access is not a substitute for a public-facing authentication design.

## Run A Task

1. Enter the current demo token, a business goal and a non-sensitive CSV, then select **Upload and register**. The business goal is descriptive only.
2. Select **Start fixed detection** and wait for `AWAITING_DECISIONS`. Review the fixed findings and choose an allowed action and its parameters for each card. At least one column must remain unchanged.
3. For date normalization, explicitly select the input formats. For filling, enter the replacement in **Fill value**: it replaces empty strings only. An empty replacement is allowed but leaves empty strings unchanged. Preserve keeps the column unchanged.
4. Confirm the decisions and consent, then wait for the result. If every column is preserved, the model and C are skipped and D checks the unchanged input. Otherwise the configured model generates code and C runs it.
5. On success, download the registered artifacts. The status evidence identifies model provider, model and attempts when present. The optional validation explanation makes a separate model call and can incur additional charges; it cannot change D or download access.

The CSV contract and supported rules are described in [architecture](architecture.md). A successful D verdict means the output conforms to approved rules, not that the input had no other issues.

## Optional Nginx Reverse Proxy

`deploy/nginx.conf.example` is a starting configuration, not an installer. A manually configured variant has been exercised on a Vultr VM using a trusted certificate and an HTTP-01 challenge location; see [testing](testing.md) for the checks performed. The example terminates HTTPS, redirects HTTP to the configured HTTPS domain, forwards to `127.0.0.1:8000`, and adds per-IP request and connection limits. It does not replace authentication or isolate shared-token users. Use only synthetic data and restrict token distribution to trusted demo users.

1. Install Nginx on the VM and arrange a real domain pointing to it. Obtain a trusted certificate and private key for that domain, and arrange automatic renewal and an Nginx reload after renewal. Certificate provisioning is not included. Never put private keys in the repository or job containers.
2. Copy the example into a file included in Nginx's `http` context, such as `/etc/nginx/conf.d/ai-data-agent.conf`. Replace every `agent.example.com` occurrence and both certificate paths; adjust the upstream port if necessary. Review existing Nginx sites and port usage before enabling it.
3. Keep the API bound to `127.0.0.1` and restrict direct access to the API and Docker socket. Allow the required HTTPS and, if used for redirects or certificate validation, HTTP ports through the firewall. Protect SSH separately.
4. Check `sudo nginx -t`, then reload Nginx through the host service manager. Verify the certificate, HTTPS access, token authentication, upload, and 429/413 responses before allowing demo access. DNS, certificate and firewall settings must also be tested on the actual VM.

Always use the HTTPS URL from the start: a redirect cannot protect credentials or data already sent over HTTP. The template disables access logs to avoid recording task URLs, but operators must still protect and review error logs. It overwrites incoming forwarding headers and assumes Nginx receives clients directly; adding a CDN or load balancer requires an explicit trusted-proxy configuration. Rate limits are per client IP, not per user, and shared networks can hit them together. The 10 MiB proxy body limit matches the application's default; adjust both when changing the application limit. No model key or Docker socket is needed by Nginx.

### HTTP-01 Certificate Renewal

The example redirects all HTTP requests and does not itself provide an ACME challenge location. When using Certbot's webroot method, replace the HTTP server's top-level `return` with locations like these, substituting the configured hostname and webroot:

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/www/html;
}
location / {
    return 308 https://agent.example.com$request_uri;
}
```

The challenge location must be reachable on port 80, and the same webroot must be used for issuance and renewal. Test with `sudo nginx -t` before reloading. Confirm renewal for the intended certificate with `sudo certbot renew --dry-run --cert-name <certificate-name>`. A dry run alone does not verify that Nginx reloads the renewed certificate.

On the observed VM, a Certbot deploy hook checks Nginx configuration and reloads Nginx after successful renewal; it was tested manually, and a Certbot timer was scheduled. Monitor actual renewals and hook failures. The hook is host configuration, not included or installed by this repository, and applies to successful certificate renewals on that host. The certificate name, DNS, firewall and hook must be configured for each deployment.

## Vultr Inference

Obtain a Vultr Serverless Inference key, not an account-management API key. Set `VULTR_SERVERLESS_INFERENCE_API_KEY` in the Runner environment or place the single key assignment in the ignored root file `.env.vultr`. Do not commit or disclose it. Environment variables take precedence; an explicitly empty value does not fall back to the file. The example configuration is not automatically loaded.

```powershell
$env:AI_AGENT_LLM_MODEL = "deepseek-v4.1-flash-1017"
python start.py --mode vultr --allow-external
```

This explicitly permits external model requests and associated costs. The adapter requests `reasoning.enabled=false`; unsupported models may fail. No automatic provider switch or implicit retry is promised. See [security](security.md) for what is sent.

For Linux, set `AI_AGENT_LLM_MODEL` in the environment or enter the model ID at the prompt. If the key is not already configured, an interactive prompt accepts it without echoing or saving it. The launcher's key-format check makes no network request; successful authentication is established only by an actual model call. Do not paste keys or demo tokens into support messages.

System proxy use is off by default. When direct access fails, `AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED=true` opts in to a trusted local HTTP proxy without credentials. TLS verification remains enabled, but a trusted intercepting proxy can read requests and keys.

## Uncertain Container Creation

After interruption, the worker reconciles old running jobs without automatically rerunning old C attempts. When creation remains uncertain, it stops processing new work and preserves the pending state. Do not clear the marker based on an empty container listing.

Stop this project's services, assess the impact on other workloads, restart the Docker daemon to terminate old create requests, and verify the attempt's container has been reclaimed. Only after that confirmation, run:

```powershell
.\.venv\Scripts\python.exe -m workers.sandbox_manager.queue_worker --data-dir .runtime --images .runtime/docker/images.json --resolve-uncertain <attempt-id> --confirm-daemon-restarted
```

On Linux, from the project directory, use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe`. Replace `<attempt-id>` with the affected attempt ID. This procedure is not needed for a normal completed task.

The flag is an operator assertion; the program does not prove the daemon was restarted. Errors require further investigation, not bypassing recovery. Cloud service management must also avoid killing the independent watchdog when a worker exits.

## Capacity And Maintenance

Configure upload size, active tasks and minimum free disk using `AI_AGENT_MAX_UPLOAD_BYTES`, `AI_AGENT_MAX_ACTIVE_TASKS`, and `AI_AGENT_MIN_FREE_DISK_BYTES`. There is no automatic account-level spending cap. See [security](security.md).

Completed tasks and their artifacts occupy disk until maintenance is performed. For a read-only check on Linux, inspect `df -h /`, `du -sh .runtime`, and `docker ps -a --filter label=ai-data-agent.task`. Inspect `find .runtime/worker_jobs -name creation.pending -print` for uncertain creates. An empty container list is only a snapshot and does not resolve a pending creation.

Offline maintenance is available via `python -m backend.local_runtime backup`, `restore`, and `cleanup`; inspect `--help` and stop all services first. Backups contain private task data and source. Restore requires the original absolute runtime path. Cleanup defaults to a dry run; `--apply` removes eligible terminal-task files and can affect historical task downloads.
