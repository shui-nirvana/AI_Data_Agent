"""Approval contract: a frozen specification cannot be created directly from unconstrained model output.


"""

from enum import StrEnum
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain.csv_policy import DATE_FORMATS, PARSER_VERSION


class Action(StrEnum):
    """Supported atomic cleaning actions."""
    PRESERVE = "preserve"                       # Preserve unchanged.
    NORMALIZE_DATE = "normalize_date"           # Normalize date format.
    FILL_NULL_WITH_LITERAL = "fill_null_with_literal" # Fill empty values with a literal.


class ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ActionParams(ImmutableModel):
    """Avoid mutable parameter dictionaries so nested objects cannot alter a frozen record."""

    input_formats: tuple[str, ...] = ()
    output_format: Literal["YYYY-MM-DD"] = "YYYY-MM-DD"
    on_unparseable: Literal["preserve"] = "preserve"
    literal: str | None = Field(default=None, max_length=1024)


class Decision(ImmutableModel):
    """An approved single-column action; the API approval transaction verifies identity and version."""

    issue_id: str = Field(min_length=1, max_length=100)
    action: Action
    column: str = Field(min_length=1, max_length=32768)
    predicate_id: str
    params: ActionParams = ActionParams()
    actor_id: str = Field(min_length=1, max_length=100)
    task_version: int = Field(ge=1)
    rule_version: Literal["1.0"] = "1.0"

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == Action.NORMALIZE_DATE:
            if self.predicate_id != "parseable_nonstandard_date":
                raise ValueError("日期动作必须使用已登记谓词")
            formats = self.params.input_formats
            if not formats or len(set(formats)) != len(formats) or any(
                item not in DATE_FORMATS for item in formats
            ) or self.params.literal is not None:
                raise ValueError("日期输入格式必须明确、唯一且属于白名单")
        elif self.action == Action.FILL_NULL_WITH_LITERAL:
            if self.predicate_id != "empty_string" or self.params.literal is None:
                raise ValueError("空值动作必须使用空字符串谓词及明确字面值")
            if self.params.input_formats:
                raise ValueError("空值动作不能携带日期参数")
            if self.params.literal.startswith(("=", "+", "-", "@")):
                raise ValueError("MVP 不新增可能触发电子表格公式的填充值")
        else:
            if self.predicate_id != "all_cells" or self.params != ActionParams():
                raise ValueError("保留动作只支持整列保护，不接受转换参数")
        return self


class FrozenSpec(ImmutableModel):
    """A specification hash is not a signature; D must receive the specification from a trusted read-only control-plane mount."""

    schema_version: Literal["1.0"] = "1.0"
    parser_version: Literal["csv-utf8-v1"] = PARSER_VERSION
    task_id: UUID
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_id: str = Field(min_length=1)
    task_version: int = Field(ge=1)
    columns: tuple[str, ...] = Field(min_length=1, max_length=100)
    decisions: tuple[Decision, ...] = Field(max_length=100)
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verify_integrity(self):
        if not self.columns or len(set(self.columns)) != len(self.columns) or any(
            not column.strip() for column in self.columns
        ):
            raise ValueError("列必须非空且唯一")
        ids, columns = set(), set()
        for decision in self.decisions:
            if decision.issue_id in ids or decision.column in columns:
                raise ValueError("重复问题或同列动作冲突")
            ids.add(decision.issue_id)
            columns.add(decision.column)
            if decision.column not in self.columns:
                raise ValueError("决策引用未知列")
            if decision.actor_id != self.owner_id or decision.task_version != self.task_version:
                raise ValueError("审批身份或任务版本不匹配")
        written = {d.column for d in self.decisions if d.action != Action.PRESERVE}
        if written == set(self.columns):
            raise ValueError("MVP 必须至少保留一个未修改列以核对行对应关系")
        if self.spec_sha256 != specification_hash(self.model_dump(mode="json")):
            raise ValueError("规范哈希不匹配")
        return self


def specification_hash(payload: dict) -> str:
    """Hash stable serialization independent of JSON whitespace and dictionary insertion order."""
    content = {key: value for key, value in payload.items() if key != "spec_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def freeze_spec(
    *, task_id: str, input_sha256: str, owner_id: str, task_version: int,
    columns: tuple[str, ...], decisions: tuple[Decision, ...],
    issues: tuple[dict, ...],
) -> FrozenSpec:
    """Build a specification without reading CSV or persisting approval. Callers must use verified A findings; the API must still perform authorization and its approval/outbox transaction.



    """
    by_id = {issue["issue_id"]: issue for issue in issues}
    if len(by_id) != len(issues) or {d.issue_id for d in decisions} != set(by_id):
        raise ValueError("必须完整裁决本任务全部问题，且问题 ID 唯一")
    for decision in decisions:
        issue = by_id[decision.issue_id]
        if (issue.get("source") != "fixed" or issue.get("column") != decision.column
                or issue.get("input_sha256") != input_sha256):
            raise ValueError("发现来源、输入哈希或目标列不匹配")
        if decision.action.value not in issue.get("actions", ["preserve"]):
            raise ValueError("该发现不支持所选动作")
    payload = {
        "schema_version": "1.0", "parser_version": PARSER_VERSION,
        "task_id": str(UUID(task_id)), "input_sha256": input_sha256,
        "owner_id": owner_id, "task_version": task_version,
        "columns": list(columns),
        "decisions": [decision.model_dump(mode="json") for decision in decisions],
    }
    payload["spec_sha256"] = specification_hash(payload)
    return FrozenSpec.model_validate(payload)
