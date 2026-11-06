"""Fixed in-container job entrypoint; Docker exec_inspect supplies the exit code."""

import os
import runpy
import sys

MODULES = {
    "PROFILE_A": "images.profiler.main",
    "CLEAN_C": "images.cleaner_runtime.main",
    "VERIFY_D": "images.validator.main",
}


def main() -> None:
    module = MODULES.get(sys.argv[1] if len(sys.argv) == 2 else "")
    if module is None:
        raise SystemExit(2)
    # Keep logs in bounded /tmp rather than the Docker log driver; the worker collects only truncated summaries.
    for descriptor, name in ((1, "stdout.log"), (2, "stderr.log")):
        fd = os.open(f"/tmp/{name}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(fd, descriptor)
        os.close(fd)
    runpy.run_module(module, run_name="__main__")


if __name__ == "__main__":
    main()
