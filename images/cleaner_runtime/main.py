"""Fixed C entrypoint for restricted Linux containers only. Source is untrusted; the API and Graph Runner must not import or invoke it.


"""

import os
import csv
import json
from pathlib import Path
import runpy
import sys

from backend.domain.spec import FrozenSpec


def main() -> None:
    """Cleaning program entrypoint."""
    if os.environ.get("AI_AGENT_SANDBOX_JOB") != "CLEAN_C":
        raise SystemExit("Stage C may run only in a restricted container created by a trusted worker")
    # The environment flag is not a sandbox boundary; the worker's Docker configuration enforces isolation.
    with Path("/input/spec.json").open("rb") as stream:
        content = stream.read(262145)
    if len(content) > 262144:
        raise ValueError("Specification exceeds the size limit")
    FrozenSpec.model_validate_json(content)
    if Path("/input/clean.py").stat().st_size > 262144:
        raise ValueError("Source exceeds the size limit")
    violation = False

    def audit(event, args):
        """Conservative extra rejection and classification, not a replacement for Docker isolation; swallowing an exception still counts as a violation."""
        nonlocal violation
        denied = event.startswith(("socket.", "subprocess.", "ctypes."))
        denied = denied or event in ("os.system", "os.fork", "os.exec", "os.posix_spawn")
        if event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes)):
                name = os.fsdecode(path)
                if name.startswith(("/input/", "/output/")):
                    writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
                    denied = bool(writing and name != "/output/cleaned.csv")
                elif name.startswith(("/etc/", "/proc/", "/sys/", "/dev/")):
                    denied = True
        if denied:
            violation = True
            raise PermissionError("sandbox operation denied")

    sys.addaudithook(audit)
    category = "completed"
    diagnostic = None
    try:
        runpy.run_path("/input/clean.py", run_name="__main__")
    except (PermissionError, OSError, MemoryError, csv.Error, UnicodeError):
        category = "restricted_or_input_error"
    except (SyntaxError, NameError, TypeError, AttributeError, KeyError, IndexError,
            ValueError, ImportError, RuntimeError, ZeroDivisionError) as exc:
        category = "runtime_error"
        allowed = (NameError, TypeError, AttributeError, KeyError, IndexError,
                   ValueError, ImportError, RuntimeError, ZeroDivisionError)
        if type(exc) in allowed:
            tb = exc.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_filename == "/input/clean.py":
                    diagnostic = {"exception_type": type(exc).__name__, "line": tb.tb_lineno}
                tb = tb.tb_next
            # Exact fixed messages map to an enum; never retain or forward the message.
            if (diagnostic is not None and type(exc) is TypeError
                    and len(exc.args) == 1 and type(exc.args[0]) is str
                    and exc.args[0] in (
                        "Path.open() got an unexpected keyword argument 'strict'",
                        "'strict' is an invalid keyword argument for open()",
                    )):
                diagnostic["reason"] = "OPEN_STRICT_UNSUPPORTED"
    except BaseException:
        category = "unknown_error"
    if violation:
        category = "security_violation"
        diagnostic = None
    # Do not collect exception messages, variables, file contents, or stack traces; the worker must bind this untrusted hint to the source.
    result = {"category": category}
    if diagnostic is not None:
        result["diagnostic"] = diagnostic
    Path("/tmp/runtime.json").write_text(json.dumps(result), encoding="utf-8")
    if category != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
