"""The sender Lambda's entry point: one command in, one durable outcome out.

The sender is the narrowest principal that touches the outside world. It holds a **total Core
deny**, the only ``sesv2:SendEmail`` grant, and the destination registry secret
([ADR-024](../../../docs/adr/ADR-024-execution-partition-and-sender-boundary.md) § 3), and it
gets asked exactly one question: run this approved send.

What is preserved here, unchanged
----------------------------------
Everything that makes a send safe already lives inside
:class:`~chorus.application.commands.send_action.SendAction` and the objects it is composed
with, and this handler adds nothing to any of it:

* the ``APPROVED@v -> SENDING@v+1`` compare-and-swap that permits an SES call from exactly one
  state, which is why a duplicate delivery makes **zero** SES calls;
* the compiler-side send authorization, reached through ``lambda:InvokeFunction`` on the
  compiler ARN because this principal cannot read Core at all;
* the ``SEND_UNKNOWN`` quarantine -- an ambiguous outcome that no path may ever retry -- and the
  ``SENDING`` refusal beside it;
* the immutable ``OUTBOUND_MESSAGE`` locator, written by the worker's projection and never here;
* exactly **one** deliberate attempt, pinned at the SDK by ``SINGLE_ATTEMPT_CLIENT_CONFIG``.

**No model is called and no private evidence is read.** The composition constructs no agent
client, no Bedrock client, no scheduler, and no Core repository, and an import-linter contract
forbids Strands, the agent contracts, and the API package from this package outright.

The recipient does not come from the payload
---------------------------------------------
:class:`~chorus.application.commands.send_action.SendActionCommand` has no field for a
recipient, a subject, or a body, and the only place an address exists in this process is the
destination registry resolved from the sender's own secret -- which answers for one exact
``(destination_id, registry_version, routing_token)`` triple and refuses every near match. So an
invocation payload cannot choose who receives an approved message.

The clock (P1, Phase 11 batch 4 repair)
-----------------------------------------
The sender no longer runs on :class:`~chorus.domain.time.SystemClock`. Its own business
timestamps -- the execution's ``started_at``, the send-authorization request's
``requested_at``, and the ``_expired()`` comparison against the fence's ``expires_at`` the
compiler computed -- must agree with the same authoritative logical clock the compiled view,
the proposal, and the approval are stamped against, or a fence acquired the instant after the
demo clock was advanced would be judged against a clock that never moved. No new IAM grant is
needed: ``ReadShareable`` is already an unrestricted read of the whole Shareable table
(deployment contract § 8's sender row), so it already reaches ``NS#DEMO#CLOCK``; what is new is
reading it, strongly, once per invocation, and binding it exactly as the watcher, the worker,
and the compiler do. A clock that cannot be read fails the *invocation* -- raised as
:class:`~functions.envelope.InvocationFailedError` -- and it does so **before** any execution
is claimed or any SES call is reachable, so the ``SEND_UNKNOWN``/``SENDING`` quarantine this
module exists to protect is never at risk from it (P2-6, § 11).

**Cold start touches no network.** The graph is built lazily on the first invocation; boto3
clients are constructed without a credential lookup or a request.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Final

import anyio

from chorus.application.commands.send_action import SendAction
from chorus.application.send_contract import (
    SEND_OPERATION,
    SendRequestError,
    decode_send_request,
    encode_send_failure,
    encode_send_result,
)
from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId, Namespace
from chorus.infrastructure.dynamodb.demo_clock import DynamoDbDemoClockStore
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.infrastructure.secrets.demo_access import create_secrets_client
from chorus.infrastructure.secrets.destination_registry import (
    SecretsManagerDestinationRegistry,
)
from chorus.ports.demo_clock import DemoClockError, DemoClockStorePort
from chorus.ports.records import StoredSafeDestination
from chorus.ports.sender import DestinationRegistryPort
from chorus.settings import Settings
from functions.envelope import EnvelopeError, InvocationFailedError, failure, read_envelope
from functions.sender.composition import SenderSettings, build_send_action, build_sender_driver

ACCEPTED_OPERATIONS: Final = frozenset({SEND_OPERATION})
"""The sender answers exactly one question. There is no second operation to name."""

MALFORMED_REQUEST: Final = "MALFORMED_REQUEST"
CLOCK_UNAVAILABLE: Final = "CLOCK_UNAVAILABLE"
"""The reason named in the raised :class:`~functions.envelope.InvocationFailedError`. Reached
strictly before an execution is claimed, so this is always safe to retry (§ 11)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SenderComposition:
    """The send use case, plus the clock it is stamped against (P1)."""

    send_action: SendAction
    clock_store: DemoClockStorePort
    scope: ScopedLogicalClock


