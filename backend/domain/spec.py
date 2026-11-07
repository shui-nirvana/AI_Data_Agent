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
                raise ValueError("The date action must use the registered predicate")
            formats = self.params.input_formats
            if not formats or len(set(formats)) != len(formats) or any(
                item not in DATE_FORMATS for item in formats
            ) or self.params.literal is not None:
                raise ValueError("Date input formats must be specified, unique, and allowed")
        elif self.action == Action.FILL_NULL_WITH_LITERAL:
            if self.predicate_id != "empty_string" or self.params.literal is None:
                raise ValueError("The fill action requires the empty-string predicate and a literal value")
            if self.params.input_formats:
                raise ValueError("The fill action cannot include date parameters")
            if self.params.literal.startswith(("=", "+", "-", "@")):
                raise ValueError("Fill values that could trigger spreadsheet formulas are not allowed")
        else:
            if self.predicate_id != "all_cells" or self.params != ActionParams():
                raise ValueError("The preserve action protects the entire column and accepts no conversion parameters")
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
            raise ValueError("Column names must be nonempty and unique")
        ids, columns = set(), set()
        for decision in self.decisions:
            if decision.issue_id in ids or decision.column in columns:
                raise ValueError("Duplicate issue or conflicting actions for the same column")
            ids.add(decision.issue_id)
            columns.add(decision.column)
            if decision.column not in self.columns:
                raise ValueError("Decision references an unknown column")
            if decision.actor_id != self.owner_id or decision.task_version != self.task_version:
                raise ValueError("Approval identity or task version does not match")
        written = {d.column for d in self.decisions if d.action != Action.PRESERVE}
        if written == set(self.columns):
            raise ValueError("At least one unchanged column must be preserved to check row correspondence")
        if self.spec_sha256 != specification_hash(self.model_dump(mode="json")):
            raise ValueError("Specification hash does not match")
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
        raise ValueError("All task issues must be decided and issue IDs must be unique")
    for decision in decisions:
        issue = by_id[decision.issue_id]
        if (issue.get("source") != "fixed" or issue.get("column") != decision.column
                or issue.get("input_sha256") != input_sha256):
            raise ValueError("Finding source, input hash, or target column does not match")
        if decision.action.value not in issue.get("actions", ["preserve"]):
            raise ValueError("The finding does not support the selected action")
    payload = {
        "schema_version": "1.0", "parser_version": PARSER_VERSION,
        "task_id": str(UUID(task_id)), "input_sha256": input_sha256,
        "owner_id": owner_id, "task_version": task_version,
        "columns": list(columns),
        "decisions": [decision.model_dump(mode="json") for decision in decisions],
    }
    payload["spec_sha256"] = specification_hash(payload)
    return FrozenSpec.model_validate(payload)
