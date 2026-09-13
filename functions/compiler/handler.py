"""The compiler Lambda's entry point: the deterministic privacy authority, behind one invoke.

Three operations and no fourth. ``CompileView`` for the request path, and the two halves of the
send fence -- ``AcquireSendAuthorizationFence`` and ``ReleaseSendAuthorizationFence`` -- for the
sender, which cannot read Core at all
([ADR-024](../../../docs/adr/ADR-024-execution-partition-and-sender-boundary.md) § 3). Both
callers are here for the same reason: **the compiler is the sole creator of views and the sole
authority over send authorization, by IAM and not by convention**, so every question it answers
has to arrive as an invocation rather than as an in-process call somebody could reroute.

What this handler must never do, and structurally cannot
---------------------------------------------------------
No model, no mail, no agent runtime, no scheduler. The composition root builds none of them and
an import-linter contract forbids Strands, the agent contracts, and the API package from this
package outright -- the compiler is the one component whose answers must not be able to become
probabilistic, and an import is the first step toward that.

Errors are answers about the request, never about the data
------------------------------------------------------------
A denial is *raised* by the authority and travels as a failure, exactly as it does in-process.
The failure body carries a reason code and nothing else: no traceback, no rejected field, no
part of a case, a fact, or an evidence item. Private Core evidence must not appear in an error
response, and the way that is guaranteed is that nothing here ever reads one into a message.

The KMS keys are required, not defaulted
-----------------------------------------
:func:`~functions.compiler.composition.build_compile_view` refuses to construct without both
evidence key ARNs, so a compiler with no key ARN fails at cold start instead of failing every
safe-evidence write in an account (deployment contract § 9). This handler does not soften that.

The clock (P1, Phase 11 batch 4 repair)
-----------------------------------------
[ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) § 2 is amended to give
the compiler a **strongly consistent read, and only a read**, of the one authoritative logical
clock. Without it a compiled view's ``generated_at``/``expires_at`` and every freshness
comparison the send fence makes -- against a view, an approval, or a mandate -- were wall-clock
instants judged against a case timeline that runs on logical time, so a view minted the instant
after the demo clock was advanced past 2030 would read as already expired against a fresh
``SystemClock`` reading, and a fence acquired against a stale wall-clock ``requested_at`` would
never agree with the sender's own comparison of the same fence.

The read happens **once per invocation**, strongly consistent, and is bound for the whole
invocation via :class:`~chorus.infrastructure.persistent_clock.ScopedLogicalClock` -- exactly
the pattern the watcher and the worker already use. A clock that cannot be read fails the
*invocation*, not the answer: it is raised as
:class:`~functions.envelope.InvocationFailedError` rather than returned as a normal refusal,
because nothing has been read, decided, or written yet and the failure is a transient
infrastructure condition an async or synchronous caller should be told to retry, not a business
outcome to acknowledge (P2-6).

Compile failures travel as themselves, not as a generic dependency rejection (P2-4)
---------------------------------------------------------------------------------------
A denial the local compile path raises -- ``PolicyDeniedError``, ``StaleAuthorizationError``,
``SendAuthorizationInProgressError``, or a domain/persistence failure -- is **not** allowed to
become an uncaught Lambda ``FunctionError``: that would arrive at the API as a generic
``DEPENDENCY_REJECTED``/503, discarding the specific 422 classification and the policy reason
codes a caller needs. So the ``CompileView`` branch below catches its own typed exceptions and
returns a normal, closed failure envelope (:func:`~chorus.application.compile_contract.
encode_compile_failure`); :func:`~chorus.application.compile_contract.decode_compile_response`
reconstructs the identical exception type on the other end, and the API's existing
``DomainError``/``ApplicationError``/``PersistenceError`` handlers classify it exactly as they
would the in-process path. An error this taxonomy does not recognise is re-raised -- an
unmapped bug must still surface as ``FunctionError``, never be disguised as a policy refusal.

**Cold start touches no network.** The graph is built lazily on the first invocation; boto3
clients are constructed without a credential lookup or a request, so importing this module needs
no AWS anything.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Final

import anyio

from chorus.application.commands.compile_view import CompileView
from chorus.application.compile_contract import (
    COMPILE_OPERATION,
    CompileRequestError,
    decode_compile_request,
    encode_compile_failure,
    encode_compile_response,
)
from chorus.application.services.send_authorization import SendAuthorization
from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId, Namespace
from chorus.infrastructure.compiler.send_authorization import (
    ACQUIRE_OPERATION,
    RELEASE_OPERATION,
)
from chorus.infrastructure.dynamodb.demo_clock import DynamoDbDemoClockStore
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.ports.demo_clock import DemoClockError, DemoClockStorePort
from chorus.ports.records import StoredSafeDestination
from chorus.settings import Settings
from functions.compiler.composition import (
    CompilerSettings,
    build_compile_view,
    build_compiler_driver,
)
from functions.compiler.fence import (
    FenceRequestError,
    build_send_authorization,
    decode_fence_request,
    decode_release_request,
    encode_fence_outcome,
)
from functions.envelope import EnvelopeError, InvocationFailedError, failure, read_envelope

ACCEPTED_OPERATIONS: Final = frozenset({COMPILE_OPERATION, ACQUIRE_OPERATION, RELEASE_OPERATION})
"""The compiler's complete invocation surface. An operation outside it fails closed."""

