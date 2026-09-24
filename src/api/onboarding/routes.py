"""Upload a clinic file, confirm what its columns mean, then commit it.

Drive this from Swagger UI at /docs, or from the review screen at
`/onboarding/uploads/{id}/review`. The order is deliberate and enforced:

    POST /onboarding/uploads                  read the file, propose a mapping
    GET  /onboarding/uploads/{id}             what was proposed, and why
    PUT  /onboarding/uploads/{id}/mapping     the reviewer's corrections
    POST /onboarding/uploads/{id}/validate    run every rule over every row
    GET  /onboarding/uploads/{id}/rows        the staged rows and their errors
    POST /onboarding/uploads/{id}/commit      apply, only if nothing is invalid

The session, its staged rows and its transform log live in the `onboarding`
schema, so an import survives a restart and can be audited afterwards. The
*parsed file* is held in memory for the life of the process: the upload's bytes
are discarded once read, so a restart mid-review means uploading the file again.
That is a deliberate trade — keeping a clinic's file on disk for longer than the
review needs is a larger risk than asking for it twice.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from src.api.dependencies import ClinicScopeDep, SessionDep
from src.api.onboarding.schemas import (
    CellOut,
    CommitOut,
    MappingIn,
    ProfileOut,
    RowOut,
    SheetOut,
    UploadOut,
    ValidationOut,
)
from src.onboarding import repository, service
from src.onboarding.canonical import Entity
from src.onboarding.reader import ReadResult, UnreadableFile, read

router = APIRouter()

# Uploads are spooled to disk and parsed, so this bounds what a single request
# can cost. The archive guards in reader.py bound what it can expand to.
MAX_UPLOAD_BYTES: Final = 50 * 1024 * 1024


#: Parsed files, by session id. Not the state of the import — that lives in the
#: database — only the sheets already read, so the reviewer's next request does
#: not need the original upload again.
_PARSED: dict[uuid.UUID, ReadResult] = {}


async def _load(
    db: SessionDep, session_id: uuid.UUID, clinic_id: uuid.UUID
) -> tuple[Any, ReadResult]:
    """The stored session and its parsed file, or a 404 that reveals nothing."""
    record = await repository.get_session(db, clinic_id=clinic_id, session_id=session_id)
    if record is None:
        # Not 403 for another clinic's session: "exists, but not yours" would
        # leak one clinic's activity to another.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such import session.")
    parsed = _PARSED.get(session_id)
    if parsed is None:
        raise HTTPException(
            status.HTTP_410_GONE,
            "The uploaded file is no longer held in memory, which happens after a "
            "restart. Upload it again to continue.",
        )
    return record, parsed


def _reports(record: Any) -> list[service.SheetReport]:
    return [service.report_from_dict(s) for s in (record.report or {}).get("sheets", [])]


def _store_reports(record: Any, reports: list[service.SheetReport], **extra: Any) -> None:
    """Replace the session's stored report. JSONB needs a new object to persist."""
    report = dict(record.report or {})
    report["sheets"] = [service.report_to_dict(r) for r in reports]
    report.update(extra)
    record.report = report


def _mapping_of(record: Any, sheet: str) -> dict[str, str | None]:
    return dict((record.report or {}).get("mappings", {}).get(sheet, {}))


