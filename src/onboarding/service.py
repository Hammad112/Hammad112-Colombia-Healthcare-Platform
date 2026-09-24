"""Turn an uploaded file into staged rows a person can review, then commit them.

The order matters and is not negotiable:

    analyse  -> read the file, propose a mapping, report what needs confirming
    validate -> run the normalizers over EVERY row, write staging rows
    commit   -> apply, in one transaction, only if nothing is invalid

Nothing reaches the clinic's real tables until `commit`, and `commit` refuses
while any row is invalid. That is the milestone's exit criterion: "no import
commits data that fails validation".

`validate` runs over 100% of rows, never a sample, and records what every rule
did to every cell (ADR-08a). The cost is one log row per cell; the benefit is
that a reviewer can answer "why is this patient's phone empty?" without
re-running anything.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from src.core.config import get_settings
from src.onboarding import normalizers as norm
from src.onboarding.canonical import (
    FIELDS_BY_ENTITY,
    Entity,
    Field,
    field_for,
    required_fields,
)
from src.onboarding.matcher import Proposal, SheetMapping, guess_entity, match_sheet
from src.onboarding.reader import ReadResult, Sheet

# A column-level decision the file cannot make for itself. Held on the session
# so the human answers it once, and every row then follows the same rule.
type ColumnDecisions = dict[str, str]


@dataclass(frozen=True, slots=True)
class CellResult:
    """What one rule did to one cell. Written to the transform log."""

    row_number: int
    column: str
    target_field: str | None
    raw: str
    normalized: str | None
    rule: str
    status: norm.Status
    message: str = ""


@dataclass(slots=True)
class RowResult:
    row_number: int
    entity: Entity
    raw: dict[str, str]
    values: dict[str, Any] = field(default_factory=dict)
    cells: list[CellResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)

    @property
    def status(self) -> norm.Status:
        if self.errors:
            return norm.Status.INVALID
        if self.reviews:
            return norm.Status.REVIEW
        return norm.Status.VALID


@dataclass(frozen=True, slots=True)
class ColumnReport:
    """What the confirmation screen shows for one column.

    `percent_valid` is deliberately the headline rather than a preview of the
    first few rows: a preview looks fine while the hundredth row is broken
    (ADR-08a).
    """

    column: str
    target_field: str | None
    confidence: str
    reason: str
    auto: bool
    total: int = 0
    valid: int = 0
    review: int = 0
    invalid: int = 0

    @property
    def percent_valid(self) -> float:
        return 100.0 * self.valid / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class SheetReport:
    sheet: str
    entity: Entity
    entity_reason: str
    columns: tuple[ColumnReport, ...]
    missing_required: tuple[str, ...]
    total_rows: int
    valid_rows: int
    review_rows: int
    invalid_rows: int
    warnings: tuple[str, ...] = ()
    #: Column-level questions only a person can answer, e.g. an ambiguous date
    #: format. The import cannot be validated until each is answered.
    questions: tuple[str, ...] = ()


def analyse(result: ReadResult) -> tuple[SheetReport, ...]:
    """Propose a mapping for every sheet, without converting anything yet."""
    return tuple(_analyse_sheet(sheet) for sheet in result.sheets)


def _ask_model(sheet: Sheet, entity: Entity, mapping: SheetMapping) -> dict[str, ColumnReport]:
    """Ask a model about the columns the deterministic stages could not resolve.

    The PDF's scope includes an LLM-assisted mapping proposal, and the
    architecture places it as a re-ranker over deterministic candidates rather
    than as the mapper. So this runs last, over the residue, and only when a key
    is configured: an ordinary Spanish export reaches it with nothing to ask.

    A failure here is not an error. The column keeps whatever the deterministic
    stages thought and goes to the person confirming the import, which is
    exactly where it would have gone with no provider at all.
    """
    from src.onboarding import llm

    settings = get_settings()
    if not settings.mapping_llm_available:
        return {}

    unresolved = [p for p in mapping.proposals if not p.auto]
    if not unresolved:
        return {}

    taken = {p.field.name for p in mapping.proposals if p.auto and p.field}
    candidates = tuple(f.name for f in FIELDS_BY_ENTITY[entity] if f.name not in taken)
    index = {header: position for position, header in enumerate(sheet.headers)}
    improved: dict[str, ColumnReport] = {}

    for proposal in unresolved:
        values = [row[index[proposal.column]] for row in sheet.rows]
        profile = llm.profile_column(values)
        question = llm.ColumnQuestion(
            header=proposal.column,
            shape=profile.shape,
            filled_percent=profile.filled_percent,
            distinct_count=profile.distinct_count,
            synthetic_examples=profile.examples,
        )
        try:
            suggestion = llm.suggest(question, entity, candidates=candidates)
        except llm.LLMUnavailable:
            continue  # the human decides, as they would have anyway
        if suggestion.target_field is None or suggestion.target_field in taken:
            continue
        taken.add(suggestion.target_field)
        improved[proposal.column] = ColumnReport(
            column=proposal.column,
            target_field=suggestion.target_field,
            # Never pre-ticked. A model's answer is a suggestion for a person to
            # confirm, not a decision: schema adherence is not correctness.
            confidence="suggested",
            reason=f"{suggestion.provider} suggests this: {suggestion.reason}",
            auto=False,
        )
    return improved


def analyse_sheet_for_test(sheet: Sheet) -> SheetReport:
    """Analyse one sheet. Exposed for tests that build a sheet directly."""
    return _analyse_sheet(sheet)


def _analyse_sheet(sheet: Sheet) -> SheetReport:
    entity, reason = guess_entity(sheet.name, sheet.headers)
    mapping = match_sheet(sheet.headers, entity)
    questions = _column_questions(sheet, mapping)
    suggested = _ask_model(sheet, entity, mapping)
    return SheetReport(
        sheet=sheet.name,
        entity=entity,
        entity_reason=reason,
        columns=tuple(suggested.get(p.column) or _column_report(p) for p in mapping.proposals),
        missing_required=mapping.missing_required,
        total_rows=len(sheet.rows),
        valid_rows=0,
        review_rows=0,
        invalid_rows=0,
        warnings=sheet.warnings,
        questions=questions,
    )


def _column_report(proposal: Proposal) -> ColumnReport:
    return ColumnReport(
        column=proposal.column,
        target_field=proposal.field.name if proposal.field else None,
        confidence=proposal.confidence.value,
        reason=proposal.reason,
        auto=proposal.auto,
    )


def _column_questions(sheet: Sheet, mapping: SheetMapping) -> tuple[str, ...]:
    """Ask about anything the column as a whole cannot decide.

    Only date order arises today: a column whose every value is ambiguous is
    either day-first or month-first, and picking one silently would misdate
    every row in it.
    """
    questions: list[str] = []
    index = {header: position for position, header in enumerate(sheet.headers)}
    for proposal in mapping.proposals:
        if proposal.field is None or proposal.field.normalizer != "date":
            continue
        column_values = [row[index[proposal.column]] for row in sheet.rows]
        # Only ask when a value would actually be refused. An ISO column is
        # undecided too, because nothing in it is day-or-month, but every value
        # parses. Asking anyway teaches reviewers to click past questions,
        # which is how the one that matters gets missed.
        ambiguous = any(
            norm.date(value, order=norm.DayFirst.UNDECIDED).status is norm.Status.REVIEW
            for value in column_values
            if value.strip()
        )
        if ambiguous and norm.detect_day_first(column_values) is norm.DayFirst.UNDECIDED:
            questions.append(
                f"{proposal.column}: dates could be day/month or month/day. "
                "Which is it? (day_first or month_first)"
            )
    return tuple(questions)


def validate(
    sheet: Sheet,
    entity: Entity,
    mapping: dict[str, str | None],
    *,
    decisions: ColumnDecisions | None = None,
) -> tuple[list[RowResult], tuple[ColumnReport, ...]]:
    """Run every normalizer over every row of one sheet.

    `mapping` is what the human confirmed: column name to canonical field name,
    or None for a column that is deliberately not imported.
    """
    decisions = decisions or {}
    index = {header: position for position, header in enumerate(sheet.headers)}

    # Date order is decided once per column, from the whole column, before any
    # row is converted. Deciding per row would let one file contain both
    # readings, which is how a birth date silently becomes a different date.
    orders: dict[str, norm.DayFirst] = {}
    for column, target in mapping.items():
        canonical = field_for(entity, target) if target else None
        if canonical and canonical.normalizer == "date":
            answer = decisions.get(column)
            orders[column] = (
                norm.DayFirst(answer)
                if answer in {"day_first", "month_first"}
                else norm.detect_day_first([row[index[column]] for row in sheet.rows])
            )

    rows: list[RowResult] = []
    counts: dict[str, Counter[str]] = {column: Counter() for column in mapping}

    for offset, raw_row in enumerate(sheet.rows, start=sheet.header_row + 1):
        row = RowResult(
            row_number=offset,
            entity=entity,
            raw={header: raw_row[index[header]] for header in sheet.headers},
        )
        for column, target in mapping.items():
            if target is None:
                continue
            canonical = field_for(entity, target)
            if canonical is None:
                continue
            raw_value = raw_row[index[column]]
            outcome = _apply(canonical, raw_value, orders.get(column, norm.DayFirst.UNDECIDED))
            counts[column][outcome.status.value] += 1
            row.cells.append(
                CellResult(
                    row_number=offset,
                    column=column,
                    target_field=target,
                    raw=raw_value,
                    normalized=_text(outcome.value),
                    rule=outcome.rule,
                    status=outcome.status,
                    message=outcome.message,
                )
            )
            if outcome.status is norm.Status.VALID:
                row.values[target] = outcome.value
            elif outcome.status is norm.Status.INVALID:
                row.errors.append(f"{column}: {outcome.message}")
            else:
                row.reviews.append(f"{column}: {outcome.message}")
        rows.append(row)

    # In the file's own column order, and including the columns nobody mapped.
    # Iterating the mapping instead would reorder the screen against the
    # spreadsheet the reviewer is comparing it to, and would hide every unmapped
    # column so they could not be mapped at all.
    reports = tuple(
        ColumnReport(
            column=header,
            target_field=mapping.get(header),
            confidence="confirmed" if mapping.get(header) else "not imported",
            reason=(
                "Confirmed by the reviewer."
                if mapping.get(header)
                else "Deliberately not imported."
            ),
            auto=True,
            total=sum(counts[header].values()) if header in counts else 0,
            valid=counts[header][norm.Status.VALID.value] if header in counts else 0,
            review=counts[header][norm.Status.REVIEW.value] if header in counts else 0,
            invalid=counts[header][norm.Status.INVALID.value] if header in counts else 0,
        )
        for header in sheet.headers
    )
    return rows, reports


def _apply(canonical: Field, raw: str, order: norm.DayFirst) -> norm.Outcome[Any]:
    """Run the one normalizer bound to this field.

    The binding lives in `canonical.py` and is fixed in code. Nothing here
    chooses a transform from the data, which is the whole point of ADR-08a.
    """
    match canonical.normalizer:
        case "document_type":
            return norm.document_type(raw)
        case "document_number":
            return norm.document_number(raw)
        case "phone":
            return norm.phone(raw)
        case "date":
            return norm.date(raw, order=order)
        case "time":
            return norm.time_of_day(raw)
        case "status":
            return norm.appointment_status(raw)
        case "full_name":
            return norm.split_full_name(raw)
        case "boolean":
            return norm.boolean(raw)
        case _:
            # No normalizer: the value is stored as written, trimmed. An empty
            # optional field is not an error.
            text = raw.strip()
            return norm.Outcome(norm.Status.VALID, text or None, "text.as_written")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, norm.SplitName):
        return f"{value.given_names} | {value.family_names}"
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    return str(value)


def summarise(
    rows: list[RowResult], report: SheetReport, columns: tuple[ColumnReport, ...]
) -> SheetReport:
    """Fold row outcomes back into the sheet report the screen renders."""
    tally = Counter(row.status.value for row in rows)
    return SheetReport(
        sheet=report.sheet,
        entity=report.entity,
        entity_reason=report.entity_reason,
        columns=columns,
        missing_required=report.missing_required,
        total_rows=len(rows),
        valid_rows=tally[norm.Status.VALID.value],
        review_rows=tally[norm.Status.REVIEW.value],
        invalid_rows=tally[norm.Status.INVALID.value],
        warnings=report.warnings,
        questions=report.questions,
    )


def _column_to_dict(column: ColumnReport) -> dict[str, Any]:
    return {
        "column": column.column,
        "target_field": column.target_field,
        "confidence": column.confidence,
        "reason": column.reason,
        "auto": column.auto,
        "total": column.total,
        "valid": column.valid,
        "review": column.review,
        "invalid": column.invalid,
    }


def report_to_dict(report: SheetReport) -> dict[str, Any]:
    """Serialise a sheet report for the session's `report` column."""
    return {
        "sheet": report.sheet,
        "entity": report.entity.value,
        "entity_reason": report.entity_reason,
        "columns": [_column_to_dict(c) for c in report.columns],
        "missing_required": list(report.missing_required),
        "total_rows": report.total_rows,
        "valid_rows": report.valid_rows,
        "review_rows": report.review_rows,
        "invalid_rows": report.invalid_rows,
        "warnings": list(report.warnings),
        "questions": list(report.questions),
    }


