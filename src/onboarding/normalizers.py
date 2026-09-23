"""Deterministic conversion of clinic values into canonical ones.

Every function here is a pure, typed rule. No model writes a transform and no
rule is inferred from the values in the file: that is the failure ADR-08a exists
to prevent, where a transform learned from sampled rows is applied to rows the
sample never represented and produces plausible wrong values.

Each returns an `Outcome`, never a bare value, because three answers are
possible and only one of them is "converted":

    VALID    the value converted, and the rule that did it is recorded
    REVIEW   the value is genuinely ambiguous, so a human decides
    INVALID  the value is damaged beyond recovery, so the row is rejected

The distinction is the whole point. `03/04/1991` is *ambiguous* — it is either 3
April or 4 March and nothing in the cell says which, so guessing corrupts a birth
date silently. A document number that arrived as `1.23457E+11` is *damaged* — the
digits are gone, and a wrong cédula attaches records to the wrong patient. The
first must ask; the second must refuse.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import phonenumbers

from src.registry.models import DocumentType


class Status(StrEnum):
    VALID = "valid"
    REVIEW = "review"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class Outcome[T]:
    """What a normalizer made of one cell, and which rule it applied.

    `rule` and `message` are written to the transform log for every row, so a
    reviewer can see why a value became what it became (ADR-08a).
    """

    status: Status
    value: T | None
    rule: str
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is Status.VALID


def _valid[T](value: T, rule: str) -> Outcome[T]:
    return Outcome(Status.VALID, value, rule)


def _review[T](rule: str, message: str) -> Outcome[T]:
    return Outcome(Status.REVIEW, None, rule, message)


def _invalid[T](rule: str, message: str) -> Outcome[T]:
    return Outcome(Status.INVALID, None, rule, message)


def strip_accents(value: str) -> str:
    """Casefold and remove accents, for matching only.

    Used to compare headers and labels. It is never applied to a value being
    stored: "Muñoz" and "Munoz" are different names, and restoring an accent
    that a file does not carry would be inventing data.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


# --------------------------------------------------------------- document type
# RIPS codes, with the spellings clinic files actually use. Resolución 948 de
# 2026 moved this list into a technical document MinSalud can revise without a
# new resolution, so it is configuration rather than a database constraint.
_DOCUMENT_TYPE_ALIASES: Final[dict[str, DocumentType]] = {
    "cc": DocumentType.CC,
    "c.c.": DocumentType.CC,
    "cedula": DocumentType.CC,
    "cedula de ciudadania": DocumentType.CC,
    "cedula ciudadania": DocumentType.CC,
    "ciudadania": DocumentType.CC,
    "id card": DocumentType.CC,
    "ced": DocumentType.CC,
    "ti": DocumentType.TI,
    "t.i.": DocumentType.TI,
    "tarjeta de identidad": DocumentType.TI,
    "tarjeta identidad": DocumentType.TI,
    "minor id": DocumentType.TI,
    "rc": DocumentType.RC,
    "r.c.": DocumentType.RC,
    "registro civil": DocumentType.RC,
    "registro civil de nacimiento": DocumentType.RC,
    "ce": DocumentType.CE,
    "c.e.": DocumentType.CE,
    "cedula de extranjeria": DocumentType.CE,
    "cedula extranjeria": DocumentType.CE,
    "foreign id": DocumentType.CE,
    "pa": DocumentType.PA,
    "pas": DocumentType.PA,
    "pasaporte": DocumentType.PA,
    "passport": DocumentType.PA,
    # PPT is the everyday name of the card; PT is the RIPS code. A frequent mismatch.
    "pt": DocumentType.PT,
    "ppt": DocumentType.PT,
    "permiso por proteccion temporal": DocumentType.PT,
    "pe": DocumentType.PE,
    "pep": DocumentType.PE,
    "permiso especial de permanencia": DocumentType.PE,
    "cd": DocumentType.CD,
    "carne diplomatico": DocumentType.CD,
    "sc": DocumentType.SC,
    "salvoconducto": DocumentType.SC,
    "salvoconducto de permanencia": DocumentType.SC,
    "de": DocumentType.DE,
    "documento extranjero": DocumentType.DE,
    "cn": DocumentType.CN,
    "certificado de nacido vivo": DocumentType.CN,
    "as": DocumentType.AS,
    "adulto sin identificar": DocumentType.AS,
    "ms": DocumentType.MS,
    "menor sin identificar": DocumentType.MS,
}