def _decisions_of(record: Any, sheet: str) -> dict[str, str]:
    return dict((record.report or {}).get("decisions", {}).get(sheet, {}))


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
    db: SessionDep,
    scope: ClinicScopeDep,
    file: Annotated[UploadFile, File(description="An .xlsx or .csv export from the clinic.")],
) -> UploadOut:
    """Read the file and propose what each column means. Nothing is converted yet.

    If this clinic already confirmed a mapping for a file of this shape, that
    profile is applied and there is nothing left to correct — which is what lets
    a repeat import run without a model call.
    """
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

        reports = list(service.analyse(result))
        sha256 = digest.hexdigest()

        # A confirmed mapping for a file of this shape replaces the proposal.
        mappings: dict[str, dict[str, str | None]] = {}
        reused: list[str] = []
        for index, report in enumerate(reports):
            sheet = next(s for s in result.sheets if s.name == report.sheet)
            profile = await repository.find_profile(
                db,
                clinic_id=scope.clinic_id,
                fingerprint=repository.header_fingerprint(sheet.headers),
            )
            if profile is None:
                mappings[report.sheet] = _default_mapping(report)
                continue
            stored = dict(profile.mapping.get("mapping", {}))
            mappings[report.sheet] = {c.column: stored.get(c.column) for c in report.columns}
            reports[index] = service.apply_profile(report, mappings[report.sheet])
            reused.append(report.sheet)

        previous = await repository.find_previous_import(
            db, clinic_id=scope.clinic_id, file_sha256=sha256
        )
        record = await repository.create_session(
            db,
            clinic_id=scope.clinic_id,
            filename=file.filename or target.name,
            file_size=written,
            file_sha256=sha256,
            report={},
        )
        _store_reports(
            record,
            reports,
            mappings=mappings,
            decisions={},
            reused_profiles=reused,
            duplicate_of=str(previous.id) if previous else None,
        )
        record.total_rows = sum(r.total_rows for r in reports)
        await db.flush()
        _PARSED[record.id] = result

        return UploadOut(
            session_id=record.id,
            filename=record.filename,
            file_sha256=sha256,
            encoding=result.encoding,
            delimiter=result.delimiter,
            sheets=[SheetOut.build(r) for r in reports],
            status=record.status,
            reused_profiles=reused,
            duplicate_of=previous.id if previous else None,
        )
    finally:
        shutil.rmtree(target.parent, ignore_errors=True)


@router.get(
    "/uploads/{session_id}",
    response_model=UploadOut,
    summary="What was proposed for this file, and why",
)
async def get_upload(db: SessionDep, session_id: uuid.UUID, scope: ClinicScopeDep) -> UploadOut:
    record, parsed = await _load(db, session_id, scope.clinic_id)
    stored = record.report or {}
    return UploadOut(
        session_id=record.id,
        filename=record.filename,
        file_sha256=record.file_sha256,
        encoding=parsed.encoding,
        delimiter=parsed.delimiter,
        sheets=[SheetOut.build(r) for r in _reports(record)],
        status=record.status,
        reused_profiles=list(stored.get("reused_profiles", [])),
        duplicate_of=uuid.UUID(stored["duplicate_of"]) if stored.get("duplicate_of") else None,
    )


@router.put(
    "/uploads/{session_id}/mapping",
    response_model=UploadOut,
    summary="Correct the proposed mapping for one sheet",
)
async def set_mapping(
    db: SessionDep, session_id: uuid.UUID, scope: ClinicScopeDep, body: MappingIn
) -> UploadOut:
    """Replace a sheet's mapping with what the reviewer confirmed.

    A field may be mapped from at most one column: two columns writing one field
    would mean one of them silently wins.
    """
    record, _ = await _load(db, session_id, scope.clinic_id)
    reports = _reports(record)
    report = next((r for r in reports if r.sheet == body.sheet), None)
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

    stored = dict(record.report or {})
    mappings = dict(stored.get("mappings", {}))
    decisions = dict(stored.get("decisions", {}))
    # Merged, not replaced. The screen posts every column, but a caller sending
    # one correction should not silently clear the rest of the sheet.
    merged = dict(mappings.get(body.sheet, {}))
    merged.update(body.mapping)
    mappings[body.sheet] = merged
    decisions[body.sheet] = dict(body.decisions)

    reports[reports.index(report)] = service.apply_profile(report, mappings[body.sheet])
    _store_reports(record, reports, mappings=mappings, decisions=decisions)
    record.status = "mapped"
    await db.flush()
    return await get_upload(db, session_id, scope)


