"""Allowlisted JSON logging that never serializes arbitrary message content."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Final

_SAFE_RECORD_FIELDS: Final[tuple[str, ...]] = (
    "service",
    "environment",
    "event_name",
    "correlation_id",
    "causation_id",
    "outcome",
    "reason_codes",
    "duration_ms",
    "retryable",
)
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_FORBIDDEN_TEXT = re.compile(r"(?i)(?:secret|private|health|email|unit|apartment|contact)")


def _is_safe_text(value: str) -> bool:
    return _SAFE_TEXT.fullmatch(value) is not None and _FORBIDDEN_TEXT.search(value) is None


def _safe_log_value(field_name: str, value: object) -> object:
    if field_name in {"duration_ms"}:
        return value if isinstance(value, int) and value >= 0 else "REDACTED"
    if field_name == "retryable":
        return value if isinstance(value, bool) else "REDACTED"
    if field_name == "reason_codes":
        if not isinstance(value, tuple | list) or len(value) > 20:
            return ["REDACTED"]
        rendered_codes = [str(item) for item in value]
        return (
            rendered_codes if all(_is_safe_text(item) for item in rendered_codes) else ["REDACTED"]
        )
    rendered_value = str(value)
    return rendered_value if _is_safe_text(rendered_value) else "REDACTED"


class ContentSafeJsonFormatter(logging.Formatter):
    """Serialize only the bounded observability fields defined by the architecture."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
        }
        for field_name in _SAFE_RECORD_FIELDS:
            if hasattr(record, field_name):
                payload[field_name] = _safe_log_value(field_name, getattr(record, field_name))
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception_class"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
