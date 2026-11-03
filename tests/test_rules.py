"""D0/D1 金样本与负例。只测试确定性规则，不代表真实容器隔离已验证。"""

import csv
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.domain.csv_policy import CSV_POLICY
from backend.domain.spec import Action, ActionParams, Decision, FrozenSpec, freeze_spec
from backend.jobs.contracts import JobRequest, Stage
from images.csv_input import read_csv
from images.profiler.main import scan
from images.validator.verify import expected_date, verify


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


@pytest.fixture
def golden(tmp_path):
    """期望结果人工列出，不能调用验证器生成，否则测试形成循环自证。"""
    original = tmp_path / "original.csv"
    candidate = tmp_path / "candidate.csv"
    rows = [
        ["id", "date", "amount"],
        ["1", "2026/10/31", "-3"],
        ["2", "31-10-2026", "5"],
        ["3", "", "0"],
        ["4", "2026/02/30", "8"],
        ["4", "2026/02/30", "8"],
    ]
    expected = [row[:] for row in rows]
    expected[1][1] = "2026-10-31"
    expected[2][1] = "2026-10-31"
    write_csv(original, rows)
    write_csv(candidate, expected)
    profile, issues = scan(original)
    decisions = tuple(
        Decision(
            issue_id=issue["issue_id"], column=issue["column"],
            action=Action.NORMALIZE_DATE if issue["column"] == "date" else Action.PRESERVE,
            predicate_id="parseable_nonstandard_date" if issue["column"] == "date" else "all_cells",
            params=ActionParams(input_formats=("YYYY/MM/DD", "DD-MM-YYYY"))
            if issue["column"] == "date" else ActionParams(),
            actor_id="demo", task_version=1,
        )
        for issue in issues
    )
    arguments = dict(
        task_id=str(uuid4()), input_sha256=profile["input_sha256"], owner_id="demo",
        task_version=1, columns=tuple(profile["columns"]), decisions=decisions,
        issues=tuple(issues),
    )
    return original, candidate, rows, expected, arguments, freeze_spec(**arguments)


def test_golden_exact_dates_and_refund_preservation(golden):
    original, candidate, _, _, _, spec = golden
    report = verify(original, candidate, spec)
    assert report["verdict"] == "passed"
    assert report["changed_cells"] == 2
    assert report["mismatch_count"] == 0
    assert report["output_sha256"] == read_csv(candidate).sha256


@pytest.mark.parametrize("mutation", ["refund", "drop_row", "reorder", "bad_date", "header"])
def test_rejects_unauthorized_candidate(golden, mutation):
    original, candidate, _, expected, _, spec = golden
    if mutation == "refund":
        expected[1][2] = "3"
    elif mutation == "drop_row":
        expected.pop()
    elif mutation == "reorder":
        expected[1], expected[2] = expected[2], expected[1]
    elif mutation == "bad_date":
        expected[4][1] = "2026-03-02"
    else:
        expected[0][2] = "revenue"
    write_csv(candidate, expected)
    assert verify(original, candidate, spec)["verdict"] == "rejected"


def test_spec_is_hash_bound_and_nested_immutable(golden):
    _, _, _, _, arguments, spec = golden
    assert freeze_spec(**arguments).spec_sha256 == spec.spec_sha256
    assert FrozenSpec.model_validate_json(spec.model_dump_json()) == spec
    payload = spec.model_dump(mode="json")
    payload["columns"][0] = "renamed"
    with pytest.raises(ValidationError):
        FrozenSpec.model_validate(payload)
    with pytest.raises(ValidationError):
        spec.decisions[0].params.literal = "tampered"


def test_requires_complete_current_owner_decisions(golden):
    _, _, _, _, arguments, _ = golden
    with pytest.raises(ValueError, match="完整裁决"):
        freeze_spec(**{**arguments, "decisions": arguments["decisions"][:-1]})
    with pytest.raises(ValidationError, match="身份或任务版本"):
        freeze_spec(**{**arguments, "owner_id": "another-owner"})
    with pytest.raises(ValidationError, match="身份或任务版本"):
        freeze_spec(**{**arguments, "task_version": 2})
    with pytest.raises(ValueError, match="输入哈希"):
        freeze_spec(**{**arguments, "input_sha256": "b" * 64})


def test_literal_fill_only_empty_not_whitespace_or_na(tmp_path):
    original, candidate = tmp_path / "in.csv", tmp_path / "out.csv"
    rows = [["id", "label"], ["1", ""], ["2", " "], ["3", "NA"]]
    write_csv(original, rows)
    expected = [row[:] for row in rows]
    expected[1][1] = "unknown"
    write_csv(candidate, expected)
    profile, issues = scan(original)
    decision = Decision(
        issue_id=issues[0]["issue_id"], column="label", action=Action.FILL_NULL_WITH_LITERAL,
        predicate_id="empty_string", params=ActionParams(literal="unknown"),
        actor_id="demo", task_version=1,
    )
    spec = freeze_spec(
        task_id=str(uuid4()), input_sha256=profile["input_sha256"], owner_id="demo",
        task_version=1, columns=tuple(profile["columns"]), decisions=(decision,),
        issues=tuple(issues),
    )
    assert verify(original, candidate, spec)["verdict"] == "passed"
    expected[2][1] = "unknown"
    write_csv(candidate, expected)
    assert verify(original, candidate, spec)["verdict"] == "rejected"