def document_type(raw: str) -> Outcome[DocumentType]:
    """Map a written document type onto its RIPS code.

    An unrecognised value goes to review and is never defaulted to `CC`: the
    document type is half of a patient's legal identity, and the wrong one pairs
    the right number with the wrong person.
    """
    text = strip_accents(raw)
    if not text:
        return _review("document_type.empty", "No document type given.")
    # Whole-token match only. "DE" is also the preposition inside almost every
    # other label ("cedula DE ciudadania"), so a substring match would see it
    # everywhere.
    found = _DOCUMENT_TYPE_ALIASES.get(text) or _DOCUMENT_TYPE_ALIASES.get(text.replace(".", ""))
    if found is None:
        return _review("document_type.unknown", f"Unrecognised document type {raw!r}.")
    return _valid(found, "document_type.alias")


# ------------------------------------------------------------- document number
_SCIENTIFIC = re.compile(r"^\d(?:\.\d+)?[eE][+-]?\d+$")


def document_number(raw: str) -> Outcome[str]:
    """Return the digits of a document number, or refuse the row.

    Excel damages these two ways, both unrecoverable, so both are rejected
    rather than repaired: a long number stored as a float becomes scientific
    notation and loses its low-order digits, and a number stored numerically
    loses any leading zero. A repaired cédula is a different person's.
    """
    text = raw.strip()
    if not text:
        return _invalid("document_number.empty", "Document number is empty.")

    if _SCIENTIFIC.match(text):
        return _invalid(
            "document_number.scientific_notation",
            f"{raw!r} was stored as a number and its digits are lost. "
            "Re-export the column formatted as text.",
        )

    # "1.045.678.901" is the Spanish thousands form; "1045678901.0" is a float
    # that happens to be whole. Neither loses information, so both are accepted.
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    elif text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]

    if not text.isdigit():
        # Passports and foreign documents legitimately contain letters.
        if re.fullmatch(r"[A-Za-z0-9-]{4,20}", text):
            return _valid(text.upper(), "document_number.alphanumeric")
        return _review("document_number.unexpected", f"{raw!r} is not a recognisable number.")

    if not 4 <= len(text) <= 20:
        return _invalid(
            "document_number.length",
            f"{raw!r} has {len(text)} digits; RIPS allows 4 to 20.",
        )
    return _valid(text, "document_number.digits")


# ----------------------------------------------------------------------- phone
def phone(raw: str, *, region: str = "CO") -> Outcome[str]:
    """Return an E.164 number, or say why it cannot be reached.

    A number that does not exist is never "corrected": the nearest valid number
    belongs to somebody else, and a reminder sent there is a disclosure to a
    stranger. The patient is still imported; only the number is flagged.
    """
    text = raw.strip()
    if not text:
        return _review("phone.empty", "No phone number given.")

    digits = re.sub(r"[^\d+]", "", text)
    try:
        parsed = phonenumbers.parse(digits, region)
    except phonenumbers.NumberParseException as error:
        return _review("phone.unparseable", f"{raw!r} is not a phone number ({error}).")

    if not phonenumbers.is_valid_number(parsed):
        return _review(
            "phone.not_assigned",
            f"{raw!r} is not an assigned Colombian number, so it cannot be reached.",
        )
    return _valid(
        phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
        "phone.e164",
    )


# ------------------------------------------------------------------ boolean-ish
_TRUE: Final = frozenset({"si", "s", "yes", "y", "true", "1", "x", "verdadero"})
_FALSE: Final = frozenset({"no", "n", "false", "0", "falso"})


