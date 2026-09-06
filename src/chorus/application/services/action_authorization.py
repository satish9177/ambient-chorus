"""The frozen derivations every Phase-8 command shares: expiry, the send key, six domains.

Everything here is a pure function of durable values. Nothing reads a clock it was not handed,
nothing touches storage, and nothing decides policy -- so a recovery path can recompute any of
it from the row it already has, which is the whole reason these are derivations rather than
stored fields.

The six idempotency domains are the part worth reading twice. They are **separate namespaces**
because they are commit proofs for different things, and a single reused row would make one
outcome answer for another: a completed send-claim record would look like proof that the
*result* had been persisted. Domains 4, 5, and 6 key on the execution's own ``idempotency_key``
rather than on a client key, which is what makes them replay-safe regardless of how the worker
was invoked or how many times -- they identify the *attempt*, not the request that asked for it
([ADR-025](../../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md) SS 12).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from chorus.domain.entities import ApprovalDecision
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    ExecutionId,
    Namespace,
    Sha256Digest,
)
from chorus.ports.idempotency import (
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.privacy.canonical import hash_value

APPROVAL_LIFETIME = timedelta(minutes=15)
"""How long a human decision authorizes a send, before the view's own expiry is applied.

The effective expiry is ``min(approved_at + 15 minutes, view.expires_at)``, so an approval
never outlives the disclosure authority it was made against. Equality at expiry means expired,
as it does everywhere else in this system.
"""

SEND_FENCE_LIFETIME = timedelta(seconds=60)
"""The fence's maximum life, and therefore the recovery window for a ``SENDING`` row."""

MIN_SEND_FENCE_WINDOW = timedelta(seconds=5)
"""Fewer than five seconds of remaining authority denies rather than races.

A fence granted with two seconds left would authorize a send whose SES call is still in flight
when the authority behind it lapses. Refusing is the answer that cannot produce a message
nobody was authorized to send.
"""

SEND_RECOVERY_WINDOW = SEND_FENCE_LIFETIME
"""A ``SENDING`` row is reconcilable only after the fence's maximum life has passed.

Deliberately the same number, and derived rather than re-declared: the window exists because
after it no fence this attempt could have held is still live, so a second value here would be a
second answer to one question.
"""

APPROVAL_REQUEST_SCHEMA = "approve-action-http-request/v1"
INVALIDATION_REQUEST_SCHEMA = "invalidate-action-http-request/v1"
SEND_REQUEST_SCHEMA = "send-action-http-request/v1"
EXECUTION_SEND_KEY_SCHEMA = "action-execution-send-key/v1"


def approval_expires_at(*, approved_at: datetime, view_expires_at: datetime) -> datetime:
    """``min(approved_at + 15 minutes, view.expires_at)``, computed in exactly one place."""

    return min(approved_at + APPROVAL_LIFETIME, view_expires_at)


def execution_send_key(
    *,
    namespace: Namespace,
    action_id: ActionId,
    execution_id: ExecutionId,
    proposal_hash: Sha256Digest,
    view_hash: Sha256Digest,
    approval_id: ApprovalId,
) -> str:
    """The frozen execution send idempotency key.

    ``sha256(namespace | action_id | execution_id | proposal_hash | view_hash | approval_id)``.

    It depends on the approval, which is exactly why a ``DRAFT`` execution cannot carry one:
    the key is defined *over* the decision, so before a decision exists there is nothing to
    derive it from. The presence table records that as ``ABSENT`` at ``DRAFT`` rather than as
    an oversight.

    Returned as the bare digest string because it is stored in ``ActionExecution`` as an opaque
    key and consumed by the three send-domain key hashes below.
    """

    return hash_value(
        {
            "schema": EXECUTION_SEND_KEY_SCHEMA,
            "namespace": namespace.value,
            "action_id": str(action_id),
            "execution_id": str(execution_id),
            "proposal_hash": proposal_hash.value,
            "view_hash": view_hash.value,
            "approval_id": str(approval_id),
        }
    ).value


def _domain_key_hash(domain: str, value: str) -> Sha256Digest:
    """Hash one domain-separated key. ``\\x1f`` is the frozen separator.

    A unit separator rather than a colon or a hyphen, because it cannot occur in a client key
    (the transport pattern admits printable ASCII only) and therefore cannot be used to make
    two different domains collide by choosing a key that contains the delimiter.
    """

    return hash_value({"schema": "action-command-key/v1", "key": f"{domain}\x1f{value}"})


def approval_start_key_hash(client_key: str) -> Sha256Digest:
    """Domain 1: the HTTP approval request."""

    return _domain_key_hash("approve-action-start", client_key)


def approval_transaction_key_hash(client_key: str) -> Sha256Digest:
    """Domain 2: the approval transaction's own commit proof."""

    return _domain_key_hash("approve-action", client_key)


def send_start_key_hash(client_key: str) -> Sha256Digest:
    """Domain 3: the send-operation start reservation."""

    return _domain_key_hash("send-action-start", client_key)


def send_claim_key_hash(execution_key: str) -> Sha256Digest:
    """Domain 4: the pre-send claim, keyed on the attempt rather than on the request."""

    return _domain_key_hash("send-claim", execution_key)


def send_result_key_hash(execution_key: str) -> Sha256Digest:
    """Domain 5: the terminal send result."""

    return _domain_key_hash("send-result", execution_key)


