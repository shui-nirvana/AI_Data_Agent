"""The sandbox diagnostic is an untrusted hint, never a raw traceback."""

import pytest

from backend.llm.diagnostics import source_diagnostic, verified_diagnostic


SOURCE = (
    "def outer():\n"
    "    def inner():\n"
    "        raise TypeError('private input')\n"
    "    inner()\n"
    "outer()\n"
)


def test_source_location_and_nested_function_are_derived_from_ast():
    assert source_diagnostic({"exception_type": "TypeError", "line": 3}, SOURCE) == {
        "exception_type": "TypeError", "line": 3, "function": "inner"
    }
    assert source_diagnostic({"exception_type": "RuntimeError", "line": 5}, SOURCE) == {
        "exception_type": "RuntimeError", "line": 5, "function": "<module>"
    }


@pytest.mark.parametrize("raw", [
    None, "PRIVATE_ROW", {}, {"exception_type": "PRIVATE_ROW", "line": 3},
    {"exception_type": "TypeError", "line": True},
    {"exception_type": "TypeError", "line": 0},
    {"exception_type": "TypeError", "line": 99999},
    {"exception_type": "TypeError", "line": 3, "message": "PRIVATE_ROW"},
    {"exception_type": ["TypeError"], "line": 3},
    {"exception_type": "TypeError", "line": 3, "reason": "PRIVATE_ROW"},
    {"exception_type": "RuntimeError", "line": 3, "reason": "OPEN_STRICT_UNSUPPORTED"},
])
def test_untrusted_or_unbounded_diagnostics_are_discarded(raw):
    assert source_diagnostic(raw, SOURCE) is None


def test_function_name_is_not_accepted_from_container_or_caller():
    valid = {"exception_type": "TypeError", "line": 3, "function": "inner"}
    assert verified_diagnostic(valid, SOURCE) == valid
    assert verified_diagnostic({**valid, "function": "PRIVATE_ROW"}, SOURCE) is None
    assert verified_diagnostic({**valid, "message": "PRIVATE_ROW"}, SOURCE) is None
    assert verified_diagnostic(valid, "def different():\n    pass\n") is None


def test_only_fixed_reason_enum_is_allowed():
    source = "def inner():\n    open('/input/data.csv', strict=True)\ninner()\n"
    diagnostic = {
        "exception_type": "TypeError", "line": 2, "function": "inner",
        "reason": "OPEN_STRICT_UNSUPPORTED",
    }
    assert verified_diagnostic(diagnostic, source) == diagnostic
    assert verified_diagnostic({**diagnostic, "reason": "PRIVATE_ROW"}, source) is None
    assert source_diagnostic({"exception_type": "TypeError", "line": 3,
                              "reason": "OPEN_STRICT_UNSUPPORTED"}, SOURCE) is None


def test_unrepresentable_function_and_invalid_source_do_not_leak():
    assert source_diagnostic(
        {"exception_type": "RuntimeError", "line": 2},
        "def \u9690\u79c1():\n    raise RuntimeError()\n",
    ) is None
    assert source_diagnostic({"exception_type": "RuntimeError", "line": 1}, "def broken(") is None
