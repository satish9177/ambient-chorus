from __future__ import annotations

import io
import logging

from chorus.infrastructure.observability import ContentSafeJsonFormatter


def test_logging_private_sentinel_is_not_serialized() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(ContentSafeJsonFormatter())
    logger = logging.getLogger("test.content-safe")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "SECRET_SENTINEL_MOTHER_HEALTH",
        extra={
            "event_name": "privacy.sentinel",
            "service": "compiler",
            "reason_codes": ["SECRET_SENTINEL_MOTHER_HEALTH"],
        },
    )

    rendered = output.getvalue()
    assert "SECRET_SENTINEL_MOTHER_HEALTH" not in rendered
    assert '"event_name":"privacy.sentinel"' in rendered
    assert '"reason_codes":["REDACTED"]' in rendered
