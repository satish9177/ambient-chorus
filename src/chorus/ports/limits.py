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

MAX_COMPILE_REQUESTED_FACTS = 100
MAX_COMPILE_REQUESTED_EVIDENCE = 20
MAX_COMPILER_GATES = 22
"""Bounds the compiler audit projection is sized against.

Restated here rather than imported because ports must not depend on the privacy package, and
asserted equal to ``chorus.privacy.policy``'s own constants by test. They exist so the largest
legal projection is a number this module can be reasoned about with, which is what makes the
400 KiB item-size proof a calculation rather than a hope.
"""

ORDINARY_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
SEND_IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 60 * 60
AUDIT_TTL_SECONDS = 90 * 24 * 60 * 60
