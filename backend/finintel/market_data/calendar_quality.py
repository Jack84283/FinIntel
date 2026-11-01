"""Calendar-aware missing-session classification for local quality scans.

The exchange calendar is deliberately loaded lazily.  The desktop backend can
start and serve unrelated local APIs when the optional calendar dependency is
unavailable; a scan then records an ``unresolved`` candidate instead of
claiming that an observation is missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

CALENDAR_RULE_VERSION = "calendar-missing-session-v1"
CALENDAR_SOURCE = "exchange_calendars"

CALENDAR_ALIASES = {
    "NYSE": "XNYS",
    "XNYS": "XNYS",
    "NASDAQ": "XNAS",
    "XNAS": "XNAS",
}

# FRED publishes the market-index series on the underlying US trading-session
# schedule.  Keeping this mapping explicit prevents a FRED index from being
# compared with an equity venue or a generic Monday-Friday calendar.
INDEX_CALENDARS = {
    "SP500": "XNYS",
    "DJIA": "XNYS",
    "NASDAQCOM": "XNAS",
    "NDX": "XNAS",
    "VIX": "XNYS",
}

# These are publication grace periods, not provider quota or retry settings.
# They only affect the classification of a very recent expected session.
DEFAULT_PUBLICATION_DELAY_SESSIONS = {
    "alpaca_sip": 0,
    "tiingo": 1,
    "alpha_vantage": 1,
    "fred": 1,
}

CLASSIFICATION_ATTRIBUTES = {
    "expected_closure": ("info", "high"),
    "pending_publication": ("info", "medium"),
    "suspected_missing": ("warning", "low"),
    "unresolved": ("info", "low"),
}


@dataclass(frozen=True)
class SuspensionEvidence:
    """One sourced interval in which an instrument was documented as halted."""

    start: date
    end: date | None
    source: str
    reason: str

    def contains(self, session: date) -> bool:
        return self.start <= session and (self.end is None or session <= self.end)


@dataclass(frozen=True)
class CalendarEvidence:
    """Optional sourced instrument evidence used to avoid false gap findings."""

    calendar_name: str | None = None
    listed_on: date | None = None
    delisted_on: date | None = None
    publication_delay_sessions: int | None = None
    source: str | None = None
    verified_at: date | None = None
    suspensions: tuple[SuspensionEvidence, ...] = ()


def _parse_date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid {field} date") from exc
    raise ValueError(f"Invalid {field} date")


def _normalize_suspension(value: object) -> SuspensionEvidence:
    if isinstance(value, SuspensionEvidence):
        suspension = value
    elif isinstance(value, Mapping):
        start = _parse_date(value.get("start"), "suspension start")
        end = _parse_date(value.get("end"), "suspension end")
        source = str(value.get("source") or "").strip()
        reason = str(value.get("reason") or "documented trading suspension").strip()
        if start is None or not source or not reason:
            raise ValueError("Suspension evidence needs start, source and reason")
        suspension = SuspensionEvidence(start, end, source, reason)
    else:
        raise TypeError("Invalid suspension evidence")
    if suspension.end is not None and suspension.end < suspension.start:
        raise ValueError("Suspension end must not precede its start")
    if not suspension.source or not suspension.reason:
        raise ValueError("Suspension evidence needs source and reason")
    return suspension


def normalize_evidence(value: CalendarEvidence | Mapping[str, object] | None) -> CalendarEvidence:
    """Validate an optional evidence record without contacting a provider."""

    if value is None:
        return CalendarEvidence()
    if isinstance(value, CalendarEvidence):
        evidence = value
    elif isinstance(value, Mapping):
        calendar_name = value.get("calendar_name", value.get("calendar"))
        listed_on = _parse_date(value.get("listed_on"), "listed_on")
        delisted_on = _parse_date(value.get("delisted_on"), "delisted_on")
        verified_at = _parse_date(value.get("verified_at"), "verified_at")
        delay = value.get("publication_delay_sessions")
        try:
            publication_delay = None if delay is None else int(delay)
        except (TypeError, ValueError) as exc:
            raise ValueError("publication_delay_sessions must be an integer") from exc
        suspensions = tuple(
            _normalize_suspension(item) for item in (value.get("suspensions") or ())
        )
        evidence = CalendarEvidence(
            calendar_name=str(calendar_name).strip().upper() if calendar_name else None,
            listed_on=listed_on,
            delisted_on=delisted_on,
            publication_delay_sessions=publication_delay,
            source=str(value.get("source") or "").strip() or None,
            verified_at=verified_at,
            suspensions=suspensions,
        )
    else:
        raise TypeError("Invalid calendar evidence")
    if evidence.calendar_name is not None:
        canonical = CALENDAR_ALIASES.get(evidence.calendar_name.upper())
        if canonical is None:
            raise ValueError(f"Unsupported exchange calendar: {evidence.calendar_name}")
        evidence = CalendarEvidence(
            calendar_name=canonical,
            listed_on=evidence.listed_on,
            delisted_on=evidence.delisted_on,
            publication_delay_sessions=evidence.publication_delay_sessions,
            source=evidence.source,
            verified_at=evidence.verified_at,
            suspensions=evidence.suspensions,
        )
    if (
        evidence.listed_on is not None
        and evidence.delisted_on is not None
        and evidence.delisted_on < evidence.listed_on
    ):
        raise ValueError("delisted_on must not precede listed_on")
    if (
        evidence.publication_delay_sessions is not None
        and not 0 <= evidence.publication_delay_sessions <= 10
    ):
        raise ValueError("publication_delay_sessions must be between 0 and 10")
    return evidence


def evidence_identity(value: CalendarEvidence | Mapping[str, object] | None) -> dict:
    """Return a deterministic, redacted policy representation for scan cursors."""

    evidence = normalize_evidence(value)
    return {
        "calendar_name": evidence.calendar_name,
        "listed_on": evidence.listed_on.isoformat() if evidence.listed_on else None,
        "delisted_on": evidence.delisted_on.isoformat() if evidence.delisted_on else None,
        "publication_delay_sessions": evidence.publication_delay_sessions,
        "source": evidence.source,
        "verified_at": evidence.verified_at.isoformat() if evidence.verified_at else None,
        "suspensions": [
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat() if item.end else None,
                "source": item.source,
                "reason": item.reason,
            }
            for item in evidence.suspensions
        ],
    }


def default_calendar_name(
    *, instrument_kind: str, code: str, exchange: str | None = None
) -> str | None:
    if instrument_kind == "index":
        return INDEX_CALENDARS.get(code.upper())
    normalized = (exchange or "").strip().upper()
    if normalized in CALENDAR_ALIASES:
        return CALENDAR_ALIASES[normalized]
    if "NASDAQ" in normalized:
        return "XNAS"
    if "NYSE" in normalized:
        return "XNYS"
    return None


@lru_cache(maxsize=4)
def _calendar(name: str):
    """Load an exchange calendar only when a quality scan needs one."""

    try:
        import exchange_calendars as xcals
    except ImportError:
        return None
    try:
        return xcals.get_calendar(name)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _calendar_sessions(name: str, start: date, end: date) -> tuple[date, ...] | None:
    if start > end:
        return ()
    calendar = _calendar(name)
    if calendar is None:
        return None
    try:
        import pandas as pd

        sessions = calendar.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
        return tuple(session.date() for session in sessions)
    except (AttributeError, ImportError, TypeError, ValueError):
        return None


def _calendar_timezone(name: str | None) -> str | None:
    if not name:
        return None
    calendar = _calendar(name)
    timezone = getattr(calendar, "tz", None) if calendar is not None else None
    return str(timezone) if timezone is not None else None


def _publication_delay(provider: str, evidence: CalendarEvidence) -> int:
    if evidence.publication_delay_sessions is not None:
        return evidence.publication_delay_sessions
    return DEFAULT_PUBLICATION_DELAY_SESSIONS.get(provider, 0)


def _pending_publication(
    session: date,
    *,
    calendar_name: str,
    provider: str,
    evidence: CalendarEvidence,
    as_of: date,
) -> bool:
    delay = _publication_delay(provider, evidence)
    if delay <= 0:
        return False
    recent_start = as_of - timedelta(days=370)
    sessions = _calendar_sessions(calendar_name, recent_start, as_of)
    if not sessions or session not in sessions:
        return False
    eligible_index = len(sessions) - 1 - delay
    return eligible_index >= 0 and sessions.index(session) > eligible_index


def _eligible(session: date, evidence: CalendarEvidence) -> bool:
    return not (
        (evidence.listed_on is not None and session < evidence.listed_on)
        or (evidence.delisted_on is not None and session > evidence.delisted_on)
    )


def _record(
    session: date,
    *,
    classification: str,
    calendar_name: str | None,
    provider: str,
    evidence: CalendarEvidence,
    as_of: date,
    previous_session: date | None,
    next_session: date | None,
    calendar_available: bool,
    suspension: SuspensionEvidence | None = None,
) -> dict:
    attributes = CLASSIFICATION_ATTRIBUTES[classification]
    return {
        "session_date": session,
        "classification": classification,
        "severity": attributes[0],
        "confidence": attributes[1],
        "rule": f"calendar_missing_session:{classification}",
        "evidence": {
            "classification": classification,
            "calendar_source": CALENDAR_SOURCE if calendar_available else "unavailable",
            "calendar_rule_version": CALENDAR_RULE_VERSION,
            "calendar_name": calendar_name,
            "calendar_timezone": _calendar_timezone(calendar_name),
            "calendar_available": calendar_available,
            "provider": provider,
            "publication_delay_sessions": _publication_delay(provider, evidence),
            "as_of": as_of.isoformat(),
            "previous_session": previous_session.isoformat() if previous_session else None,
            "next_session": next_session.isoformat() if next_session else None,
            "listing_boundary": {
                "listed_on": evidence.listed_on.isoformat() if evidence.listed_on else None,
                "delisted_on": evidence.delisted_on.isoformat() if evidence.delisted_on else None,
                "source": evidence.source,
                "verified_at": evidence.verified_at.isoformat() if evidence.verified_at else None,
            },
            "suspension": (
                {
                    "start": suspension.start.isoformat(),
                    "end": suspension.end.isoformat() if suspension.end else None,
                    "source": suspension.source,
                    "reason": suspension.reason,
                }
                if suspension
                else None
            ),
        },
    }


def classify_missing_sessions(
    observed_sessions: set[date] | list[date] | tuple[date, ...],
    *,
    calendar_name: str | None,
    provider: str,
    evidence: CalendarEvidence | Mapping[str, object] | None = None,
    expected_start: date | None = None,
    expected_end: date | None = None,
    as_of: date | None = None,
) -> list[dict]:
    """Classify absent sessions in an observed or explicitly expected window.

    If no expected bounds are supplied, only the interval between the first and
    last observed sessions is considered. This avoids inventing leading or
    trailing gaps for a partial historical download.
    """

    normalized = normalize_evidence(evidence)
    as_of = as_of or datetime.now(UTC).date()
    if expected_start is None and observed_sessions:
        expected_start = min(observed_sessions)
    if expected_end is None and observed_sessions:
        expected_end = max(observed_sessions)
    if expected_start is None or expected_end is None or expected_start > expected_end:
        return []
    observed = set(observed_sessions)
    canonical_name = (
        CALENDAR_ALIASES.get(calendar_name.upper()) if calendar_name else normalized.calendar_name
    )
    if canonical_name is None:
        # Weekdays are only a review hint when the venue is unknown. No holiday
        # or exceptional-closure claim is made, and the finding is unresolved.
        candidates = [
            expected_start + timedelta(days=offset)
            for offset in range((expected_end - expected_start).days + 1)
            if (expected_start + timedelta(days=offset)).weekday() < 5
            and expected_start + timedelta(days=offset) not in observed
        ]
        return [
            _record(
                session,
                classification="unresolved",
                calendar_name=None,
                provider=provider,
                evidence=normalized,
                as_of=as_of,
                previous_session=None,
                next_session=None,
                calendar_available=False,
            )
            for session in candidates
            if _eligible(session, normalized)
        ]

    sessions = _calendar_sessions(canonical_name, expected_start, expected_end)
    if sessions is None:
        return [
            _record(
                session,
                classification="unresolved",
                calendar_name=canonical_name,
                provider=provider,
                evidence=normalized,
                as_of=as_of,
                previous_session=None,
                next_session=None,
                calendar_available=False,
            )
            for session in sorted(
                {
                    expected_start + timedelta(days=offset)
                    for offset in range((expected_end - expected_start).days + 1)
                }
                - observed
            )
            if session.weekday() < 5 and _eligible(session, normalized)
        ]

    records = []
    for session in sessions:
        if session in observed or not _eligible(session, normalized):
            continue
        suspension = next((item for item in normalized.suspensions if item.contains(session)), None)
        if suspension is not None:
            classification = "expected_closure"
        elif _pending_publication(
            session,
            calendar_name=canonical_name,
            provider=provider,
            evidence=normalized,
            as_of=as_of,
        ):
            classification = "pending_publication"
        else:
            classification = "suspected_missing"
        observed_before = [item for item in observed if item < session]
        observed_after = [item for item in observed if item > session]
        records.append(
            _record(
                session,
                classification=classification,
                calendar_name=canonical_name,
                provider=provider,
                evidence=normalized,
                as_of=as_of,
                previous_session=max(observed_before, default=None),
                next_session=min(observed_after, default=None),
                calendar_available=True,
                suspension=suspension,
            )
        )
    return records


def classify_missing_session_gap(
    previous_session: date,
    next_session: date,
    *,
    calendar_name: str | None,
    provider: str,
    evidence: CalendarEvidence | Mapping[str, object] | None = None,
    as_of: date | None = None,
) -> list[dict]:
    """Classify only the bounded gap between two stored observations."""

    return classify_missing_sessions(
        {previous_session, next_session},
        calendar_name=calendar_name,
        provider=provider,
        evidence=evidence,
        as_of=as_of,
    )
