"""The confirmation screen: one page, no build step, no JavaScript framework.

The PDF asks for a mapping confirmation screen. This is it — a server-rendered
page a person opens after uploading, showing what each column was taken to mean
and how much of it converted, with a dropdown to correct anything wrong.

It is deliberately plain HTML. A staff interface belongs to M11 and will be
designed properly; anything more here would be work the milestone does not own,
and would have to be thrown away. What matters now is that a human can actually
see and change the mapping before anything is written, because that is the step
the whole milestone turns on.

Two decisions carried from ADR-08a are visible on the page:

- Each column shows **percent valid over every row**, not a preview of the first
  few. A preview looks reassuring while row 100 is broken.
- Anything the file cannot decide is a **question**, shown before the columns
  and blocking the commit until answered.
"""

from __future__ import annotations

import contextlib
import html
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.dependencies import ClinicScopeDep, SessionDep
from src.onboarding import repository, service
from src.onboarding.canonical import FIELDS_BY_ENTITY, Entity

router = APIRouter()

_STYLE = """
:root { color-scheme: light dark; --line:#d6dae2; --muted:#5b667a; --ok:#1b7f79;
        --warn:#b5651d; --bad:#b3261e; --bg:#fff; --fg:#1d2433; }
@media (prefers-color-scheme: dark) {
  :root { --line:#333a46; --muted:#9aa4b5; --bg:#15181d; --fg:#e8ecf3; } }
* { box-sizing:border-box; }
body { font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; margin:0;
       background:var(--bg); color:var(--fg); }
main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:17px; margin:28px 0 8px; }
.sub { color:var(--muted); margin:0 0 20px; }
table { border-collapse:collapse; width:100%; margin:8px 0 4px; font-size:14px; }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line);
        vertical-align:top; }
th { font-weight:600; font-size:12px; text-transform:uppercase;
     letter-spacing:.04em; color:var(--muted); }
select { width:100%; padding:5px; font:inherit; background:var(--bg);
         color:var(--fg); border:1px solid var(--line); border-radius:5px; }
.bar { display:inline-block; min-width:52px; }
.ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.note { color:var(--muted); font-size:13px; }
.card { border:1px solid var(--line); border-radius:8px; padding:12px 16px;
        margin:12px 0; }
.card.warn { border-color:var(--warn); }
.card.bad { border-color:var(--bad); }
button { font:inherit; padding:9px 18px; border-radius:6px; border:1px solid var(--ok);
         background:var(--ok); color:#fff; cursor:pointer; }
button.secondary { background:transparent; color:var(--fg); border-color:var(--line); }
ul { margin:6px 0; padding-left:20px; }
code { font-family:ui-monospace,Consolas,monospace; font-size:13px; }
"""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _percent(column: service.ColumnReport) -> str:
    """Percent valid, coloured by how much attention it needs."""
    if not column.total:
        return '<span class="note">not yet validated</span>'
    share = column.percent_valid
    css = "ok" if share == 100 else ("warn" if share >= 80 else "bad")
    detail = []
    if column.review:
        detail.append(f"{column.review} to review")
    if column.invalid:
        detail.append(f"{column.invalid} rejected")
    suffix = f' <span class="note">({", ".join(detail)})</span>' if detail else ""
    return f'<span class="bar {css}">{share:.0f}%</span>{suffix}'


def _options(entity: Entity, selected: str | None) -> str:
    chosen = "" if selected else " selected"
    parts = [f'<option value=""{chosen}>— do not import —</option>']
    for field in FIELDS_BY_ENTITY[entity]:
        mark = " selected" if field.name == selected else ""
        required = " *" if field.requirement.value == "required" else ""
        parts.append(
            f'<option value="{_escape(field.name)}"{mark}>'
            f"{_escape(field.name)}{required} — {_escape(field.description[:70])}</option>"
        )
    return "".join(parts)


def _sheet_section(report: service.SheetReport, session_id: uuid.UUID, clinic_id: uuid.UUID) -> str:
    rows = "".join(
        f"<tr><td><code>{_escape(c.column)}</code></td>"
        f'<td><select name="col::{_escape(c.column)}">{_options(report.entity, c.target_field)}'
        "</select></td>"
        f"<td>{_percent(c)}</td>"
        f'<td class="note">{_escape(c.reason)}</td></tr>'
        for c in report.columns
    )

    blocks = []
    if report.questions:
        items = "".join(f"<li>{_escape(q)}</li>" for q in report.questions)
        # Each question is a column the file cannot decide for itself. The answer
        # applies to the whole column, never row by row.
        inputs = "".join(
            f'<label class="note">{_escape(q.split(":")[0])}: '
            f'<select name="ask::{_escape(q.split(":")[0])}">'
            '<option value="">— choose —</option>'
            '<option value="day_first">day / month / year</option>'
            '<option value="month_first">month / day / year</option>'
            "</select></label><br>"
            for q in report.questions
        )
        blocks.append(
            f'<div class="card warn"><strong>Needs your decision</strong>'
            f"<ul>{items}</ul>{inputs}</div>"
        )
    if report.missing_required:
        missing = ", ".join(_escape(m) for m in report.missing_required)
        blocks.append(
            f'<div class="card bad"><strong>Required fields not mapped:</strong> {missing}.'
            " Choose a column for each, or export the file with those columns.</div>"
        )
    if report.warnings:
        items = "".join(f"<li>{_escape(w)}</li>" for w in report.warnings)
        blocks.append(f'<div class="card"><strong>About this file</strong><ul>{items}</ul></div>')

    counts = (
        f"{report.total_rows} rows · {report.valid_rows} ready · "
        f"{report.review_rows} to review · {report.invalid_rows} rejected"
        if report.valid_rows or report.review_rows or report.invalid_rows
        else f"{report.total_rows} rows"
    )

    return f"""
    <h2>{_escape(report.sheet)}</h2>
    <p class="sub">Read as <strong>{_escape(report.entity.value)}</strong> —
       {_escape(report.entity_reason)}<br>{counts}</p>
    {"".join(blocks)}
    <form method="post"
          action="/onboarding/uploads/{session_id}/review/{_escape(report.sheet)}?clinic_id={clinic_id}">
      <table>
        <tr><th>Column in the file</th><th>Import as</th><th>Valid</th><th>Why</th></tr>
        {rows}
      </table>
      <p><button type="submit">Save this sheet</button></p>
    </form>
    """


