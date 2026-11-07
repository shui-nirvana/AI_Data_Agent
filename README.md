# AI Data Agent

AI Data Agent is a sandbox-first CSV cleaning demonstration. A fixed profiler finds issues, a person approves per-column rules, and an LLM generates cleaning code for approved changes. A fresh isolated Docker container executes the code, and an independent verifier checks the result before publication.

## Status

- On an Ubuntu 24.04 x86-64 Vultr VM, a browser-driven 50,000-row workflow completed through an HTTPS reverse proxy with Vultr Serverless Inference and `deepseek-v4.1-flash-1017`. Independent verification passed, and the downloaded CSV was checked against the input.
- The temporary online demo uses a trusted TLS certificate and a shared demo token. Selected unauthenticated task requests were rejected; the deployment is not a hardened or multi-user service.
- Selected Docker isolation, resource and failure-recovery tests also passed on that VM. These are scoped tests, not a security certification.
- Windows local Docker and Vultr inference completed a separate 50,000-row controlled repair test using an injected failure.
- This is a single-user demo for non-sensitive data, not a production service.

## Infrastructure

The backend consists of a FastAPI service, a LangGraph Runner, a SQLite-backed queue and an independent Docker worker. The Runner connects to a configured provider for code generation and eligible repairs; the worker executes jobs in Docker containers on the same host. Vultr Serverless Inference and a Linux Vultr VM have both been exercised. See [testing](docs/testing.md) for the scope of the observed runs.

## Workflow

1. Upload a CSV and explicitly start fixed profiling (A).
2. Review and approve the per-column actions. The business goal is saved as a description; it is not converted into rules.
3. When changes are approved, generate code and run it in a new container (C). Eligible execution failures can trigger bounded repair using the exact source that was executed.
4. Independently verify the candidate against the input and approved rules (D). Only verified and registered artifacts are downloadable. When every column is preserved, generation and C are skipped and D requires byte-identical output.
5. Optionally request a model-assisted explanation. The model selects presentation order and style; the system inserts verified facts. This cannot change D's verdict or download permission.

The supported actions are `preserve`, date normalization for selected formats, and replacing empty strings with a literal. An empty fill literal is allowed but leaves empty cells unchanged. At least one column must remain unchanged; rows are not deleted, deduplicated or reordered. D checks the approved rules, not whether every data issue was found.

## Quick Start

On Windows, install Python 3.11 or newer and Docker Desktop with its Linux engine running:

```powershell
python start.py
```

On Linux, install Python with `venv`, Git and Docker Engine, then run from the project directory:

```bash
python3 start_linux.py --mode preserve
# Or, to authorize external model calls:
python3 start_linux.py --mode vultr --allow-external
```

Run one start command at a time. The launcher prepares dependencies and the job image, starts the API, Graph Runner and worker, and displays a private temporary demo token. Open the local page, enter the token, upload a non-sensitive CSV, start profiling, and approve the rules. Preserve mode does not call a model. In Vultr mode, supply the model ID and inference key when prompted or configure them privately as described in [operations](docs/operations.md). Startup checks key format, not remote authentication.

```powershell
python start.py status
python start.py stop
```

On Linux use `python3 start_linux.py status` or `python3 start_linux.py stop`. Keep the launcher terminal open. The API listens on `127.0.0.1` by default; remote access can use a trusted SSH tunnel or an explicitly configured HTTPS reverse proxy, as described in [operations](docs/operations.md). Readiness does not prove model authentication or the full workflow. Initial setup and image builds need network access; jobs run without network access.

## Privacy And Limits

External model requests require explicit consent and may incur charges. Generation sends anonymized column indices, approved rule summaries and aggregate statistics. Repair also sends the complete source actually executed by the failing job and limited diagnostics. Raw rows, real column names, literal fill values and raw exceptions or stacks are not deliberately sent as separate data, but anything present in the source is sent with it. Do not use sensitive data or expose this demo publicly.

Docker containers share the host kernel; sandboxing is not a security certification. The demo uses a shared token, not multi-user isolation. Upload, active-task and sandbox resource limits exist. An optional Nginx template provides per-IP request limits when deployed; there is no account-level monetary budget. Spreadsheet formulas may remain in exported CSVs. See [security](docs/security.md).

## Tests And Documentation

```powershell
python -m pip install -e ".[llm,worker,workflow,test]"
python -m pytest -q
```

Docker, browser and live model tests require additional explicit opt-in. Live model calls cost money; see [testing](docs/testing.md).

- [Architecture](docs/architecture.md)
- [Operations](docs/operations.md)
- [Security](docs/security.md)
- [Testing and evidence](docs/testing.md)

Licensed under the [MIT License](LICENSE).
