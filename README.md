# AI Data Agent

AI Data Agent is a sandbox-first CSV cleaning demonstration for the Vultr hackathon. A fixed profiler finds issues, a person approves per-column rules, an LLM generates cleaning code, and a fresh isolated Docker container executes it. An independent verifier checks the result before publication.

## Status

- Vultr Serverless Inference has been exercised with `deepseek-v4.1-flash-1017` from a local machine. Local Docker and Vultr inference completed both a 50,000-row normal workflow and a controlled repair workflow.
- The application has **not yet been deployed or accepted on a Vultr VM**. A public demo, video, and final LabLab submission remain outstanding.
- This is a single-user demo for non-sensitive data, not a production or public-facing service. The current interface and runtime messages are in Chinese.

## Infrastructure

The backend consists of a FastAPI service, a LangGraph Runner, a SQLite-backed queue and an independent Docker worker. The Runner connects to Vultr Serverless Inference for code generation and eligible repairs; the worker executes jobs in local Docker containers. Vultr Serverless Inference is the Vultr service exercised so far. The intended Vultr VM hosting and sandbox deployment still require implementation and acceptance; no cloud VM execution is claimed.

## Workflow

1. Upload a CSV and explicitly start fixed profiling (A).
2. Review and approve the per-column actions. The business goal is saved as a description; it is not converted into rules.
3. When changes are approved, generate code and run it in a new container (C). Eligible execution failures can trigger bounded repair using the exact source that was executed.
4. Independently verify the candidate against the input and approved rules (D). Only verified and registered artifacts are downloadable. When every column is preserved, generation and C are skipped and D requires byte-identical output.
5. Optionally request a model-assisted explanation. The model selects presentation order and style; the system inserts verified facts. This cannot change D's verdict or download permission.

## Local Quick Start

On Windows, install Python 3.11 or newer and Docker Desktop with its Linux engine running. The control-plane lock was validated on Windows with Python 3.12; other versions are not yet validated.

```powershell
python start.py
```

The launcher prepares dependencies and the job image, offers model choices, starts API, Graph Runner and worker processes, and displays a temporary demo token. Open the local page, enter the token, upload a non-sensitive CSV, start profiling, and approve the rules. Keep the terminal open.

```powershell
python start.py status
python start.py stop
```

The readiness check does not prove that Docker, the model, or the full workflow is healthy. See [operations](docs/operations.md) for model configuration and recovery. Initial setup and image builds need network access; jobs run without network access.

A separate `python3 start_linux.py` foreground launcher is available for Linux. The launcher and dependency lock have not yet been accepted on a Vultr VM; see [operations](docs/operations.md) before using it.

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
