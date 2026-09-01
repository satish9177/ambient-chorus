"""One storage operation is one wire attempt, and ambiguity survives to the caller.

Every other transaction test in this suite injects an already-classified CHORUS error above
the storage port. That is precisely the blind spot this file exists to close: botocore sits
*below* the port, and left at its defaults it applies DynamoDB's ``legacy`` policy of up to
ten attempts per call. A retry there is invisible above the port, and only the last attempt's
exception survives -- so a connect failure on attempt two would be read as proof that
attempt one never reached the service.

These tests therefore run the real chain:

``create_dynamodb_client`` -> ``DynamoDbStorageDriver`` -> botocore -> ``StorageUnitOfWork``

Faults are raised from a ``before-send`` handler, which is the last botocore hook before the
request leaves the client, so the retry layer is genuinely exercised rather than bypassed.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
from tests.contract.persistence.conftest import WireHarness
from tests.fixtures.persistence import NOW, PRIMARY, build_repositories, digest

from chorus.infrastructure.dynamodb.client import SINGLE_ATTEMPT_RETRIES
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.ports.errors import (
    ExternalDependencyError,
    PersistenceErrorCode,
    UnknownTransactionOutcomeError,
)
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

REQUEST_HASH = digest("wire-attempt-request")
TRANSACT = "TransactWriteItems"
GET_ITEM = "GetItem"
LOST_RESPONSE = ReadTimeoutError(endpoint_url="http://endpoint.invalid")
NEVER_CONNECTED = ConnectTimeoutError(endpoint_url="http://endpoint.invalid")


def audited_plan(harness: WireHarness) -> TransactionPlan:
    """A realistic audited case mutation carrying its own commit proof."""

    repositories = build_repositories(harness.driver)
    key = PRIMARY.idempotency_key()
    return TransactionPlan(
        name="wire-attempt",
        operations=(
            repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case()),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
            repositories.idempotency.stage_create_completed(
                key,
                request_hash=REQUEST_HASH,
                result_entity_refs=(),
                response_status=200,
                now=NOW,
            ),
        ),
        audit_required=True,
        commit_proof=repositories.idempotency.commit_proof(key, request_hash=REQUEST_HASH),
    )


def script(
    harness: WireHarness,
    *,
    transacts: tuple[Exception, ...] = (),
    reads: tuple[Exception, ...] = (),
) -> list[str]:
    """Script faults per outgoing request; return the client request tokens actually sent.

    A fault list shorter than the number of requests lets the remainder through, so a test
    can say "the first attempt is lost, anything after it succeeds" and then assert how many
    requests there really were. Tokens are read off the wire rather than from the plan,
    because the driver binds the plan's token to the rendered request before sending it.
    """

    tokens: list[str] = []
    pending_transacts = list(transacts)
    pending_reads = list(reads)

    def handler(request: Any, **_: object) -> None:
        target = request.headers.get("X-Amz-Target", "")
        target = target.decode() if isinstance(target, bytes) else str(target)
        if target.endswith(GET_ITEM):
            if pending_reads:
                raise pending_reads.pop(0)
            return None
        if not target.endswith(TRANSACT):
            return None
        body = request.body
        payload = json.loads(body.decode() if isinstance(body, bytes) else body)
        tokens.append(payload["ClientRequestToken"])
        if pending_transacts:
            raise pending_transacts.pop(0)
        return None

    harness.client.meta.events.register_first("before-send.dynamodb", handler)
    return tokens


def test_the_client_is_pinned_to_a_single_request_attempt(wire: WireHarness) -> None:
    """``total_max_attempts`` counts the initial request, so one means no retry at all."""

    assert SINGLE_ATTEMPT_RETRIES == {"mode": "standard", "total_max_attempts": 1}
    assert wire.client.meta.config.retries["total_max_attempts"] == 1
    assert wire.client.meta.config.retries["mode"] == "standard"


def test_the_pinned_policy_beats_an_ambient_retry_environment(
    wire: WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's ``AWS_MAX_ATTEMPTS`` must not reintroduce a hidden attempt."""

    from chorus.infrastructure.dynamodb.client import create_dynamodb_client

    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "10")
    monkeypatch.setenv("AWS_RETRY_MODE", "legacy")
    client: Any = create_dynamodb_client(region_name="us-east-1", endpoint_url=None)

    assert client.meta.config.retries["total_max_attempts"] == 1
    assert client.meta.config.retries["mode"] == "standard"


async def test_one_transaction_call_makes_exactly_one_wire_attempt(wire: WireHarness) -> None:
    plan = audited_plan(wire)

    await StorageUnitOfWork(driver=wire.driver).commit(plan)

    assert wire.transact_attempts() == 1


