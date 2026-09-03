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
    "namespace",
    "actor_type",
    "actor_id_hash",
    "community_id",
    "case_id",
    "case_version",
    "operation_id",
    "invocation_id",
    "entity_type",
    "entity_id",
    "entity_version",
    "input_hash",
    "output_hash",
    "policy_version",
    "prompt_version",
    "outcome",
    "reason_codes",
    "counts",
    "duration_ms",
    "attempt",
    "retryable",
)
"""The allowlist, extended for the agent-invocation events Phase 3 emits.

Every added field is an identifier, a digest, a version, or a count. There is deliberately no
field for a message, a summary, a prompt, a completion, a quotation, or an exception message:
a value that is not on this list is not truncated or hashed, it is simply never written.
"""
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_FORBIDDEN_TEXT = re.compile(r"(?i)(?:secret|private|health|email|unit|apartment|contact)")


def _is_safe_text(value: str) -> bool:
    return _SAFE_TEXT.fullmatch(value) is not None and _FORBIDDEN_TEXT.search(value) is None


def _safe_log_value(field_name: str, value: object) -> object:
    if field_name in _NUMERIC_FIELDS:
        return value if isinstance(value, int) and value >= 0 else "REDACTED"
    if field_name == "retryable":
        return value if isinstance(value, bool) else "REDACTED"
    if field_name == "counts":
        return _safe_counts(value)
    if field_name == "reason_codes":
        if not isinstance(value, tuple | list) or len(value) > 20:
            return ["REDACTED"]
        rendered_codes = [str(item) for item in value]
        return (
            rendered_codes if all(_is_safe_text(item) for item in rendered_codes) else ["REDACTED"]
        )
    rendered_value = str(value)
    return rendered_value if _is_safe_text(rendered_value) else "REDACTED"


_NUMERIC_FIELDS: Final[frozenset[str]] = frozenset(
    {"duration_ms", "attempt", "case_version", "entity_version"}
)

_MAX_COUNT_KEYS: Final = 20


def _safe_counts(value: object) -> object:
    """Render a bounded mapping of non-negative integer counts, or nothing at all.

    Counts are the one structured field an event may carry, so the shape is checked rather
    than trusted: string keys that look like identifiers, integer values, and a hard bound on
    how many of them there are.
    """

    if not isinstance(value, dict) or len(value) > _MAX_COUNT_KEYS:
        return "REDACTED"
    rendered: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not _is_safe_text(key):
            return "REDACTED"
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return "REDACTED"
        rendered[key] = count
    return rendered


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