def report_from_dict(data: dict[str, Any]) -> SheetReport:
    return SheetReport(
        sheet=data["sheet"],
        entity=Entity(data["entity"]),
        entity_reason=data.get("entity_reason", ""),
        columns=tuple(ColumnReport(**column) for column in data.get("columns", ())),
        missing_required=tuple(data.get("missing_required", ())),
        total_rows=data.get("total_rows", 0),
        valid_rows=data.get("valid_rows", 0),
        review_rows=data.get("review_rows", 0),
        invalid_rows=data.get("invalid_rows", 0),
        warnings=tuple(data.get("warnings", ())),
        questions=tuple(data.get("questions", ())),
    )


def apply_profile(report: SheetReport, mapping: dict[str, str | None]) -> SheetReport:
    """Re-state a report under a mapping a person confirmed.

    A confirmed column is no longer a proposal, so its confidence becomes
    "confirmed" and it is pre-ticked. A column the reviewer cleared is shown as
    deliberately not imported, rather than as something the matcher failed on.
    """
    columns = tuple(
        ColumnReport(
            column=c.column,
            target_field=mapping.get(c.column),
            confidence="confirmed" if mapping.get(c.column) else "not imported",
            reason=(
                "Confirmed for this clinic."
                if mapping.get(c.column)
                else "Deliberately not imported."
            ),
            auto=True,
            total=c.total,
            valid=c.valid,
            review=c.review,
            invalid=c.invalid,
        )
        for c in report.columns
    )
    assigned = {target for target in mapping.values() if target}
    missing = tuple(
        name for name in (f.name for f in required_fields(report.entity)) if name not in assigned
    )
    if report.entity is Entity.PATIENT and (
        "full_name" in assigned or {"given_names", "family_names"} <= assigned
    ):
        missing = tuple(m for m in missing if m not in {"full_name", "given_names", "family_names"})
    return SheetReport(
        sheet=report.sheet,
        entity=report.entity,
        entity_reason=report.entity_reason,
        columns=columns,
        missing_required=missing,
        total_rows=report.total_rows,
        valid_rows=report.valid_rows,
        review_rows=report.review_rows,
        invalid_rows=report.invalid_rows,
        warnings=report.warnings,
        questions=report.questions,
    )
