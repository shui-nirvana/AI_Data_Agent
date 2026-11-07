# User Guide

## Open The Demo

Open the [online demo](https://139.180.154.243.sslip.io/) and enter the current **Demo access token**, provided privately by the operator. If the browser shows a certificate warning, do not enter the token. The online address is temporary; contact the operator if it is unavailable.

Use only non-sensitive data and keep the token private. Everyone sharing the token can access the same owner's tasks.

## Clean A CSV

1. Under **Create a task**, enter a **Business goal**, choose a UTF-8 CSV file, and select **Upload and register**. The goal is descriptive; it does not automatically become a cleaning rule.
2. Under **Task status**, select **Start fixed detection**. Wait for `AWAITING_DECISIONS`, then review **Fixed detection and human decisions**.
3. Choose an action for each column. **Preserve** leaves it unchanged; **Fill empty strings** replaces only empty cells with the value you enter; **Normalize dates** converts selected valid date formats to `YYYY-MM-DD`. At least one column must remain unchanged.
4. Review the choices and consent statement, check the confirmation box, and select **Submit and freeze approval**. Model calls may incur charges; choosing **Preserve** for every column skips model generation.
5. When the status is `SUCCEEDED`, download the cleaned CSV and reports from **Execution and validation results**. The optional **Generate validation explanation** makes a separate model call and may incur an additional charge.

An empty fill value leaves empty cells unchanged. Fill values are case-sensitive: `UNKNOWN` and `unknown` are different. A successful validation means the output follows the approved rules; it does not mean every possible issue in the original data was detected.

To reopen a task, enter its ID under **Restore task ID** and select **Load task**. If your token is rejected, ask the operator for the current token: it changes when the application restarts.

## Run Locally

Install Python 3.11 or newer and a running Linux Docker engine. On Windows, use Docker Desktop and run from the project directory:

```powershell
python start.py
```

On Linux, use one of these commands from the project directory:

```bash
python3 start_linux.py --mode preserve
python3 start_linux.py --mode vultr --allow-external
```

Choose the first command for an all-preserve run without a model, or the second for Vultr model calls. When prompted, provide the Vultr Serverless Inference key and model ID privately. The key input does not echo. The launcher displays the local page address and a new demo token; keep the terminal open while using the app.

To stop, use Ctrl+C or run `python start.py stop` on Windows or `python3 start_linux.py stop` on Linux. For remote access, use the configured HTTPS address or a trusted SSH tunnel. See [security](security.md) for data and access limits.

## Operator Notes

The API binds to `127.0.0.1` by default. The optional `deploy/nginx.conf.example` requires a domain, trusted certificate and firewall configuration; it is not an installer. For Certbot HTTP-01 webroot renewal, configure a reachable challenge location and reload Nginx after renewal. Do not put tokens or private keys in the repository.

If container creation becomes uncertain, the worker stops new work. Do not delete `creation.pending` or assume an empty container list resolves it. An operator must stop the project, assess other workloads, restart the Docker daemon to end old create requests, and verify the affected container is reclaimed before using `--resolve-uncertain` with `--confirm-daemon-restarted`. See the command help for the queue worker. Historical task files consume disk; review maintenance options before deleting them.