_composition: SenderComposition | None = None


def sender_settings(settings: Settings) -> SenderSettings:
    """Map process configuration onto the sender's own settings, and nothing wider.

    ``outbox_directory`` is deliberately absent, which is what selects the **deployed**
    composition: the live SESv2 client, and the compiler-invocation send authority rather than
    the in-process one that would need a Core handle this role is denied.
    """

    return SenderSettings(
        region=settings.aws_region,
        namespace=settings.namespace,
        core_table=settings.core_table,
        shareable_table=settings.shareable_table,
        audit_table=settings.audit_table,
        destination=StoredSafeDestination(
            destination_id=DestinationId(settings.destination_id),
            kind=DestinationKind.PROPERTY_MANAGER,
            registry_version=settings.destination_registry_version,
            routing_token=settings.destination_routing_token,
            display_label=settings.destination_display_label,
        ),
        from_identity_id=settings.ses_from_identity_id,
        ses_configuration_set=settings.ses_configuration_set,
        cursor_secret=secrets.token_bytes(32),
        compiler_function_arn=settings.compiler_function_arn,
    )


def build_registry(settings: Settings) -> DestinationRegistryPort:
    """The one secret this principal reads, and the only place an address exists.

    Refused rather than defaulted when unconfigured: a deployed sender with no registry has no
    recipient it may resolve, and that has to fail at construction rather than at the first send
    -- by which time an execution has already been claimed.
    """

    if not settings.destination_registry_secret_arn:
        raise ValueError("a deployed sender needs the destination registry secret ARN")
    return SecretsManagerDestinationRegistry(
        client=create_secrets_client(region_name=settings.aws_region),
        secret_id=settings.destination_registry_secret_arn,
    )


def composition() -> SenderComposition:
    """Build the object graph once per execution environment, on first use.

    ``scope`` (P1) is the one logical clock the send use case is built over, so its business
    timestamps and its ``_expired()`` fence comparison agree with the same case-world instant
    the compiler, the proposal, and the approval are all stamped against -- never
    ``SystemClock``.
    """

    global _composition
    if _composition is None:
        settings = Settings.load()
        sender = sender_settings(settings)
        driver = build_sender_driver(sender)
        scope = ScopedLogicalClock()
        _composition = SenderComposition(
            send_action=build_send_action(
                sender, clock=scope, registry=build_registry(settings), driver=driver
            ),
            clock_store=DynamoDbDemoClockStore(
                driver=driver, namespace=Namespace(sender.namespace)
            ),
            scope=scope,
        )
    return _composition


async def run(event: object, *, built: SenderComposition | None = None) -> dict[str, Any]:
    """Run one approved send to its durable outcome. The async body the handler drives."""

    try:
        _, payload = read_envelope(event, accepted=ACCEPTED_OPERATIONS)
        command = decode_send_request(payload)
    except (EnvelopeError, SendRequestError):
        # Refused at the parse: no execution has been read, no fence acquired, no SES call made.
        return failure(MALFORMED_REQUEST)
    graph = built or composition()
    try:
        record = await graph.clock_store.read()
    except DemoClockError as error:
        # Fails the invocation, not the answer -- and it is safe to, because it happens
        # strictly before an execution is claimed or SES is reachable: no send attempt is
        # possible yet, so the ``SEND_UNKNOWN``/``SENDING`` quarantine below is never at risk
        # (P2-6, § 11). A normal return here would be read as "handled" by the caller and
        # never retried, silently losing the send.
        raise InvocationFailedError(CLOCK_UNAVAILABLE) from error
    try:
        with graph.scope.bound_to(record.logical_time):
            return encode_send_result(await graph.send_action.execute(command))
    except Exception as error:
        # The caller branches on *which* failure a send had -- terminal denial, ambiguous
        # outcome, definite failure -- so the kind travels with the safe code and nothing else.
        # An error this taxonomy does not recognise is re-raised by ``encode_send_failure``
        # rather than flattened, so an unmapped bug surfaces as one instead of as a settled
        # send.
        return encode_send_failure(error)


def handler(event: object, context: object = None) -> dict[str, Any]:
    """The Lambda entry point. One invocation, one event loop, one deliberate attempt."""

    return anyio.run(run, event)


__all__ = [
    "ACCEPTED_OPERATIONS",
    "CLOCK_UNAVAILABLE",
    "MALFORMED_REQUEST",
    "SenderComposition",
    "build_registry",
    "composition",
    "handler",
    "run",
    "sender_settings",
]