@router.get(
    "/uploads/{session_id}/review",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="The mapping confirmation screen",
)
async def review_screen(
    db: SessionDep, session_id: uuid.UUID, scope: ClinicScopeDep, request: Request
) -> HTMLResponse:
    record = await repository.get_session(db, clinic_id=scope.clinic_id, session_id=session_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such import session.")

    stored = record.report or {}
    reports = [service.report_from_dict(s) for s in stored.get("sheets", [])]
    sections = "".join(_sheet_section(r, session_id, scope.clinic_id) for r in reports)

    blocking = stored.get("blocking", [])
    if blocking:
        items = "".join(f"<li>{_escape(b)}</li>" for b in blocking)
        banner = (
            f'<div class="card bad"><strong>This import cannot be committed yet.</strong>'
            f"<ul>{items}</ul></div>"
        )
    elif record.status == "committed":
        banner = '<div class="card"><strong>This import has been committed.</strong></div>'
    elif record.status == "validated":
        banner = (
            '<div class="card"><strong>Ready to import.</strong> Rows awaiting review '
            "will not be written.</div>"
        )
    else:
        banner = (
            '<div class="card">Check the mapping below, then <strong>Validate</strong> to '
            "convert every row.</div>"
        )

    reused = stored.get("reused_profiles", [])
    reused_note = (
        f'<p class="note">Mapping reused from a profile you confirmed earlier for: '
        f"{_escape(', '.join(reused))}.</p>"
        if reused
        else ""
    )
    duplicate = (
        '<div class="card warn">This exact file has already been imported for this '
        "clinic. Importing it again would repeat work someone has already done.</div>"
        if stored.get("duplicate_of")
        else ""
    )

    query = f"?clinic_id={scope.clinic_id}"
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Confirm import — {_escape(record.filename)}</title>
<style>{_STYLE}</style></head>
<body><main>
  <h1>Confirm this import</h1>
  <p class="sub"><code>{_escape(record.filename)}</code> · status
     <strong>{_escape(record.status)}</strong></p>
  {duplicate}{banner}{reused_note}
  {sections}
  <h2>When the mapping is right</h2>
  <form method="post" action="/onboarding/uploads/{session_id}/review/validate{query}"
        style="display:inline">
    <button class="secondary" type="submit">Validate every row</button>
  </form>
  <form method="post" action="/onboarding/uploads/{session_id}/review/commit{query}"
        style="display:inline;margin-left:8px">
    <button type="submit">Import</button>
  </form>
  <p class="note" style="margin-top:20px">
    Percentages count every row in the file, not a sample. Rows awaiting review are
    never written. Nothing is imported until you press Import.
  </p>
</main></body></html>""")


@router.post("/uploads/{session_id}/review/validate", include_in_schema=False)
async def review_validate(
    db: SessionDep, session_id: uuid.UUID, scope: ClinicScopeDep
) -> RedirectResponse:
    from src.api.onboarding.routes import validate

    await validate(db, session_id, scope)
    return RedirectResponse(
        f"/onboarding/uploads/{session_id}/review?clinic_id={scope.clinic_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/uploads/{session_id}/review/commit", include_in_schema=False)
async def review_commit(
    db: SessionDep, session_id: uuid.UUID, scope: ClinicScopeDep
) -> RedirectResponse:
    """Import, or show the reason it was refused on the screen itself."""
    from src.api.onboarding.routes import commit

    # A refusal is not an error here: the screen already renders why, from the
    # blocking reasons validate stored, which is more use than a raw 409.
    with contextlib.suppress(HTTPException):
        await commit(db, session_id, scope, save_profile=True)
    return RedirectResponse(
        f"/onboarding/uploads/{session_id}/review?clinic_id={scope.clinic_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/uploads/{session_id}/review/{sheet}", include_in_schema=False)
async def review_save_mapping(
    db: SessionDep,
    session_id: uuid.UUID,
    sheet: str,
    scope: ClinicScopeDep,
    request: Request,
) -> RedirectResponse:
    """Apply what the reviewer chose on the screen.

    Form fields are prefixed so one submission carries both kinds of answer:
    `col::<column>` is a mapping choice, `ask::<column>` answers a question.
    """
    from src.api.onboarding.routes import set_mapping
    from src.api.onboarding.schemas import MappingIn

    form = await request.form()
    mapping: dict[str, str | None] = {}
    decisions: dict[str, str] = {}
    for key, value in form.multi_items():
        text = str(value).strip()
        if key.startswith("col::"):
            mapping[key[5:]] = text or None
        elif key.startswith("ask::") and text:
            decisions[key[5:]] = text

    await set_mapping(
        db, session_id, scope, MappingIn(sheet=sheet, mapping=mapping, decisions=decisions)
    )
    return RedirectResponse(
        f"/onboarding/uploads/{session_id}/review?clinic_id={scope.clinic_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = ["router"]