def boolean(raw: str) -> Outcome[bool]:
    """Read SI/NO/X/1/0 and their variants.

    Blank is review rather than False: an empty attendance cell usually means
    "not recorded yet", and reading it as "did not attend" invents a fact.
    """
    text = strip_accents(raw).replace(".", "")
    if not text:
        return _review("boolean.empty", "Empty; it is not clear whether this means no.")
    if text in _TRUE:
        return _valid(True, "boolean.true")
    if text in _FALSE:
        return _valid(False, "boolean.false")
    # "N/A" is ambiguous in Spanish clinic files between "no aplica" and
    # "no asistió", which are opposites.
    return _review("boolean.unknown", f"{raw!r} is not a clear yes or no.")


# ------------------------------------------------------------------------ name
# Particles that belong to the surname that follows them.
_PARTICLES: Final = frozenset(
    {"de", "del", "la", "las", "los", "san", "santa", "van", "von", "da", "do", "di", "y", "e"}
)

# Given names that are two words. Without this list "María José Pérez Gómez"
# looks like four separate tokens and splits in the wrong place.
_COMPOUND_GIVEN: Final = frozenset(
    {
        "maria jose",
        "maria fernanda",
        "maria camila",
        "maria paula",
        "maria alejandra",
        "maria isabel",
        "maria clara",
        "maria del",
        "ana maria",
        "ana sofia",
        "ana lucia",
        "juan carlos",
        "juan pablo",
        "juan david",
        "juan jose",
        "juan manuel",
        "juan sebastian",
        "jose luis",
        "jose maria",
        "jose antonio",
        "jose david",
        "luis carlos",
        "luis fernando",
        "luis miguel",
        "carlos andres",
        "carlos alberto",
        "jorge luis",
        "diana carolina",
        "sandra milena",
        "luz marina",
        "luz dary",
        "leidy johana",
        "jhon jairo",
        "andres felipe",
        "luisa fernanda",
        "claudia patricia",
        "sandra patricia",
        "martha lucia",
    }
)


@dataclass(frozen=True, slots=True)
class SplitName:
    given_names: str
    family_names: str


def split_full_name(raw: str) -> Outcome[SplitName]:
    """Split one name column into given names and surnames, or refuse.

    Only two shapes are decidable. Two tokens are one given name and one
    surname. Four are two and two, once a compound given name is glued.

    Three tokens are refused because they are genuinely undecidable:
    "Carlos Pérez Gómez" is one given name and two surnames, while
    "Juan Carlos Pérez" is two given names and one surname, and nothing in the
    text distinguishes them. Ley 2129 de 2021 also lets parents choose the order
    of surnames, so no "the paternal surname comes first" rule can help.

    Five or more are refused for the same reason, compounded by particles.
    A refusal costs a human one decision on the confirmation screen; a wrong
    split attaches a record to the wrong person.
    """
    text = " ".join(raw.split())
    if not text:
        return _review("name.empty", "No name given.")

    tokens = text.split()

    # Glue particles onto the token they modify: "de la Cruz" is one surname.
    glued: list[str] = []
    buffer: list[str] = []
    for token in tokens:
        if strip_accents(token) in _PARTICLES:
            buffer.append(token)
            continue
        glued.append(" ".join([*buffer, token]) if buffer else token)
        buffer = []
    if buffer:  # trailing particle: the name is malformed
        return _review("name.trailing_particle", f"{raw!r} ends with a connecting word.")

    # Four parts are two given names and two surnames. The compound list is not
    # consulted here: "Carlos Andrés Pérez Gómez" splits the same way whether or
    # not "Carlos Andrés" is a recognised pair, and gluing it first would leave
    # three parts and send a perfectly clear name to review.
    if len(glued) == 4:
        return _valid(
            SplitName(f"{glued[0]} {glued[1]}", f"{glued[2]} {glued[3]}"), "name.four_tokens"
        )
    if len(glued) == 2:
        return _valid(SplitName(glued[0], glued[1]), "name.two_tokens")

    if len(glued) == 3:
        # Three parts are undecidable in general, but a recognised compound
        # given name settles it: "María José Pérez" is one person's two given
        # names and one surname, not one given name and two surnames.
        pair = " ".join(strip_accents(token) for token in glued[:2])
        if pair in _COMPOUND_GIVEN:
            return _valid(SplitName(f"{glued[0]} {glued[1]}", glued[2]), "name.compound_given")
        return _review(
            "name.three_tokens",
            f"{raw!r} could be one given name and two surnames, or two given names and one. "
            "Confirm the split, or export the name as separate columns.",
        )
    return _review(
        "name.unsupported_shape",
        f"{raw!r} has {len(glued)} parts; confirm which are given names and which are surnames.",
    )