@router.post(
    "/uploads/{session_id}/validate",
    response_model=ValidationOut,
    summary="Run every rule over every row and stage the results",
)
async def validate(db: SessionDep, session_id: uuid.UUID, scope: ClinicScopeDep) -> ValidationOut:
    """Convert all rows, stage them, and say whether they may be committed.

    Runs over 100% of rows, never a sample (ADR-08a): a sample that looks fine
    is exactly how a bad transform reaches production.
    """
    record, parsed = await _load(db, session_id, scope.clinic_id)
    reports = _reports(record)

    summaries: list[service.SheetReport] = []
    blocking: list[str] = []
    totals = {"valid": 0, "review": 0, "invalid": 0}

    for report in reports:
        sheet = next(s for s in parsed.sheets if s.name == report.sheet)
        mapping = _mapping_of(record, report.sheet)
        if not any(mapping.values()):
            continue  # nothing confirmed for this sheet, so nothing to import

        rows, columns = service.validate(
            sheet, report.entity, mapping, decisions=_decisions_of(record, report.sheet)
        )
        await repository.replace_staging(
            db,
            clinic_id=scope.clinic_id,
            session_id=record.id,
            sheet=report.sheet,
            entity=report.entity,
            rows=rows,
        )
        summary = service.summarise(rows, report, columns)
        summaries.append(summary)
        totals["valid"] += summary.valid_rows
        totals["review"] += summary.review_rows
        totals["invalid"] += summary.invalid_rows

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

    if not summaries:
        blocking.append("No sheet has a confirmed mapping, so there is nothing to import.")

    record.status = "validated" if not blocking else "needs_review"
    record.valid_rows = totals["valid"]
    record.review_rows = totals["review"]
    record.invalid_rows = totals["invalid"]
    _store_reports(record, summaries or reports, blocking=blocking)
    await db.flush()

    return ValidationOut(
        session_id=record.id,
        status=record.status,
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
    db: SessionDep,
    session_id: uuid.UUID,
    scope: ClinicScopeDep,
    sheet: Annotated[str | None, Query(description="Which sheet's rows to show.")] = None,
    row_status: Annotated[
        str | None, Query(alias="status", description="valid, review or invalid.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[RowOut]:
    await _load(db, session_id, scope.clinic_id)
    if not await repository.staged_rows(
        db, clinic_id=scope.clinic_id, session_id=session_id, limit=1
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Validate the import before reading its rows."
        )
    rows = await repository.staged_rows(
        db,
        clinic_id=scope.clinic_id,
        session_id=session_id,
        sheet=sheet,
        status=row_status,
        limit=limit,
    )
    return [
        RowOut(
            row_number=r.row_number,
            status=r.status,
            values={k: str(v) for k, v in (r.normalized or {}).items()},
            errors=list((r.errors or {}).get("errors", [])),
            reviews=list((r.errors or {}).get("reviews", [])),
        )
        for r in rows
    ]


@router.get(
    "/uploads/{session_id}/transform-log",
    response_model=list[CellOut],
    summary="What every rule did to every cell (ADR-08a)",
)
async def transform_log(
    db: SessionDep,
    session_id: uuid.UUID,
    scope: ClinicScopeDep,
    cell_status: Annotated[
        str | None, Query(alias="status", description="valid, review or invalid.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> list[CellOut]:
    """The evidence that no transform was inferred from the data.

    Every cell carries the named rule that produced its value, so a reviewer can
    ask why a value became what it became without re-running the import.
    """
    await _load(db, session_id, scope.clinic_id)
    entries = await repository.transform_log(
        db, clinic_id=scope.clinic_id, session_id=session_id, status=cell_status, limit=limit
    )
    if not entries and not await repository.transform_log(
        db, clinic_id=scope.clinic_id, session_id=session_id, limit=1
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Validate the import before reading its transform log."
        )
    return [
        CellOut(
            row_number=e.row_number,
            column=e.column_name,
            target_field=e.target_field,
            raw=e.raw_value or "",
            normalized=e.normalized_value,
            rule=e.rule,
            status=e.status,
            message=e.message or "",
        )
        for e in entries
    ]


@router.post(
    "/uploads/{session_id}/commit",
    response_model=CommitOut,
    summary="Apply the import, if and only if nothing is invalid",
)
async def commit(
    db: SessionDep,
    session_id: uuid.UUID,
    scope: ClinicScopeDep,
    save_profile: Annotated[
        bool, Query(description="Remember this mapping for the next file of the same shape.")
    ] = True,
) -> CommitOut:
    """Write the valid rows into the clinic's tables, in one transaction, or refuse.

    Refuses on exactly what `validate` reported. Recomputing a narrower check
    here is how the two drift apart: an earlier version counted invalid rows
    only, so a sheet blocked on an unanswered date question committed anyway.

    Rows awaiting review are not written. A patient imported without a usable
    identifier is worse than a patient not yet imported.
    """
    record, parsed = await _load(db, session_id, scope.clinic_id)
    stored = record.report or {}

    if record.status == "committed":
        raise HTTPException(status.HTTP_409_CONFLICT, "This import was already committed.")
    # What blocked validation is reported before "validate it first", so the
    # reviewer is told what is actually wrong rather than to repeat a step they
    # have already done.
    if blocking := list(stored.get("blocking", [])):
        raise HTTPException(status.HTTP_409_CONFLICT, "Refusing to commit. " + " ".join(blocking))
    if record.status != "validated":
        raise HTTPException(status.HTTP_409_CONFLICT, "Validate the import before committing it.")

    committed: dict[str, int] = {}
    skipped: dict[str, int] = {}
    conflicts: list[str] = []

    # Specialties before doctors, so a doctor can reference the catalogue its
    # own file defines.
    order = {Entity.SPECIALTY: 0, Entity.PATIENT: 1, Entity.DOCTOR: 2}
    for report in sorted(_reports(record), key=lambda r: order.get(r.entity, 9)):
        mapping = _mapping_of(record, report.sheet)
        if not any(mapping.values()):
            continue
        sheet = next(s for s in parsed.sheets if s.name == report.sheet)
        rows, _ = service.validate(
            sheet, report.entity, mapping, decisions=_decisions_of(record, report.sheet)
        )
        applied = await repository.apply_rows(
            db, clinic_id=scope.clinic_id, entity=report.entity, rows=rows
        )
        committed[report.sheet] = applied.created + applied.updated
        skipped[report.sheet] = applied.skipped
        conflicts.extend(applied.conflicts)

        if save_profile:
            await repository.save_profile(
                db,
                clinic_id=scope.clinic_id,
                name=f"{record.filename} - {report.sheet}",
                fingerprint=repository.header_fingerprint(sheet.headers),
                mapping={"mapping": mapping, "entity": report.entity.value},
            )

    record.status = "committed"
    await db.flush()

    return CommitOut(
        session_id=record.id,
        status=record.status,
        committed=committed,
        skipped=skipped,
        conflicts=conflicts,
        message=(
            "Imported. Rows awaiting review were not written"
            + (
                ", and the mapping was saved for the next file of this shape."
                if save_profile
                else "."
            )
        ),
    )


@router.get(
    "/profiles",
    response_model=list[ProfileOut],
    summary="Mapping profiles this clinic has confirmed",
)
async def profiles(db: SessionDep, scope: ClinicScopeDep) -> list[ProfileOut]:
    """A profile here is why a repeat import needs no review and no model call."""
    return [
        ProfileOut(
            id=p.id,
            name=p.name,
            header_fingerprint=p.header_fingerprint,
            entity=str(p.mapping.get("entity", "")),
            mapping={k: v for k, v in p.mapping.get("mapping", {}).items() if v},
            version=p.version,
            updated_at=p.updated_at,
        )
        for p in await repository.list_profiles(db, clinic_id=scope.clinic_id)
    ]


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
