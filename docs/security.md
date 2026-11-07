# Security And Privacy

This project is a controlled single-machine demonstration, not a hardened public service. Do not upload sensitive information. Everyone with the shared demo token has access to the same owner; there is no multi-user isolation. A temporary HTTPS deployment has been exercised with selected unauthenticated task requests rejected, but this is not a comprehensive security assessment.

## Execution

Jobs use a fresh Docker container with networking disabled, a non-root user, read-only root and input, dropped capabilities, `no-new-privileges`, and CPU, memory, PID, temporary storage and wall-clock limits. Builds can use the network; job execution cannot. Docker shares the host kernel and does not guarantee protection against kernel compromise.

The worker applies fixed per-stage policies, not model-selected limits:

| Stage | Memory | CPU budget | PIDs | Output tmpfs | Wall timeout |
| --- | ---: | ---: | ---: | ---: | ---: |
| Profiling A | 256 MiB | 30 seconds | 32 | 1,000,000 bytes | 45 seconds |
| Cleaning C | 512 MiB | 45 seconds | 32 | 12,000,000 bytes | 60 seconds |
| Verification D | 256 MiB | 30 seconds | 32 | 1,000,000 bytes | 45 seconds |

The worker also configures a one-CPU quota. These are configured policies; the scope and limitations of observed VM checks are documented in [testing](testing.md).

An independent watchdog monitors each attempt and reconciles containers by exact identity after the deadline, even if the worker exits. A fixed container lifetime provides an additional bound. Neither can guarantee timely cleanup when the host, Docker, watchdog, or filesystem is unavailable. An uncertain create response is not resolved by one empty listing; the worker stops consuming new work until an operator follows the recovery procedure in [operations](operations.md).

## External Model Data

With explicit consent, initial generation sends anonymous column indices, approved rule summaries and aggregate statistics. Repair additionally sends the full source actually executed in the failed attempt and limited diagnostics. Raw CSV rows, actual column names, fill literals, and raw exception messages and stacks are not intentionally sent as independent data. However, the executed source itself may contain sensitive information, which would be transmitted with it.

An optional validation explanation sends only allowlisted D verdict, check states and completed cell statistics. The model chooses presentation order and style; the application renders the verified facts. The explanation cannot change D's decision or publication and is a separate potentially billable call. Keys belong in the Runner environment or ignored local key files, never in Git, screenshots, logs, or containers. Proxy use is opt-in; a trusted HTTPS-intercepting proxy may see requests and credentials.

## Operational Gaps

The default upload limit is 10 MiB, with an active-task cap and minimum free-disk check. An optional Nginx template adds per-IP request and connection limits when explicitly installed; the application itself has no HTTP rate limiting. These limits are not account-wide quotas and can affect users sharing an IP address. The system limits sandbox resources and model calls, but has no account-wide monetary spending cap. Operators must monitor provider billing and disk capacity. A public deployment would also need real authentication, network controls and separate security review.

For a temporary online demo, share the token privately only with trusted users. HTTPS protects transport but does not provide separate identities, prevent authorized users from accessing shared tasks, or cap model spending. The launcher generates a new token on each restart. Stop exposing the demo when it is no longer needed, and keep certificates, private keys and tokens outside the repository.

The UI may show business goals, actual column names and value ranges. Review screen sharing and screenshots. CSV exports can retain spreadsheet formulas; review them before opening in spreadsheet software. Backups, runtime files, test evidence, and model responses may contain sensitive material and must be protected.
