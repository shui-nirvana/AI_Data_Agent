"""D container entrypoint. Read originals, specifications, and candidates only from fixed read-only paths and produce a bounded report; do not invoke it in the API process.


"""

import csv
import json
from pathlib import Path

from backend.domain.spec import FrozenSpec
from images.validator.verify import verify


def main() -> None:
    """Validation program entrypoint."""
    output = Path("/output")
    output.mkdir(parents=True, exist_ok=True)
    try:
        with Path("/input/spec.json").open("rb") as stream:
            content = stream.read(262145)
        if len(content) > 262144:
            raise ValueError("Specification exceeds the size limit")
        spec = FrozenSpec.model_validate_json(content)
        report = verify(Path("/input/data.csv"), Path("/input/candidate.csv"), spec)
    except (ValueError, csv.Error, OSError) as exc:
        # Do not report raw exception messages; they may contain user data or host paths.
        report = {"verdict": "rejected", "error_code": "INVALID_INPUT_OR_SPEC",
                  "error_type": type(exc).__name__}
    (output / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    if report["verdict"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
