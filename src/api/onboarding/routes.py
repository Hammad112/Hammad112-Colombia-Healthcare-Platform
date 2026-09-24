"""Upload a clinic file, confirm what its columns mean, then commit it.

Drive this from Swagger UI at /docs. The order is deliberate and enforced:

    POST /onboarding/uploads                  read the file, propose a mapping
    GET  /onboarding/uploads/{id}             what was proposed, and why
    PUT  /onboarding/uploads/{id}/mapping     the reviewer's corrections
    POST /onboarding/uploads/{id}/validate    run every rule over every row
    GET  /onboarding/uploads/{id}/rows        the staged rows and their errors
    POST /onboarding/uploads/{id}/commit      apply, only if nothing is invalid

Sessions live in memory for now, not in the database. That is a deliberate
limit of this checkpoint: the staging tables exist (migration 0005) but nothing
writes to them yet, so a restart loses an in-progress import. Committing to the
clinic's real tables is likewise not wired up; `commit` reports what it would
write and refuses whenever anything is invalid, which is the guarantee that
needs proving first.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from src.api.dependencies import ClinicScopeDep
from src.api.onboarding.schemas import (
    CellOut,
    CommitOut,
    MappingIn,
    RowOut,
    SheetOut,
    UploadOut,
    ValidationOut,
)
from src.onboarding import service
from src.onboarding.canonical import Entity
from src.onboarding.reader import ReadResult, UnreadableFile, read

router = APIRouter()

# Uploads are spooled to disk and parsed, so this bounds what a single request
# can cost. The archive guards in reader.py bound what it can expand to.
MAX_UPLOAD_BYTES: Final = 50 * 1024 * 1024


@dataclass(slots=True)
class _Session:
    """One import in progress. In memory until the staging tables are wired up."""

    id: uuid.UUID
    clinic_id: uuid.UUID
    filename: str
    file_sha256: str
    result: ReadResult
    reports: tuple[service.SheetReport, ...]
    status: str = "analyzed"
    #: sheet name -> confirmed column mapping
    mappings: dict[str, dict[str, str | None]] = field(default_factory=dict)
    #: sheet name -> answers to that sheet's questions
    decisions: dict[str, dict[str, str]] = field(default_factory=dict)
    rows: dict[str, list[service.RowResult]] = field(default_factory=dict)
    #: Why this import may not be committed. Written by `validate`, read by
    #: `commit`, so the two can never disagree about whether it is safe.
    blocking: list[str] = field(default_factory=list)


_SESSIONS: dict[uuid.UUID, _Session] = {}


def _session(session_id: uuid.UUID, clinic_id: uuid.UUID) -> _Session:
    found = _SESSIONS.get(session_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such import session.")
    if found.clinic_id != clinic_id:
        # Not 403: revealing that the session exists would leak one clinic's
        # activity to another.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such import session.")
    return found


def _default_mapping(report: service.SheetReport) -> dict[str, str | None]:
    """What the reviewer sees pre-ticked: confident proposals only."""
    return {c.column: (c.target_field if c.auto else None) for c in report.columns}


@router.post(
    "/uploads",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a clinic spreadsheet and get a proposed mapping",
)
async def upload(
    scope: ClinicScopeDep,
    file: Annotated[UploadFile, File(description="An .xlsx or .csv export from the clinic.")],
) -> UploadOut:
    """Read the file and propose what each column means. Nothing is converted yet."""
    target = Path(tempfile.mkdtemp()) / (file.filename or "upload")
    digest = hashlib.sha256()
    written = 0
    try:
        with target.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                    )
                digest.update(chunk)
                handle.write(chunk)

        try:
            result = read(target)
        except UnreadableFile as error:
            # The reader's refusals are written for the person who uploaded the
            # file, so they are passed through rather than replaced.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

        reports = service.analyse(result)
        session = _Session(
            id=uuid.uuid4(),
            clinic_id=scope.clinic_id,
            filename=file.filename or target.name,
            file_sha256=digest.hexdigest(),
            result=result,
            reports=reports,
        )
        session.mappings = {r.sheet: _default_mapping(r) for r in reports}
        _SESSIONS[session.id] = session

        return UploadOut(
            session_id=session.id,
            filename=session.filename,
            file_sha256=session.file_sha256,
            encoding=result.encoding,
            delimiter=result.delimiter,
            sheets=[SheetOut.build(r) for r in reports],
            status=session.status,
        )
    finally:
        shutil.rmtree(target.parent, ignore_errors=True)


@router.get(
    "/uploads/{session_id}",
    response_model=UploadOut,
    summary="What was proposed for this file, and why",
)
async def get_upload(session_id: uuid.UUID, scope: ClinicScopeDep) -> UploadOut:
    session = _session(session_id, scope.clinic_id)
    return UploadOut(
        session_id=session.id,
        filename=session.filename,
        file_sha256=session.file_sha256,
        encoding=session.result.encoding,
        delimiter=session.result.delimiter,
        sheets=[SheetOut.build(r) for r in session.reports],
        status=session.status,
    )


@router.put(
    "/uploads/{session_id}/mapping",
    response_model=UploadOut,
    summary="Correct the proposed mapping for one sheet",
)
async def set_mapping(session_id: uuid.UUID, scope: ClinicScopeDep, body: MappingIn) -> UploadOut:
    """Replace a sheet's mapping with what the reviewer confirmed.

    A field may be mapped from at most one column: two columns writing one field
    would mean one of them silently wins.
    """
    session = _session(session_id, scope.clinic_id)
    report = next((r for r in session.reports if r.sheet == body.sheet), None)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No sheet named {body.sheet!r}.")

    known = {c.column for c in report.columns}
    unknown = set(body.mapping) - known
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"These columns are not in the sheet: {sorted(unknown)}.",
        )

    assigned = [target for target in body.mapping.values() if target]
    duplicated = {t for t in assigned if assigned.count(t) > 1}
    if duplicated:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"More than one column maps to {sorted(duplicated)}; one would overwrite the other.",
        )

    session.mappings[body.sheet] = dict(body.mapping)
    session.decisions[body.sheet] = dict(body.decisions)
    session.status = "mapped"
    return await get_upload(session_id, scope)


@router.post(
    "/uploads/{session_id}/validate",
    response_model=ValidationOut,
    summary="Run every rule over every row and stage the results",
)
async def validate(session_id: uuid.UUID, scope: ClinicScopeDep) -> ValidationOut:
    """Convert all rows, count what converted, and say whether it may be committed.

    Runs over 100% of rows, never a sample (ADR-08a): a sample that looks fine
    is exactly how a bad transform reaches production.
    """
    session = _session(session_id, scope.clinic_id)

    summaries: list[service.SheetReport] = []
    blocking: list[str] = []
    for report in session.reports:
        sheet = next(s for s in session.result.sheets if s.name == report.sheet)
        mapping = session.mappings.get(report.sheet, {})
        if not any(mapping.values()):
            continue  # nothing confirmed for this sheet, so nothing to import

        rows, columns = service.validate(
            sheet,
            report.entity,
            mapping,
            decisions=session.decisions.get(report.sheet),
        )
        session.rows[report.sheet] = rows
        summary = service.summarise(rows, report, columns)
        summaries.append(summary)

        if summary.invalid_rows:
            blocking.append(
                f"{summary.sheet}: {summary.invalid_rows} row(s) could not be converted."
            )
        if summary.missing_required:
            blocking.append(
                f"{summary.sheet}: no column was mapped to {list(summary.missing_required)}."
            )
        if summary.questions:
            blocking.append(f"{summary.sheet}: {len(summary.questions)} question(s) unanswered.")

    session.blocking = blocking
    session.status = "validated" if not blocking else "needs_review"
    return ValidationOut(
        session_id=session.id,
        status=session.status,
        sheets=[SheetOut.build(s) for s in summaries],
        can_commit=not blocking,
        blocking=blocking,
    )


@router.get(
    "/uploads/{session_id}/rows",
    response_model=list[RowOut],
    summary="The staged rows, so a reviewer can see what would be written",
)
async def get_rows(
    session_id: uuid.UUID,
    scope: ClinicScopeDep,
    sheet: Annotated[str, Query(description="Which sheet's rows to show.")],
    row_status: Annotated[
        str | None, Query(alias="status", description="valid, review or invalid.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[RowOut]:
    session = _session(session_id, scope.clinic_id)
    rows = session.rows.get(sheet)
    if rows is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Validate the import before reading its rows."
        )
    selected = [r for r in rows if row_status is None or r.status.value == row_status]
    return [
        RowOut(
            row_number=r.row_number,
            status=r.status.value,
            values={k: str(v) for k, v in r.values.items()},
            errors=r.errors,
            reviews=r.reviews,
        )
        for r in selected[:limit]
    ]


@router.get(
    "/uploads/{session_id}/transform-log",
    response_model=list[CellOut],
    summary="What every rule did to every cell (ADR-08a)",
)
async def transform_log(
    session_id: uuid.UUID,
    scope: ClinicScopeDep,
    sheet: Annotated[str, Query(description="Which sheet's cells to show.")],
    cell_status: Annotated[
        str | None, Query(alias="status", description="valid, review or invalid.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> list[CellOut]:
    """The evidence that no transform was inferred from the data.

    Every cell carries the named rule that produced its value, so a reviewer can
    ask why a value became what it became without re-running the import.
    """
    session = _session(session_id, scope.clinic_id)
    rows = session.rows.get(sheet)
    if rows is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Validate the import before reading its transform log."
        )
    cells = [
        CellOut(
            row_number=c.row_number,
            column=c.column,
            target_field=c.target_field,
            raw=c.raw,
            normalized=c.normalized,
            rule=c.rule,
            status=c.status.value,
            message=c.message,
        )
        for row in rows
        for c in row.cells
        if cell_status is None or c.status.value == cell_status
    ]
    return cells[:limit]


@router.post(
    "/uploads/{session_id}/commit",
    response_model=CommitOut,
    summary="Apply the import, if and only if nothing is invalid",
)
async def commit(session_id: uuid.UUID, scope: ClinicScopeDep) -> CommitOut:
    """Refuse while anything is invalid; otherwise report what would be written.

    Writing to the clinic's tables is not wired up in this checkpoint. The
    refusal is, because that is the guarantee the milestone turns on: no import
    commits data that fails validation.
    """
    session = _session(session_id, scope.clinic_id)
    if not session.rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "Validate the import before committing it.")

    # Whatever stopped validation stops the commit. Recomputing a narrower
    # check here is how the two drift apart: an earlier version counted invalid
    # rows only, so a sheet blocked on an unanswered date-format question
    # committed anyway.
    if session.blocking:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Refusing to commit. " + " ".join(session.blocking),
        )

    committed = {
        sheet: sum(1 for r in rows if r.status.value == "valid")
        for sheet, rows in session.rows.items()
    }
    skipped = {
        sheet: sum(1 for r in rows if r.status.value == "review")
        for sheet, rows in session.rows.items()
    }
    session.status = "ready_to_commit"
    return CommitOut(
        session_id=session.id,
        status=session.status,
        committed=committed,
        skipped=skipped,
        message=(
            "Validation passed. Writing to the clinic tables is not enabled in this "
            "checkpoint, so nothing was stored; the counts show what would be."
        ),
    )


@router.get(
    "/canonical-fields",
    summary="The fields a column can be mapped to",
)
async def canonical_fields() -> dict[str, list[dict[str, object]]]:
    """What the confirmation screen offers in its dropdowns."""
    from src.onboarding.canonical import FIELDS_BY_ENTITY

    return {
        entity.value: [
            {
                "name": f.name,
                "requirement": f.requirement.value,
                "description": f.description,
                "examples": list(f.examples),
                "aliases": list(f.aliases[:8]),
            }
            for f in fields
        ]
        for entity, fields in FIELDS_BY_ENTITY.items()
    }


__all__ = ["Entity", "router"]
