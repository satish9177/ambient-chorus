"""The sender Lambda: one command, one outcome, and a failure taxonomy that survives the wire.

The send path's safety properties all live inside
:class:`~chorus.application.commands.send_action.SendAction`, so what is asserted here is that
crossing a Lambda boundary does not weaken any of them:

* the command carries **no recipient, subject, body, or retry flag** -- there is no field for
  one, and the recipient is resolved inside the sender from its own registry secret;
* ``SEND_UNKNOWN`` and ``SENDING`` travel as themselves, so the quarantine no path may retry
  stays a quarantine;
* an **ambiguous** persistence outcome is reconstructed as ambiguous, so the caller leaves the
  operation recoverable instead of settling a write that may have committed;
* a malformed request is refused before an execution is read, a fence is acquired, or SES is
  reachable.

Every client is a stub. No SES, no Secrets Manager, no DynamoDB, and no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from functions.envelope import InvocationFailedError
from functions.sender.handler import (
    ACCEPTED_OPERATIONS,
    CLOCK_UNAVAILABLE,
    MALFORMED_REQUEST,
    SenderComposition,
    run,
)

from chorus.application.commands.send_action import (
    SendActionCommand,
    SendActionResult,
    SendDeniedError,
    SendReplayOutcome,
)
from chorus.application.errors import ApplicationError, ApplicationErrorCode
from chorus.application.send_contract import (
    SEND_OPERATION,
    SEND_REQUEST_SCHEMA,
    RemoteSendAction,
    SendFailureKind,
    SendRequestError,
    decode_send_request,
    decode_send_result,
    encode_send_failure,
    encode_send_request,
)
from chorus.domain.entities import ActionExecutionState
from chorus.domain.errors import DomainError, DomainErrorCode
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    ExecutionId,
    Namespace,
    Sha256Digest,
)
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.infrastructure.secrets.destination_registry import (
    DESTINATION_REGISTRY_SCHEMA,
    DestinationRegistryUnavailableError,
    SecretsManagerDestinationRegistry,
    parse_registry_secret,
)
from chorus.ports.demo_clock import DemoClockRecord, DemoClockUnavailableError
from chorus.ports.errors import (
    ExternalDependencyError,
    PersistenceError,
    PersistenceErrorCode,
)

DIGEST = Sha256Digest(f"sha256:{'d' * 64}")
ROUTING_TOKEN = UUID("00000000-0000-4000-8000-000000000009")
FAKE_ADDRESS = "property-manager@chorus.invalid"
FAKE_FROM = "chorus@chorus.invalid"
SEED = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def command() -> SendActionCommand:
    return SendActionCommand(
        namespace=Namespace("DEMO"),
        community_id=CommunityId(UUID("11111111-1111-4111-8111-111111111111")),
        case_id=CaseId(UUID("22222222-2222-4222-8222-222222222222")),
        action_id=ActionId(UUID("33333333-3333-4333-8333-333333333333")),
        execution_id=ExecutionId(UUID("44444444-4444-4444-8444-444444444444")),
        approval_id=ApprovalId(UUID("55555555-5555-4555-8555-555555555555")),
        expected_execution_version=2,
        actor_id_hash=DIGEST,
        correlation_id=UUID("66666666-6666-4666-8666-666666666666"),
        idempotency_key="send-key-0001",
    )


def sent() -> SendActionResult:
    return SendActionResult(
        execution_id=command().execution_id,
        state=ActionExecutionState.SENT,
        version=3,
        ses_message_id="0100018a-fake-message-id",
        failure_code=None,
        reason_codes=("SEND_ACCEPTED",),
        ses_call_made=True,
    )


class StubSend:
    def __init__(self, answer: SendActionResult | None = None, error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.commands: list[SendActionCommand] = []

    async def execute(self, command: SendActionCommand) -> SendActionResult:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        assert self.answer is not None
        return self.answer


class StubClockStore:
    """P1: the sender now reads the authoritative clock once per invocation."""

    def __init__(self, *, instant: datetime | None = SEED) -> None:
        self.instant = instant
        self.reads = 0

    async def read(self) -> DemoClockRecord:
        self.reads += 1
        if self.instant is None:
            raise DemoClockUnavailableError("no clock row")
        return DemoClockRecord(
            logical_time=self.instant,
            version=1,
            reset_generation=1,
            seed_instant=SEED,
            advance_count=0,
        )

    async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
        raise AssertionError("the sender must never advance the clock")


def built(send: StubSend, clock: StubClockStore | None = None) -> SenderComposition:
    return SenderComposition(
        send_action=send,  # type: ignore[arg-type]
        clock_store=clock or StubClockStore(),
        scope=ScopedLogicalClock(),
    )


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {"operation": SEND_OPERATION, "payload": payload}


# -- the command boundary --------------------------------------------------------------------


def test_the_sender_answers_exactly_one_operation() -> None:
    assert {SEND_OPERATION} == ACCEPTED_OPERATIONS


def test_a_send_request_round_trips_field_for_field() -> None:
    original = command()
    payload = encode_send_request(original)

    assert payload["schema"] == SEND_REQUEST_SCHEMA
    assert decode_send_request(payload) == original


def test_the_request_carries_no_recipient_subject_or_body() -> None:
    """Asserted over the rendered payload, because the absence is the security property."""

    rendered = json.dumps(encode_send_request(command()))
    for forbidden in ("recipient", "address", "subject", "body", "template", "retry"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda body: body.__setitem__("schema", "send-action-request/v9"), id="schema"
        ),
        pytest.param(lambda body: body.pop("execution_id"), id="no-execution"),
        pytest.param(lambda body: body.__setitem__("expected_execution_version", 0), id="version"),
        pytest.param(lambda body: body.__setitem__("case_id", "not-a-uuid"), id="case"),
    ],
)
async def test_a_malformed_request_never_reaches_the_send_path(mutate: Any) -> None:
    body = encode_send_request(command())
    mutate(body)
    with pytest.raises(SendRequestError):
        decode_send_request(body)

    send = StubSend(sent())
    assert await run(envelope(body), built=send) == {  # type: ignore[arg-type]
        "status": "REFUSED",
        "reason_code": MALFORMED_REQUEST,
    }
    assert send.commands == []


async def test_an_unknown_operation_is_refused(anyio_backend: str) -> None:
    send = StubSend(sent())
    result = await run(
        {"operation": "SendAnything", "payload": encode_send_request(command())},
        built=send,  # type: ignore[arg-type]
    )
    assert result["status"] == "REFUSED"
    assert send.commands == []


# -- outcomes ----------------------------------------------------------------------------------


async def test_a_completed_send_returns_the_frozen_result() -> None:
    send = StubSend(sent())
    body = await run(envelope(encode_send_request(command())), built=built(send))

    assert send.commands == [command()]
    assert decode_send_result(body) == sent()


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(ActionExecutionState.SEND_UNKNOWN, id="send-unknown"),
        pytest.param(ActionExecutionState.SENDING, id="sending"),
        pytest.param(ActionExecutionState.FAILED, id="failed"),
    ],
)
async def test_every_terminal_state_travels_as_itself(state: ActionExecutionState) -> None:
    """``SEND_UNKNOWN`` is a quarantine no path may retry; it must not become "failed"."""

    outcome = SendActionResult(
        execution_id=command().execution_id,
        state=state,
        version=3,
        ses_message_id=None,
        failure_code="SES_UNREACHABLE",
        reason_codes=(),
        ses_call_made=state is not ActionExecutionState.FAILED,
    )
    body = await run(
        envelope(encode_send_request(command())),
        built=built(StubSend(outcome)),
    )
    assert decode_send_result(body).state is state


# -- the failure taxonomy ------------------------------------------------------------------------


async def test_a_denial_is_reconstructed_as_a_denial() -> None:
    """Terminal, and the caller must be able to see that rather than a generic failure."""

    denied = SendDeniedError(SendReplayOutcome.ALREADY_SENT, ActionExecutionState.SENT)
    body = await run(
        envelope(encode_send_request(command())),
        built=built(StubSend(error=denied)),
    )
    assert body["kind"] == SendFailureKind.DENIED.value

    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    with pytest.raises(SendDeniedError) as raised:
        await RemoteSendAction(invoker=Invoker()).execute(command())
    assert raised.value.state is ActionExecutionState.SENT


async def test_an_ambiguous_outcome_stays_ambiguous_across_the_wire() -> None:
    """The single most important line here: an unknown outcome must never settle as definite."""

    ambiguous = PersistenceError(PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "SEND")
    body = await run(
        envelope(encode_send_request(command())),
        built=built(StubSend(error=ambiguous)),
    )
    assert body["kind"] == SendFailureKind.UNKNOWN_OUTCOME.value

    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    with pytest.raises(PersistenceError) as raised:
        await RemoteSendAction(invoker=Invoker()).execute(command())
    assert raised.value.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        pytest.param(
            ApplicationError(ApplicationErrorCode.POLICY_DENIED),
            SendFailureKind.APPLICATION,
            id="application",
        ),
        pytest.param(
            DomainError(DomainErrorCode.VALIDATION_ERROR), SendFailureKind.DOMAIN, id="domain"
        ),
        pytest.param(
            PersistenceError(PersistenceErrorCode.PERSISTENCE_CONFLICT, "SEND"),
            SendFailureKind.PERSISTENCE,
            id="persistence",
        ),
    ],
)
def test_each_failure_family_keeps_its_own_kind(error: Exception, kind: SendFailureKind) -> None:
    assert encode_send_failure(error)["kind"] == kind.value


def test_an_unmapped_error_is_re_raised_rather_than_flattened() -> None:
    """A bug must surface as a bug, not as a settled send with an invented code."""

    with pytest.raises(RuntimeError):
        encode_send_failure(RuntimeError("something nobody classified"))


def test_a_failure_body_carries_no_message_and_no_traceback() -> None:
    body = encode_send_failure(
        SendDeniedError(SendReplayOutcome.ALREADY_SENT, ActionExecutionState.SENT)
    )
    assert set(body) == {"schema", "status", "kind", "code", "state"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"schema": "send-action-result/v9"}, id="wrong-schema"),
        pytest.param({"schema": "send-action-result/v1", "status": "INVENTED"}, id="bad-status"),
        pytest.param(
            {"schema": "send-action-result/v1", "status": "FAILED", "kind": "INVENTED"},
            id="unknown-kind",
        ),
        pytest.param(
            {"schema": "send-action-result/v1", "status": "COMPLETED"}, id="incomplete-result"
        ),
    ],
)
async def test_an_unparseable_answer_raises_rather_than_becoming_an_outcome(
    body: dict[str, Any],
) -> None:
    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    with pytest.raises(ExternalDependencyError):
        await RemoteSendAction(invoker=Invoker()).execute(command())


# -- the destination registry -----------------------------------------------------------------


def registry_secret() -> str:
    return json.dumps(
        {
            "schema": DESTINATION_REGISTRY_SCHEMA,
            "destination_id": "property_manager:demo",
            "kind": "PROPERTY_MANAGER",
            "registry_version": 1,
            "routing_token": str(ROUTING_TOKEN),
            "display_label": "Property Management",
            "address": FAKE_ADDRESS,
            "identity_id": "chorus-demo-sender",
            "from_address": FAKE_FROM,
            "reply_to_address": "chorus-replies@chorus.invalid",
            "identity_arn": "arn:aws:ses:us-east-1:000000000000:identity/chorus-demo-sender",
        }
    )


class FakeSecrets:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def get_secret_value(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return {"SecretString": self.payload}


async def test_the_registry_answers_only_for_the_exact_triple() -> None:
    """All three, and no near match: the triple is what the human's approval bound."""

    registry = SecretsManagerDestinationRegistry(
        client=FakeSecrets(registry_secret()), secret_id="arn:aws:secretsmanager:::secret:fake"
    )
    from chorus.domain.ids import DestinationId

    resolved = await registry.resolve_destination(
        destination_id=DestinationId("property_manager:demo"),
        registry_version=1,
        routing_token=ROUTING_TOKEN,
    )
    assert resolved.address == FAKE_ADDRESS

    # One wrong element out of the three is enough to refuse, every time.
    for destination_id, version, token in (
        (DestinationId("property_manager:demo"), 2, ROUTING_TOKEN),
        (DestinationId("property_manager:demo"), 1, uuid4()),
        (DestinationId("property_manager:other"), 1, ROUTING_TOKEN),
    ):
        with pytest.raises(DestinationRegistryUnavailableError):
            await registry.resolve_destination(
                destination_id=destination_id,
                registry_version=version,
                routing_token=token,
            )


