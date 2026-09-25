"""DST-aware local timezone resolution for dashboard time calculations."""

from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _timezone_name_from_system() -> str | None:
    for variable in ("AI_USAGE_TIMEZONE", "TZ"):
        value = os.environ.get(variable, "").strip()
        if value:
            return value

    localtime = Path("/etc/localtime")
    try:
        resolved = str(localtime.resolve())
        marker = "zoneinfo/"
        if marker in resolved:
            return resolved.split(marker, 1)[1]
    except OSError:
        pass

    try:
        value = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return None


def local_timezone_name() -> str:
    """Return the configured IANA timezone name, falling back to UTC."""
    name = _timezone_name_from_system()
    if name:
        try:
            ZoneInfo(name)
            return name
        except ZoneInfoNotFoundError:
            pass
    return "UTC"


def local_timezone() -> tzinfo:
    """Return a DST-aware timezone for the local dashboard process."""
    return ZoneInfo(local_timezone_name())


def timezone_name(value: tzinfo | None) -> str:
    """Return a stable API label for a timezone-aware value."""
    if value is None:
        return "UTC"
    key = getattr(value, "key", None)
    if isinstance(key, str) and key:
        return key
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    current = datetime.now(value)
    return current.tzname() or str(value) or "UTC"


def attach_timezone(value: datetime) -> datetime:
    """Attach the dashboard's DST-aware local timezone to a naive datetime."""
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=local_timezone())


def as_utc(value: datetime) -> datetime:
    """Convert aware or dashboard-local naive datetimes to UTC."""
    if value.tzinfo is None:
        value = attach_timezone(value)
    return value.astimezone(timezone.utc)