MALFORMED_REQUEST: Final = "MALFORMED_REQUEST"
CLOCK_UNAVAILABLE: Final = "CLOCK_UNAVAILABLE"
"""The reason named in the raised :class:`~functions.envelope.InvocationFailedError` when the
authoritative clock cannot be read. Never returned as a normal payload (P2-6)."""

COMMUNITY_PUBLIC_LABEL: Final = "Community"
"""The safe public label a compiled view carries when the deployment names none.

Overridden by ``CHORUS_COMMUNITY_PUBLIC_LABEL`` where a deployment sets one; it is safe
configuration by construction -- it is the label that appears *in* an external-safe view.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class CompilerComposition:
    """The two use cases the compiler function serves, plus the clock both are stamped by."""

    compile_view: CompileView
    authorization: SendAuthorization
    clock_store: DemoClockStorePort
    scope: ScopedLogicalClock


_composition: CompilerComposition | None = None


def compiler_settings(settings: Settings) -> CompilerSettings:
    """Map process configuration onto the compiler's own settings, and nothing wider.

    The destination is assembled from the **non-secret** deployment values -- identifier, kind,
    registry version, routing token, display label -- and never from the sender's destination
    secret, which the compiler role is not granted and must not become a second holder of.
    """

    return CompilerSettings(
        region=settings.aws_region,
        namespace=settings.namespace,
        core_table=settings.core_table,
        shareable_table=settings.shareable_table,
        audit_table=settings.audit_table,
        private_evidence_bucket=settings.private_evidence_bucket,
        export_evidence_bucket=settings.export_evidence_bucket,
        community_public_label=COMMUNITY_PUBLIC_LABEL,
        destination=StoredSafeDestination(
            destination_id=DestinationId(settings.destination_id),
            kind=DestinationKind.PROPERTY_MANAGER,
            registry_version=settings.destination_registry_version,
            routing_token=settings.destination_routing_token,
            display_label=settings.destination_display_label,
        ),
        cursor_secret=secrets.token_bytes(32),
        private_evidence_key_arn=settings.private_evidence_key_arn,
        export_evidence_key_arn=settings.export_evidence_key_arn,
    )


def composition() -> CompilerComposition:
    """Build the object graph once per execution environment, on first use.

    ``scope`` (P1) is the single logical clock both the compile use case and the send-fence
    authority are built over, so a view's ``generated_at``/``expires_at`` and the fence's
    freshness comparisons are stamped against the same case-world instant every other business
    timestamp in the system uses -- never ``SystemClock``.
    """

    global _composition
    if _composition is None:
        compiler = compiler_settings(Settings.load())
        driver = build_compiler_driver(compiler)
        scope = ScopedLogicalClock()
        _composition = CompilerComposition(
            compile_view=build_compile_view(compiler, clock=scope, driver=driver),
            authorization=build_send_authorization(compiler, clock=scope),
            clock_store=DynamoDbDemoClockStore(
                driver=driver, namespace=Namespace(compiler.namespace)
            ),
            scope=scope,
        )
    return _composition


async def run(event: object, *, built: CompilerComposition | None = None) -> dict[str, Any]:
    """Dispatch one invocation on its **declared** operation, and on nothing else."""

    try:
        operation, payload = read_envelope(event, accepted=ACCEPTED_OPERATIONS)
    except EnvelopeError:
        return failure(MALFORMED_REQUEST)
    graph = built or composition()
    try:
        record = await graph.clock_store.read()
    except DemoClockError as error:
        # Fails the invocation, not the answer: nothing has been read, decided, or written yet,
        # so this is safe to retry and AWS's own retry/FunctionError machinery should see it
        # rather than a caller reading a normal payload as a settled answer (P2-6).
        raise InvocationFailedError(CLOCK_UNAVAILABLE) from error
    with graph.scope.bound_to(record.logical_time):
        return await _dispatch(operation, payload, graph)


async def _dispatch(
    operation: str, payload: dict[str, Any], graph: CompilerComposition
) -> dict[str, Any]:
    if operation == COMPILE_OPERATION:
        try:
            command = decode_compile_request(payload)
        except CompileRequestError:
            return failure(MALFORMED_REQUEST)
        try:
            result = await graph.compile_view.execute(command)
        except Exception as error:
            # P2-4/P2-9: a typed compile denial travels as itself, never as a generic
            # ``FunctionError``/503 -- see the module docstring. ``encode_compile_failure``
            # re-raises anything it does not recognise, so an unmapped bug still surfaces as a
            # ``FunctionError`` rather than a disguised policy refusal.
            return encode_compile_failure(error)
        return encode_compile_response(result)
    if operation == ACQUIRE_OPERATION:
        try:
            request = decode_fence_request(payload)
        except FenceRequestError:
            return failure(MALFORMED_REQUEST)
        return encode_fence_outcome(await graph.authorization.authorize(request))
    try:
        scope, execution_id = decode_release_request(payload)
    except FenceRequestError:
        return failure(MALFORMED_REQUEST)
    await graph.authorization.release(scope, execution_id)
    return {"status": "RELEASED"}


def handler(event: object, context: object = None) -> dict[str, Any]:
    """The Lambda entry point. One invocation, one event loop, one operation."""

    return anyio.run(run, event)


__all__ = [
    "ACCEPTED_OPERATIONS",
    "CLOCK_UNAVAILABLE",
    "COMMUNITY_PUBLIC_LABEL",
    "MALFORMED_REQUEST",
    "CompilerComposition",
    "compiler_settings",
    "composition",
    "handler",
    "run",
]