@pytest.mark.parametrize("formats", [(), ("MM/DD/YYYY",), ("YYYY/MM/DD", "YYYY/MM/DD")])
def test_rejects_unknown_or_ambiguous_format_policy(formats):
    with pytest.raises(ValidationError):
        Decision(
            issue_id="date", column="date", action=Action.NORMALIZE_DATE,
            predicate_id="parseable_nonstandard_date", params=ActionParams(input_formats=formats),
            actor_id="demo", task_version=1,
        )


def test_no_heuristic_date_parsing():
    assert expected_date("01/02/2026", ("YYYY/MM/DD",)) == "01/02/2026"
    assert expected_date("2026/02/30", ("YYYY/MM/DD",)) == "2026/02/30"
    assert expected_date("2024/02/29", ("YYYY/MM/DD",)) == "2024-02-29"
    assert expected_date("2026/1/2", ("YYYY/MM/DD",)) == "2026/1/2"


def test_rejects_all_columns_written_and_unknown_action(tmp_path):
    source = tmp_path / "single.csv"
    write_csv(source, [["date"], ["2026/10/31"]])
    profile, issues = scan(source)
    decision = Decision(
        issue_id=issues[0]["issue_id"], column="date", action=Action.NORMALIZE_DATE,
        predicate_id="parseable_nonstandard_date",
        params=ActionParams(input_formats=("YYYY/MM/DD",)), actor_id="demo", task_version=1,
    )
    with pytest.raises(ValidationError, match="保留一个"):
        freeze_spec(
            task_id=str(uuid4()), input_sha256=profile["input_sha256"], owner_id="demo",
            task_version=1, columns=("date",), decisions=(decision,), issues=tuple(issues),
        )
    with pytest.raises(ValidationError):
        Decision.model_validate({**decision.model_dump(), "action": "drop_duplicates"})


def test_bounded_parser_and_bom(tmp_path):
    source = tmp_path / "input.csv"
    source.write_bytes(b"\xef\xbb\xbfid,name\r\n1,\"a\r\nb\"\r\n")
    table = read_csv(source)
    assert table.columns == ("id", "name")
    assert table.rows == (("1", "a\r\nb"),)
    with pytest.raises(ValueError, match="字节"):
        read_csv(source, CSV_POLICY.model_copy(update={"max_bytes": 1}))
    with pytest.raises(ValueError, match="单元格"):
        read_csv(source, CSV_POLICY.model_copy(update={"max_cells": 1}))


@pytest.mark.parametrize("body", [b"a,a\n1,2\n", b"a,\n1,2\n", b"a\n\xff\n", b"a,b\n1\n"])
def test_rejects_bad_csv_without_silent_skips(tmp_path, body):
    source = tmp_path / "input.csv"
    source.write_bytes(body)
    with pytest.raises((ValueError, UnicodeError)):
        read_csv(source)


def test_profiler_full_registry_and_single_column_cards(golden):
    original, _, _, _, _, _ = golden
    first, issues = scan(original)
    second, repeat = scan(original)
    assert first == second and issues == repeat
    assert len(first["tool_runs"]) == 5
    assert first["numeric_by_column"]["amount"]["negative_count"] == 1
    assert first["duplicate_rows"] == 1
    assert len([issue for issue in issues if issue["column"] == "date"]) == 1
    assert all(run["input_sha256"] == first["input_sha256"] for run in first["tool_runs"])


def test_rejects_nonhex_job_hash():
    with pytest.raises(ValueError, match="SHA-256"):
        JobRequest(str(uuid4()), str(uuid4()), Stage.PROFILE_A, "z" * 64)


def test_rejects_replaced_original(golden):
    original, candidate, rows, _, _, spec = golden
    rows[1][2] = "999"
    write_csv(original, rows)
    report = verify(original, candidate, spec)
    assert report["verdict"] == "rejected"
    assert report["checks"][0] == {"id": "input_hash", "passed": False}


def test_zero_change_requires_identical_bytes(golden):
    original, candidate, rows, _, arguments, _ = golden
    decisions = tuple(
        Decision(issue_id=d.issue_id, column=d.column, action=Action.PRESERVE,
                 predicate_id="all_cells", actor_id="demo", task_version=1)
        for d in arguments["decisions"]
    )
    spec = freeze_spec(**{**arguments, "decisions": decisions})
    candidate.write_bytes(original.read_bytes())
    assert verify(original, candidate, spec)["verdict"] == "passed"
    # 相同单元格但换行/序列化变更：无修改任务不能伪装成字节一致。
    with candidate.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)
    assert verify(original, candidate, spec)["verdict"] == "rejected"
