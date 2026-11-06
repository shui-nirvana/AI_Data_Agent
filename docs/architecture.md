# Architecture

The API, LangGraph Runner, and Docker queue worker are separate processes using a shared local data directory and SQLite. The API neither runs generated code nor loads Docker. The Runner coordinates durable work and pauses for approval using LangGraph `interrupt()`; the worker alone manages containers. A durable outbox links approved decisions and workflow resumption.

## Trust Boundaries

- A runs fixed profiling tools against a read-only CSV snapshot. Its findings are not permission to change data.
- The user approves a versioned, validated specification. The system binds this specification to the task and input.
- C executes model-generated source in a fresh isolated container. The original model response and the source actually executed are separately recorded and hashed. Repair uses the failed attempt's actual execution snapshot, which can differ from the original response.
- D independently reads the original and candidate and checks approved cell-level transformations and protected cells. D rejects do not trigger a change of rules or bypass verification. In the all-preserve case D checks byte equality; the corresponding check is conditional when changes are approved.
- Publication rechecks relevant bindings and hashes, registers artifacts, and only then marks the task successful. Downloads verify artifact integrity again.

For eligible implementation failures, generation and repair share a maximum of three model calls per task and at most three C attempts. Security and resource failures and D rejection are not bypassed by repair. Diagnostics are restricted; raw exception messages and stacks are not sent as separate data.

The optional D explanation sends allowlisted verification facts to a model that chooses ordering and a `plain` or `brief` style. The application renders factual text itself; explanation failure does not alter the verdict, publication, or download rights.

The current CSV contract is UTF-8 with optional BOM, comma-separated records with consistent column counts, and nonempty unique headers. Missing means an empty string, not whitespace or `NA`. Dates use explicitly selected supported formats. Rows are not deleted, deduplicated, or reordered. D verifies conformance to approved rules, not the business correctness of those rules.