# ------------------------------------------------------------------------ date
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_SLASHED = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")

# Excel's 1900 system is wrong before serial 61: it includes a 29 February 1900
# that never existed, and openpyxl maps serials 59 and 60 onto the same date.
_MIN_SAFE_1900_SERIAL: Final = 61
_EPOCH_1900: Final = dt.date(1899, 12, 30)
_EPOCH_1904: Final = dt.date(1904, 1, 1)


class DayFirst(StrEnum):
    """Which way a whole column reads. Decided once per column, never per row."""

    DAY_FIRST = "day_first"
    MONTH_FIRST = "month_first"
    UNDECIDED = "undecided"


def detect_day_first(values: list[str]) -> DayFirst:
    """Decide a column's date order from the whole column.

    A value whose first component exceeds 12 can only be a day, which settles
    the column. If no value settles it, the column stays UNDECIDED and every
    value in it goes to review: `03/04/1991` is 3 April or 4 March, and falling
    back to a locale guesses, because the file was written by an Excel whose
    locale we do not know.
    """
    saw_day_first = False
    saw_month_first = False
    for value in values:
        match = _SLASHED.match(value.strip())
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12 and second <= 12:
            saw_day_first = True
        elif second > 12 and first <= 12:
            saw_month_first = True

    if saw_day_first and saw_month_first:
        return DayFirst.UNDECIDED  # the column contradicts itself
    if saw_day_first:
        return DayFirst.DAY_FIRST
    if saw_month_first:
        return DayFirst.MONTH_FIRST
    return DayFirst.UNDECIDED


def date(raw: str, *, order: DayFirst, epoch_1904: bool = False) -> Outcome[dt.date]:
    """Convert one cell to a date under a decision already made for the column."""
    text = raw.strip()
    if not text:
        return _review("date.empty", "No date given.")

    # openpyxl hands back real date cells already converted, as ISO text.
    if " " in text and _ISO.match(text.split(" ")[0]):
        text = text.split(" ")[0]
    if _ISO.match(text):
        try:
            return _valid(dt.date.fromisoformat(text), "date.iso")
        except ValueError as error:
            return _invalid("date.impossible", f"{raw!r} is not a real date ({error}).")

    if match := _SLASHED.match(text):
        first, second, year = (int(g) for g in match.groups())
        if order is DayFirst.UNDECIDED:
            return _review(
                "date.ambiguous_column",
                f"{raw!r} could be day/month or month/day. Confirm the format for this column.",
            )
        day, month = (first, second) if order is DayFirst.DAY_FIRST else (second, first)
        try:
            return _valid(dt.date(year, month, day), f"date.{order.value}")
        except ValueError as error:
            return _invalid("date.impossible", f"{raw!r} is not a real date ({error}).")

    if text.isdigit():
        return _excel_serial(int(text), epoch_1904=epoch_1904)

    return _review("date.unrecognised", f"{raw!r} is not a date we recognise.")


def _excel_serial(serial: int, *, epoch_1904: bool) -> Outcome[dt.date]:
    """Convert a raw Excel serial, refusing the range Excel itself gets wrong."""
    if epoch_1904:
        return _valid(_EPOCH_1904 + dt.timedelta(days=serial), "date.serial_1904")
    if serial < _MIN_SAFE_1900_SERIAL:
        # Serial 60 is Excel's imaginary 29 February 1900, and everything below
        # it is off by one depending on who is counting.
        return _invalid(
            "date.serial_pre_1900_bug",
            f"Serial {serial} falls in the range Excel dates incorrectly (before 1 March 1900).",
        )
    return _valid(_EPOCH_1900 + dt.timedelta(days=serial), "date.serial_1900")