async def test_a_lost_response_makes_no_second_wire_attempt(wire: WireHarness) -> None:
    """The fault that used to be followed by nine more attempts now ends the call."""

    script(wire, transacts=(LOST_RESPONSE,))
    plan = audited_plan(wire)

    with pytest.raises(UnknownTransactionOutcomeError):
        await wire.driver.transact_write(
            plan.operations, client_request_token=plan.client_request_token
        )

    assert wire.transact_attempts() == 1


async def test_an_unresolvable_lost_response_stays_unknown_and_never_retryable(
    wire: WireHarness,
) -> None:
    """A request that may have reached the service, and cannot be resolved, stays ambiguous.

    This is the property the whole classification exists for: ``retryable`` must never mean
    "this may already have run". The transaction's response is lost and the commit proof
    cannot be read, so there is no evidence either way and nothing is attempted again.
    """

    script(wire, transacts=(LOST_RESPONSE,), reads=(LOST_RESPONSE,))
    plan = audited_plan(wire)

    with pytest.raises(ExternalDependencyError) as raised:
        await StorageUnitOfWork(driver=wire.driver).commit(plan)

    assert isinstance(raised.value, UnknownTransactionOutcomeError)
    assert raised.value.retryable is False
    assert raised.value.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME
    assert wire.transact_attempts() == 1


async def test_a_connect_failure_can_only_follow_a_proven_non_commit(
    wire: WireHarness,
) -> None:
    """The regression this file exists for, scripted exactly as it used to happen.

    The first transaction loses its response, so it may have committed. A connect failure is
    classified as a definite non-commit, and it used to be able to land on a hidden SDK retry
    of *that same call* -- erasing the ambiguity of an attempt that had reached the service.

    Now the only way a second transaction happens is the licensed retry, and that retry
    exists only because the strong commit-proof read proved the first one did not commit. The
    connect failure therefore describes a request that genuinely never reached the service,
    which is the one situation in which reporting it as retryable is true.
    """

    script(wire, transacts=(LOST_RESPONSE, NEVER_CONNECTED))
    plan = audited_plan(wire)

    with pytest.raises(ExternalDependencyError) as raised:
        await StorageUnitOfWork(driver=wire.driver).commit(plan)

    # Two transactions, one wire attempt each, with the proof read between them.
    assert wire.transact_attempts() == 2
    assert wire.attempts.count(f"DynamoDB_20120810.{GET_ITEM}") == 1
    assert wire.attempts.index(f"DynamoDB_20120810.{GET_ITEM}") == 1
    assert raised.value.retryable is True
    assert not isinstance(raised.value, UnknownTransactionOutcomeError)


async def test_commit_proof_resolution_drives_the_single_licensed_retry(
    wire: WireHarness,
) -> None:
    """A proven non-commit licenses exactly one retry, and each attempt is one wire call."""

    tokens = script(wire, transacts=(LOST_RESPONSE,))
    plan = audited_plan(wire)
    repositories = build_repositories(wire.driver)

    await StorageUnitOfWork(driver=wire.driver).commit(plan)

    # One lost attempt plus one licensed retry, and no attempt beyond them.
    assert wire.transact_attempts() == 2
    record = await repositories.idempotency.load(PRIMARY.idempotency_key())
    assert record is not None
    assert record.request_hash == REQUEST_HASH
    # The retry is the same request under the same token, so DynamoDB's own ten-minute
    # window can still absorb it. Pinning the retry policy must not disturb that.
    assert len(tokens) == 2
    assert tokens[0] == tokens[1]


async def test_commit_proof_resolution_suppresses_the_retry_when_the_write_committed(
    wire: WireHarness,
) -> None:
    """A proof found by the strong read ends the command instead of writing twice."""

    plan = audited_plan(wire)
    repositories = build_repositories(wire.driver)
    await wire.driver.write_item(
        repositories.idempotency.stage_create_completed(
            PRIMARY.idempotency_key(),
            request_hash=REQUEST_HASH,
            result_entity_refs=(),
            response_status=200,
            now=NOW,
        )
    )
    script(wire, transacts=(LOST_RESPONSE,))

    await StorageUnitOfWork(driver=wire.driver).commit(plan)

    assert wire.transact_attempts() == 1


async def test_the_client_request_token_survives_the_pinned_policy(
    wire: WireHarness,
) -> None:
    """Pinning retries must not disturb the token a plan derives from its own content."""

    tokens = script(wire)
    plan = audited_plan(wire)
    rebuilt = audited_plan(wire)

    await StorageUnitOfWork(driver=wire.driver).commit(plan)

    assert rebuilt.client_request_token == plan.client_request_token
    assert len(tokens) == 1
    assert UUID(tokens[0]).version == 4
