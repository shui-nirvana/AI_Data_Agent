# Architecture

The API, LangGraph Runner, and Docker queue worker are separate processes using a shared local data directory and SQLite. The API neither runs generated code nor manages Docker containers. The Runner coordinates durable work and pauses for approval using LangGraph `interrupt()`; the worker alone manages containers. A durable outbox links approved decisions and workflow resumption.

## Workflow

1. The API streams the uploaded CSV into private storage and records its hash. Uploading does not start profiling.
2. A worker runs fixed profiling (A) in a container. The profiler checks CSV structure, empty cells, duplicate rows, supported date formats and numeric ranges. Duplicate rows and numeric observations do not authorize deletion or numeric cleanup.
3. The user approves one action for each finding card. The server validates and freezes the specification with the input hash, task version and owner; the business goal remains a description and does not become a rule.
4. If a change is approved, the Runner requests code and the worker executes it in a new C container. If all columns are preserved, generation and C are skipped; the input becomes D's candidate.
5. D independently compares the original and candidate against the frozen specification. Publication verifies the relevant bindings and registers the output, validation report and manifest before allowing downloads.

The current actions preserve a column, normalize selected date formats to `YYYY-MM-DD`, or fill exactly empty strings with a literal. A column has at most one action, and at least one column must remain unchanged to check row correspondence. An empty fill literal is valid but produces no change to empty cells.

Findings for the same column are combined into one card, whose available actions depend on the findings. Negative numbers can only be preserved; duplicate rows are dataset-level observations and cannot be removed. Columns without a finding remain unchanged. The profiler does not identify malformed email addresses or flag invalid dates as date issues.

## Trust Boundaries

- A runs fixed profiling tools against a read-only CSV snapshot. Its findings are not permission to change data.
- The user approves a versioned, validated specification. The system binds this specification to the task and input.
- C executes model-generated source in a fresh isolated container. The original model response and the source actually executed are separately recorded and hashed. Repair uses the failed attempt's actual execution snapshot, which can differ from the original response.
- D independently reads the original and candidate and checks approved cell-level transformations and protected cells. D rejects do not trigger a change of rules or bypass verification. In the all-preserve case D checks byte equality; the corresponding check is conditional when changes are approved.
- Publication rechecks relevant bindings and hashes, registers artifacts, and only then marks the task successful. Downloads verify artifact integrity again.

For eligible implementation failures, generation and repair share a maximum of three code-generation model calls per task and at most three C attempts. Some calls can be spent correcting invalid model responses before execution. A repair request contains the hash-verified source snapshot associated with the failed job, not an assumption that it matches the original model response. Limited diagnostics can include exception type, source line and function when available. Security and resource failures and D rejection are not bypassed by repair; raw exception messages and stacks are not sent as separate data.

The optional D explanation is a separate on-demand model call. It sends allowlisted verification facts to a model that chooses ordering and a `plain` or `brief` style. The application validates that response and renders factual text itself; explanation failure does not alter the verdict, publication, or download rights.

## CSV Contract

The current CSV contract is UTF-8 with optional BOM, comma-separated records with consistent column counts, and nonempty unique headers. The parser limits input to 10 MiB, 100,000 rows, 100 columns, 32,768 characters per field and 1,000,000 cells. The API has a separate configurable upload limit.

Missing means an empty string, not whitespace or `NA`. Supported date inputs are `YYYY-MM-DD`, `YYYY/MM/DD`, and `DD-MM-YYYY`; only explicitly selected, valid dates are converted, and unparseable values are preserved. Rows are not deleted, deduplicated, or reordered. D verifies conformance to approved rules, not the business correctness of those rules or the absence of other data issues.