def test_a_malformed_registry_secret_is_refused() -> None:
    for payload in ("not json", "[]", '{"schema": "destination-registry/v9"}'):
        with pytest.raises(DestinationRegistryUnavailableError):
            parse_registry_secret(payload)


async def test_no_registry_failure_message_contains_an_address() -> None:
    registry = SecretsManagerDestinationRegistry(
        client=FakeSecrets(registry_secret()), secret_id="arn:aws:secretsmanager:::secret:fake"
    )
    from chorus.domain.ids import DestinationId

    with pytest.raises(DestinationRegistryUnavailableError) as raised:
        await registry.resolve_destination(
            destination_id=DestinationId("property_manager:demo"),
            registry_version=99,
            routing_token=ROUTING_TOKEN,
        )
    rendered = f"{raised.value!r} {raised.value}"
    assert FAKE_ADDRESS not in rendered
    assert FAKE_FROM not in rendered


# -- P1: the sender reads the same authoritative clock, and stamps timestamps with it --------


async def test_the_sender_reads_the_clock_before_executing_the_send() -> None:
    send = StubSend(sent())
    clock = StubClockStore(instant=SEED)
    await run(envelope(encode_send_request(command())), built=built(send, clock))
    assert clock.reads == 1


async def test_the_bound_reading_is_what_the_send_use_case_observes() -> None:
    """Not just read -- actually bound, so ``SendAction.clock.now()`` returns the fetched
    reading rather than a value the send use case invented on its own."""

    captured: list[datetime] = []

    class ObservingSend:
        async def execute(self, command: SendActionCommand) -> SendActionResult:
            # The scope is bound by the time this runs; reading it here is exactly what
            # ``SendAction``'s own ``self.clock.now()`` calls do.
            captured.append(scope.now())
            return sent()

    scope = ScopedLogicalClock()
    graph = SenderComposition(
        send_action=ObservingSend(),  # type: ignore[arg-type]
        clock_store=StubClockStore(instant=SEED),
        scope=scope,
    )
    await run(envelope(encode_send_request(command())), built=graph)
    assert captured == [SEED]


