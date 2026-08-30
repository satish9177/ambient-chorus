from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from chorus.domain.ids import CaseId, FactId, SensitiveStr, Sha256Digest
from chorus.domain.time import format_utc, parse_utc, require_utc


def test_nominal_ids_with_same_uuid_are_not_equal() -> None:
    value = UUID("1d6a75df-e1bd-5e42-9605-4e7d80fd68e1")
    case_id: object = CaseId(value)
    fact_id: object = FactId(value)

    assert case_id != fact_id


def test_sensitive_string_repr_and_str_are_redacted() -> None:
    private = SensitiveStr("SECRET_SENTINEL_PRIVATE")

    assert "SECRET_SENTINEL_PRIVATE" not in repr(private)
    assert str(private) == "***"
    assert private.reveal() == "SECRET_SENTINEL_PRIVATE"


def test_utc_round_trip_has_exactly_six_fractional_digits() -> None:
    instant = datetime(2026, 8, 30, 12, 34, 56, 123, tzinfo=UTC)

    rendered = format_utc(instant)

    assert rendered == "2026-08-30T12:34:56.000123Z"
    assert parse_utc(rendered) == instant


def test_non_utc_and_noncanonical_time_fail() -> None:
    with pytest.raises(ValueError, match="UTC"):
        require_utc(datetime(2026, 8, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))))
    with pytest.raises(ValueError, match="six fractional"):
        parse_utc("2026-08-30T12:34:56.1Z")


def test_digest_requires_frozen_prefix_and_lowercase_hex() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        Sha256Digest("A" * 64)