def send_projection_key_hash(execution_key: str) -> Sha256Digest:
    """Domain 6: recovery and the case projection to ``ACTIONED``."""

    return _domain_key_hash("send-projection", execution_key)


def invalidation_key_hash(client_key: str) -> Sha256Digest:
    """The invalidation command's own separator, keyed on the caller's key.

    Withdrawal and clearing are not one of ADR-025 SS 12's six domains, because they are not a
    send -- but they live in the same ``SEND_ACTION`` family and the same ``EXECUTION``
    partition, so reusing the send-start separator would let one client key address two
    semantically different commands. A separate separator is the only reading of "do not reuse
    one idempotency key across semantically different commands" that holds here.

    It is keyed on the *client* key rather than on the execution's, because an invalidation is
    a request a human made, not an attempt the system is making: two withdrawals of one
    execution under different keys are two commands, and the second one correctly finds the row
    already moved.
    """

    return _domain_key_hash("invalidate-action", client_key)


def action_partition(namespace: Namespace, action_id: ActionId) -> IdempotencyPartition:
    """The proposal's own partition, where the two approval domains live."""

    return IdempotencyPartition(
        kind=IdempotencyPartitionKind.ACTION, namespace=namespace, action_id=action_id
    )


def execution_partition(namespace: Namespace, action_id: ActionId) -> IdempotencyPartition:
    """The execution's own partition, where the three send domains live (ADR-024)."""

    return IdempotencyPartition(
        kind=IdempotencyPartitionKind.EXECUTION, namespace=namespace, action_id=action_id
    )


def approval_key(
    *,
    namespace: Namespace,
    action_id: ActionId,
    actor_id_hash: Sha256Digest,
    key_hash: Sha256Digest,
) -> IdempotencyKey:
    return IdempotencyKey(
        partition=action_partition(namespace, action_id),
        command=IdempotentCommand.APPROVE_ACTION,
        actor_id_hash=actor_id_hash,
        key_hash=key_hash,
    )


def send_key(
    *,
    namespace: Namespace,
    action_id: ActionId,
    actor_id_hash: Sha256Digest,
    key_hash: Sha256Digest,
) -> IdempotencyKey:
    return IdempotencyKey(
        partition=execution_partition(namespace, action_id),
        command=IdempotentCommand.SEND_ACTION,
        actor_id_hash=actor_id_hash,
        key_hash=key_hash,
    )


def approval_request_hash(
    *,
    case_id: CaseId,
    action_id: ActionId,
    execution_id: ExecutionId,
    decision: ApprovalDecision,
    expected_execution_version: int,
    view_hash: Sha256Digest,
    proposal_hash: Sha256Digest,
    preview_hash: Sha256Digest,
) -> Sha256Digest:
    """Everything the approver chose, so two different decisions never share one key.

    ``decision`` is inside it deliberately. An approval and a rejection of the same proposal
    under one ``Idempotency-Key`` are two different commands, and answering the second with the
    first's recorded outcome would silently convert a human's "no" into a "yes".
    """

    if expected_execution_version < 1:
        raise ValueError("expected_execution_version must be positive")
    return hash_value(
        {
            "schema": APPROVAL_REQUEST_SCHEMA,
            "case_id": str(case_id),
            "action_id": str(action_id),
            "execution_id": str(execution_id),
            "decision": decision.value,
            "expected_execution_version": expected_execution_version,
            "view_hash": view_hash.value,
            "proposal_hash": proposal_hash.value,
            "preview_hash": preview_hash.value,
        }
    )


def invalidation_request_hash(
    *,
    case_id: CaseId,
    action_id: ActionId,
    expected_execution_version: int,
    proposal_hash: Sha256Digest,
) -> Sha256Digest:
    """The withdrawal/clearing request's identity, over exactly the frozen body."""

    if expected_execution_version < 1:
        raise ValueError("expected_execution_version must be positive")
    return hash_value(
        {
            "schema": INVALIDATION_REQUEST_SCHEMA,
            "case_id": str(case_id),
            "action_id": str(action_id),
            "expected_execution_version": expected_execution_version,
            "proposal_hash": proposal_hash.value,
        }
    )


def send_request_hash(
    *,
    case_id: CaseId,
    action_id: ActionId,
    execution_id: ExecutionId,
    approval_id: ApprovalId,
    expected_execution_version: int,
) -> Sha256Digest:
    """The send request's identity, over exactly the frozen execute body."""

    if expected_execution_version < 1:
        raise ValueError("expected_execution_version must be positive")
    return hash_value(
        {
            "schema": SEND_REQUEST_SCHEMA,
            "case_id": str(case_id),
            "action_id": str(action_id),
            "execution_id": str(execution_id),
            "approval_id": str(approval_id),
            "expected_execution_version": expected_execution_version,
        }
    )


__all__ = [
    "APPROVAL_LIFETIME",
    "MIN_SEND_FENCE_WINDOW",
    "SEND_FENCE_LIFETIME",
    "SEND_RECOVERY_WINDOW",
    "action_partition",
    "approval_expires_at",
    "approval_key",
    "approval_request_hash",
    "approval_start_key_hash",
    "approval_transaction_key_hash",
    "execution_partition",
    "execution_send_key",
    "invalidation_key_hash",
    "invalidation_request_hash",
    "send_claim_key_hash",
    "send_key",
    "send_projection_key_hash",
    "send_request_hash",
    "send_result_key_hash",
    "send_start_key_hash",
]