# -- P2-6 / § 11: a clock outage fails the invocation, and never risks a duplicate send ------


async def test_an_unreadable_clock_fails_the_invocation_before_any_send_attempt() -> None:
    """Safe to raise here specifically because it happens before ``SendAction.execute()`` is
    ever called: no execution is claimed, no fence acquired, no SES call reachable."""

    send = StubSend(sent())
    with pytest.raises(InvocationFailedError) as raised:
        await run(
            envelope(encode_send_request(command())),
            built=built(send, StubClockStore(instant=None)),
        )
    assert str(raised.value) == CLOCK_UNAVAILABLE
    assert send.commands == []


async def test_a_clock_sdk_error_also_fails_the_invocation_not_the_send() -> None:
    class ExplodingClockStore:
        async def read(self) -> DemoClockRecord:
            raise DemoClockUnavailableError("throttled")

        async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
            raise AssertionError("the sender must never advance the clock")

    send = StubSend(sent())
    with pytest.raises(InvocationFailedError):
        await run(
            envelope(encode_send_request(command())),
            built=built(send, ExplodingClockStore()),  # type: ignore[arg-type]
        )
    assert send.commands == []


async def test_a_clock_outage_never_produces_a_retry_triggering_send_unknown() -> None:
    """The two failure modes must stay distinct: a clock outage before any attempt is a
    retryable *invocation* failure, and ``SEND_UNKNOWN`` -- an ambiguous outcome *after* an
    attempt -- must never be raised in a way that could trigger an automatic resend.

    This asserts the second half directly: when the send path itself reports the ambiguous
    outcome (as a normal *result*, not raised), the handler returns it as data. Nothing here
    converts a settled ``SEND_UNKNOWN`` execution result into an
    :class:`~functions.envelope.InvocationFailedError`.
    """

    quarantined = SendActionResult(
        execution_id=command().execution_id,
        state=ActionExecutionState.SEND_UNKNOWN,
        version=3,
        ses_message_id=None,
        failure_code="SES_READ_TIMEOUT",
        reason_codes=(),
        ses_call_made=True,
    )
    body = await run(envelope(encode_send_request(command())), built=built(StubSend(quarantined)))
    assert decode_send_result(body).state is ActionExecutionState.SEND_UNKNOWN
