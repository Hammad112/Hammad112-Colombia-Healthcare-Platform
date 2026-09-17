"""Time zone used for presenting and interpreting clinic-local times.

Colombia observes UTC-5 all year and has not used daylight saving time since
1993, so a fixed offset is exact and avoids depending on the IANA database,
which Windows does not ship with Python. Timestamps are stored as UTC
`timestamptz`; this offset is applied only at the edges.

Every clinic is assumed to be in Colombia. `clinics.timezone` is stored and
returned by the review API but not used for time conversion; supporting clinics
elsewhere would mean converting with each clinic's own zone.
"""

from __future__ import annotations

from datetime import timedelta, timezone

BOGOTA = timezone(timedelta(hours=-5), name="America/Bogota")
