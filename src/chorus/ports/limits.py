"""Frozen V1 persistence bounds.

These bounds exist so a single case aggregate can always be mutated inside DynamoDB's
transaction and item limits. They are hard caps, never defaults to be raised locally.
"""

from __future__ import annotations

MAX_ACTIVE_FACTS_PER_CASE = 100
MAX_VIEWS_PER_CASE = 25
MAX_ACTIONS_PER_CASE = 10
MAX_COMMITMENTS_PER_CASE = 20

TRANSACTION_MAX_OPERATIONS = 100
BATCH_GET_MAX_KEYS = 100
MAX_PAGE_SIZE = 100

TRANSACTION_TOKEN_WINDOW_SECONDS = 10 * 60
"""How long DynamoDB treats a repeated ``ClientRequestToken`` as the same request.

Documented here because adapter behaviour depends on it: after this window a re-sent
transaction is a genuinely new request, and only the plan's own conditions still prevent it
from applying twice.
"""

ORDINARY_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
SEND_IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 60 * 60
AUDIT_TTL_SECONDS = 90 * 24 * 60 * 60
