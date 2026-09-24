"""Ask a model which canonical field an ambiguous column holds.

This is a **fallback**, not the mapping engine. The dictionary, containment and
fuzzy stages in `matcher.py` resolve ordinary Spanish and English headings on
their own, so a typical import calls nothing here at all. A model is asked only
about a column those stages left ambiguous, and only when a key is configured;
with no key the column simply goes to the person confirming the import, which is
where it would have gone anyway.

**No patient value can reach a provider, structurally rather than by care.**
`ColumnQuestion` carries a column *name*, our derived statistics, and examples
we generated ourselves. It has no field that can hold a cell, so a caller cannot
pass one by mistake, and `build_messages` is given nothing else. A sentinel test
plants recognisable values in every fixture cell and asserts none of them
appears in an outbound payload.

**The model chooses from a list; it never writes a transform.** The response
schema constrains `target_field` to an enum of canonical field names plus
`__NO_MATCH__`, so a hallucinated field is rejected by the schema rather than
detected afterwards. Which normalizer then converts the column is fixed in
`canonical.py`. This is the separation ADR-08a exists to enforce.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, Field, ValidationError

from src.core.config import Settings, get_settings
from src.core.logging import get_logger
from src.onboarding.canonical import FIELDS_BY_ENTITY, Entity

log = get_logger(__name__)

#: Returned by the model when no canonical field fits. Explicit, so "none of
#: these" is a choice it can make rather than something it has to fake.
NO_MATCH: Final = "__NO_MATCH__"


class LLMUnavailable(Exception):
    """No provider could answer. The column goes to the human instead."""


@dataclass(frozen=True, slots=True)
class ColumnQuestion:
    """One ambiguous column, described without any of its contents.

    There is deliberately no field here for a cell value. The type is the
    guarantee: a caller cannot hand this object a patient's name, so no amount
    of carelessness downstream can transmit one.
    """

    #: The heading exactly as the file spells it. A heading is not patient data.
    header: str
    #: What the values look like, derived locally and never the values
    #: themselves: "10 digits", "date-like", "1 of 4 repeating values".
    shape: str
    #: How full the column is, as a percentage. A count, not content.
    filled_percent: int
    #: How many distinct values it holds. Again a count.
    distinct_count: int
    #: Examples **we generated** to show the shape, never sampled from the file.
    synthetic_examples: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Suggestion:
    column: str
    target_field: str | None
    confidence: float
    reason: str
    provider: str
    model: str


class _Proposal(BaseModel):
    """The shape the model must answer in."""

    target_field: str = Field(description="A canonical field name, or __NO_MATCH__.")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=200)


@dataclass(slots=True)
class _Provider:
    name: str
    model: str
    api_key: str
    base_url: str | None = None


def _providers(settings: Settings) -> list[_Provider]:
    """Providers to try, in order.

    OpenAI first because that is the key the client pays for. Groq second: its
    quota is independent, so it is a real failover for a rate limit rather than
    a second attempt at the same exhausted budget.
    """
    found: list[_Provider] = []
    if key := settings.openai_api_key.get_secret_value():
        found.append(_Provider("openai", settings.mapping_model_openai, key))
    if key := settings.groq_api_key.get_secret_value():
        found.append(_Provider("groq", settings.mapping_model_groq, key, settings.groq_base_url))
    return found


def _candidate_fields(entity: Entity, candidates: tuple[str, ...]) -> list[dict[str, str]]:
    """The fields the model may choose between, described for a reader."""
    allowed = set(candidates)
    return [
        {
            "name": f.name,
            "means": f.description,
            # Examples from the canonical schema, written by us. The file's own
            # values are never used for this.
            "looks_like": ", ".join(f.examples) if f.examples else "",
        }
        for f in FIELDS_BY_ENTITY[entity]
        if not allowed or f.name in allowed
    ]


def build_messages(
    question: ColumnQuestion, entity: Entity, candidates: tuple[str, ...] = ()
) -> list[dict[str, str]]:
    """The exact payload sent to a provider. Nothing else is transmitted.

    The heading is repeated in the user message: with no real values to go on,
    emphasising the header is the one thing measured to help a zero-shot model
    (Magneto, VLDB 2025), and our privacy rule puts us firmly in that regime.
    """
    fields = _candidate_fields(entity, candidates)
    return [
        {
            "role": "system",
            "content": (
                "You map a column heading from a Colombian medical clinic's spreadsheet "
                "onto one canonical field. Headings may be Spanish or English, may lack "
                "accents, and may be abbreviated. Choose the single best field, or "
                f"{NO_MATCH} if none fits. Never invent a field name. You are given no "
                "patient data and must not ask for any."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "column_heading": question.header,
                    "heading_again": question.header,
                    "value_shape": question.shape,
                    "filled_percent": question.filled_percent,
                    "distinct_values": question.distinct_count,
                    "example_of_that_shape": list(question.synthetic_examples),
                    "candidate_fields": fields,
                },
                ensure_ascii=False,
            ),
        },
    ]


def _schema(entity: Entity, candidates: tuple[str, ...]) -> dict[str, Any]:
    """A JSON schema whose enum makes a hallucinated field impossible."""
    allowed = [f["name"] for f in _candidate_fields(entity, candidates)] + [NO_MATCH]
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "column_mapping",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "target_field": {"type": "string", "enum": allowed},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["target_field", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
    }


def suggest(
    question: ColumnQuestion,
    entity: Entity,
    *,
    candidates: tuple[str, ...] = (),
    settings: Settings | None = None,
    client_factory: Any = None,
) -> Suggestion:
    """Ask the configured providers about one column, or raise.

    Raising is a normal outcome, not an error: the caller shows the column to a
    person instead, which is what would have happened without a provider at all.
    """
    settings = settings or get_settings()
    providers = _providers(settings)
    if not providers:
        raise LLMUnavailable("No model provider is configured.")

    messages = build_messages(question, entity, candidates)
    # A digest of what was sent, so a reviewer can confirm afterwards exactly
    # which payload left the machine without the payload itself being stored.
    digest = hashlib.sha256(
        json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    failures: list[str] = []
    for index, provider in enumerate(providers):
        started = time.monotonic()
        try:
            client = (client_factory or _openai_client)(provider, settings)
            response = client.chat.completions.create(
                model=provider.model,
                messages=messages,
                response_format=_schema(entity, candidates),
                temperature=0,
            )
            content = response.choices[0].message.content or ""
            proposal = _Proposal.model_validate_json(content)
        except ValidationError as error:
            # Under a strict schema this should be impossible, so it means the
            # model has stopped honouring it. Failing over is right; retrying
            # the same provider would only repeat the same violation.
            failures.append(f"{provider.name}: response did not match the schema")
            _log_call(provider, question, digest, started, "schema_violation", str(error))
            continue
        except Exception as error:  # provider errors are not ours to classify
            failures.append(f"{provider.name}: {type(error).__name__}")
            _log_call(provider, question, digest, started, "error", str(error)[:200])
            continue

        _log_call(
            provider,
            question,
            digest,
            started,
            "ok",
            "",
            usage=getattr(response, "usage", None),
            attempt=index,
        )
        target = None if proposal.target_field == NO_MATCH else proposal.target_field
        return Suggestion(
            column=question.header,
            target_field=target,
            confidence=proposal.confidence,
            reason=proposal.reason,
            provider=provider.name,
            model=provider.model,
        )

    raise LLMUnavailable("; ".join(failures))


def _openai_client(provider: _Provider, settings: Settings) -> Any:
    """One SDK for both providers: Groq speaks the OpenAI protocol."""
    from openai import OpenAI

    return OpenAI(
        api_key=provider.api_key,
        base_url=provider.base_url,
        timeout=settings.mapping_timeout_seconds,
        max_retries=2,
    )


def _log_call(
    provider: _Provider,
    question: ColumnQuestion,
    payload_sha256: str,
    started: float,
    outcome: str,
    detail: str,
    *,
    usage: Any = None,
    attempt: int = 0,
) -> None:
    """Record the call for the audit trail.

    The prompt is absent rather than redacted: `payload_sha256` lets a reviewer
    recompute the digest from the column names in the import and confirm what
    was sent, without the payload ever being stored.
    """
    log.info(
        "llm.call",
        provider=provider.name,
        model=provider.model,
        purpose="column_mapping",
        column=question.header,
        attempt=attempt,
        latency_ms=round((time.monotonic() - started) * 1000),
        outcome=outcome,
        detail=detail or None,
        payload_sha256=payload_sha256,
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
    )


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """Local statistics about a column. Derived here, never transmitted raw."""

    shape: str
    filled_percent: int
    distinct_count: int
    examples: tuple[str, ...] = field(default=())


def profile_column(values: list[str]) -> ColumnProfile:
    """Describe a column's values without keeping any of them.

    Everything returned is a count or a category. The `examples` are patterns
    built from the shape, so what leaves the machine describes the column
    without ever being a patient's.
    """
    filled = [v.strip() for v in values if v.strip()]
    if not filled:
        return ColumnProfile("empty", 0, 0)

    percent = round(100 * len(filled) / len(values))
    distinct = len(set(filled))

    digits = sum(1 for v in filled if v.isdigit())
    with_at = sum(1 for v in filled if "@" in v)
    dated = sum(1 for v in filled if _looks_like_date(v))
    timed = sum(1 for v in filled if ":" in v and len(v) <= 12)

    share = len(filled)
    if digits == share:
        lengths = {len(v) for v in filled}
        length = f"{lengths.pop()} digits" if len(lengths) == 1 else "digits of varying length"
        return ColumnProfile(length, percent, distinct, (_synthetic_digits(filled[0]),))
    if with_at > share * 0.8:
        return ColumnProfile("email addresses", percent, distinct, ("nombre@ejemplo.com",))
    if dated > share * 0.8:
        return ColumnProfile("dates", percent, distinct, ("2026-10-15",))
    if timed > share * 0.8:
        return ColumnProfile("times of day", percent, distinct, ("07:30",))
    if distinct <= 8 and share > 8:
        # A small set of repeating labels: a status or a category.
        return ColumnProfile(
            f"{distinct} repeating labels", percent, distinct, ("etiqueta-a", "etiqueta-b")
        )
    return ColumnProfile("free text", percent, distinct, ("texto",))


def _looks_like_date(value: str) -> bool:
    return bool(value) and sum(c in "/-" for c in value) == 2 and any(c.isdigit() for c in value)


def _synthetic_digits(sample: str) -> str:
    """A number of the same length, invented rather than taken from the file."""
    return "1234567890123456789012"[: len(sample)] or "1234567890"