# ------------------------------------------------------------------------ time
# Spanish clinic files write the meridiem several ways, and Excel in a Spanish
# locale separates the letters with a non-breaking or narrow no-break space, so
# splitting on an ordinary space misses them.
_TIME_12H = re.compile(
    r"^(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*"
    r"([ap])\s*\.?\s*m\s*\.?$",
    re.IGNORECASE,
)
_TIME_24H = re.compile(r"^(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?$")
_SPACES: Final = str.maketrans({" ": " ", " ": " ", " ": " "})


def time_of_day(raw: str) -> Outcome[dt.time]:
    """Convert a clock time, in 24-hour or Spanish 12-hour form."""
    text = raw.strip().translate(_SPACES)
    if not text:
        return _review("time.empty", "No time given.")

    if match := _TIME_12H.match(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        second = int(match.group(3) or 0)
        meridiem = match.group(4).lower()
        if not 1 <= hour <= 12:
            return _invalid("time.impossible", f"{raw!r} has no valid 12-hour clock hour.")
        if meridiem == "p" and hour != 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0  # 12 a.m. is midnight
        return _valid(dt.time(hour, minute, second), "time.twelve_hour")

    if match := _TIME_24H.match(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        second = int(match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            return _invalid("time.impossible", f"{raw!r} is not a real time.")
        return _valid(dt.time(hour, minute, second), "time.twenty_four_hour")

    # Excel stores a time as a fraction of a day.
    try:
        fraction = float(text)
    except ValueError:
        return _review("time.unrecognised", f"{raw!r} is not a time we recognise.")
    if not 0.0 <= fraction < 1.0:
        return _review("time.out_of_range", f"{raw!r} is not a fraction of a day.")
    # Rounded, not truncated: 0.354166666 is 08:29:59.99, which truncates to
    # 08:29:59 and reads as a minute earlier than the clinic wrote.
    total = round(fraction * 86_400)
    return _valid(dt.time(total // 3600 % 24, total % 3600 // 60, total % 60), "time.day_fraction")


# ---------------------------------------------------------------------- status
_STATUS_ALIASES: Final[dict[str, str]] = {
    "agendada": "scheduled",
    "asignada": "scheduled",
    "programada": "scheduled",
    "reservada": "scheduled",
    "pending": "scheduled",
    "scheduled": "scheduled",
    "confirmada": "confirmed",
    "confirmado": "confirmed",
    "confirmed": "confirmed",
    "atendida": "completed",
    "cumplida": "completed",
    "asistio": "completed",
    "realizada": "completed",
    "completed": "completed",
    "cancelada": "cancelled",
    "anulada": "cancelled",
    "cancelled": "cancelled",
    "no asistio": "no_show",
    "inasistencia": "no_show",
    "no cumplio": "no_show",
    "no show": "no_show",
    "no_show": "no_show",
    "reprogramada": "rescheduled",
    "reagendada": "rescheduled",
    "rescheduled": "rescheduled",
}


def appointment_status(raw: str) -> Outcome[str]:
    """Map a written appointment state onto the canonical set.

    `pendiente` is deliberately absent: in Colombian files it means both "not
    yet confirmed" and "waiting list", which are different states. Bare `NA` is
    absent too, being either "no aplica" or "no asistió" — opposites.
    """
    text = strip_accents(raw)
    if not text:
        return _review("status.empty", "No status given.")
    if found := _STATUS_ALIASES.get(text):
        return _valid(found, "status.alias")
    if text in {"pendiente", "na", "n/a"}:
        return _review("status.ambiguous", f"{raw!r} has more than one meaning; confirm it.")
    return _review("status.unknown", f"Unrecognised status {raw!r}.")
