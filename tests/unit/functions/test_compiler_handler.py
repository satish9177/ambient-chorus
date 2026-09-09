"""The compiler Lambda: three operations, one closed dispatch, and a view that survives the wire.

The compiler is the deterministic privacy authority, and everything asserted here is about
keeping it that way across a Lambda boundary:

* the dispatch branches on the **declared** operation and on nothing else, and an unknown one
  fails closed rather than falling through;
* ``compile-request/v1`` and ``compile-response/v1`` round-trip a full compiled view field for
  field, so what a presenter is shown is what the compiler actually produced;
* a request the decoder rejects never reaches the use case, so a malformed compile costs a
  validation and not a partial read of a case;
* the remote adapter never turns an unparseable answer into an empty view -- "the compiler could
  not be reached" and "the compiler excluded everything" are different facts.

The composition is handed in. No S3, no DynamoDB, no KMS, and no AWS client is constructed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from functions.compiler.fence import encode_fence_outcome
from functions.compiler.handler import (
    ACCEPTED_OPERATIONS,
    MALFORMED_REQUEST,
    CompilerComposition,
    run,
)
from functions.envelope import InvocationFailedError

from chorus.application.commands.compile_view import (
    CompileViewCommand,
    CompileViewResult,
    ExcludedFactView,
    IncludedFactView,
    RequestedFactInput,
)
from chorus.application.compile_contract import (
    COMPILE_OPERATION,
    COMPILE_REQUEST_SCHEMA,
    COMPILE_RESPONSE_SCHEMA,
    CompileFailureKind,
    CompileRequestError,
    RemoteCompileView,
    decode_compile_request,
    decode_compile_response,
    encode_compile_failure,
    encode_compile_request,
    encode_compile_response,
)
from chorus.application.errors import (
    ApplicationError,
    ApplicationErrorCode,
    PolicyDeniedError,
    SendAuthorizationInProgressError,
    StaleAuthorizationError,
)
from chorus.domain.entities import (
    DestinationKind,
    DisclosureScope,
    EvidenceStatus,
    FactType,
    Purpose,
)
from chorus.domain.errors import DomainError, DomainErrorCode
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    DestinationId,
    EvidenceItemId,
    ExecutionId,
    ExportFactId,
    FactId,
    Namespace,
    SafeEvidenceRefId,
    Sha256Digest,
    ViewId,
)
from chorus.infrastructure.compiler.send_authorization import (
    ACQUIRE_OPERATION,
    RELEASE_OPERATION,
)
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.ports.demo_clock import DemoClockRecord, DemoClockUnavailableError
from chorus.ports.errors import ExternalDependencyError, PersistenceError, PersistenceErrorCode
from chorus.ports.records import (
    StoredMandateVersionRef,
    StoredSafeDestination,
    StoredSafeEvidenceRef,
    StoredShareableFact,
    StoredShareableView,
    TransformationKind,
)
from chorus.ports.send_authorization import SendAuthorizationDenied

NAMESPACE = Namespace("DEMO")
COMMUNITY = CommunityId(UUID("11111111-1111-4111-8111-111111111111"))
CASE = CaseId(UUID("22222222-2222-4222-8222-222222222222"))
DIGEST = Sha256Digest(f"sha256:{'c' * 64}")
GENERATED = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def destination() -> StoredSafeDestination:
    return StoredSafeDestination(
        destination_id=DestinationId("property_manager:demo"),
        kind=DestinationKind.PROPERTY_MANAGER,
        registry_version=1,
        routing_token=UUID("00000000-0000-4000-8000-000000000009"),
        display_label="Property Management",
    )


def command() -> CompileViewCommand:
    return CompileViewCommand(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        case_id=CASE,
        compile_id=UUID("33333333-3333-4333-8333-333333333333"),
        expected_case_version=2,
        requested_facts=(
            RequestedFactInput(
                fact_id=FactId(uuid4()), necessity="NECESSARY", intended_usage="REPAIR_REQUEST"
            ),
        ),
        requested_evidence_ids=(EvidenceItemId(uuid4()),),
        destination=destination(),
        purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        actor_id_hash=DIGEST,
        idempotency_key="compile-key-0001",
        correlation_id=uuid4(),
    )


def view() -> StoredShareableView:
    ref_id = SafeEvidenceRefId(uuid4())
    return StoredShareableView(
        schema_version="shareable-case-view/v2",
        view_id=ViewId(uuid4()),
        case_id=CASE,
        community_public_label="A residential community",
        case_version=2,
        authorization_version=3,
        policy_version="policy/v1",
        compiler_version="compiler/v1",
        policy_build_hash=DIGEST,
        destination=destination(),
        purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        generated_at=GENERATED,
        expires_at=GENERATED + timedelta(hours=1),
        mandate_version_set=(
            StoredMandateVersionRef(mandate_id=uuid4(), version=2, terms_hash=DIGEST),
        ),
        authorization_snapshot_hash=DIGEST,
        shareable_facts=(
            StoredShareableFact(
                export_fact_id=ExportFactId(uuid4()),
                fact_type=FactType.INCIDENT_OCCURRENCE,
                safe_text="The lift has been out of service for several days.",
                effective_scope=DisclosureScope.ANONYMOUS_CASE,
                evidence_status=EvidenceStatus.CORROBORATED,
                contributor_count=3,
                transformation=TransformationKind.AGGREGATED,
                transformation_rule_id="rule/aggregate/v1",
                safe_evidence_ref_ids=(ref_id,),
                content_hash=DIGEST,
            ),
        ),
        safe_evidence_refs=(
            StoredSafeEvidenceRef(
                safe_evidence_ref_id=ref_id,
                media_type="image/png",
                export_handle_id=uuid4(),
                sha256=DIGEST,
                caption="A notice on the lift door.",
                created_by_rule_id="rule/evidence/v1",
                content_hash=DIGEST,
            ),
        ),
        audit_refs=(uuid4(),),
        view_hash=DIGEST,
    )


def result() -> CompileViewResult:
    compiled = view()
    return CompileViewResult(
        compile_id=UUID("33333333-3333-4333-8333-333333333333"),
        audit_event_id=uuid4(),
        view=compiled,
        included=(
            IncludedFactView(
                fact_id=FactId(uuid4()),
                export_fact_ids=(compiled.shareable_facts[0].export_fact_id.value,),
            ),
        ),
        excluded=(
            ExcludedFactView(fact_id=FactId(uuid4()), reason_codes=("SCOPE_INTERNAL_ONLY",)),
        ),
        replayed=False,
    )


class RecordingCompile:
    def __init__(self, answer: CompileViewResult | None = None) -> None:
        self.answer = answer or result()
        self.commands: list[CompileViewCommand] = []

    async def execute(self, command: CompileViewCommand) -> CompileViewResult:
        self.commands.append(command)
        return self.answer


class RecordingAuthorization:
    def __init__(self) -> None:
        self.authorized: list[Any] = []
        self.released: list[Any] = []

    async def authorize(self, request: Any) -> Any:
        self.authorized.append(request)
        return SendAuthorizationDenied(reason_codes=("CASE_NOT_AUTHORIZED",))

    async def release(self, scope: Any, execution_id: Any) -> None:
        self.released.append((scope, execution_id))


class StubClockStore:
    """P1: the compiler now reads the authoritative clock once per invocation."""

    def __init__(self, *, instant: datetime | None = GENERATED) -> None:
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
            seed_instant=GENERATED,
            advance_count=0,
        )

    async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
        raise AssertionError("the compiler must never advance the clock")


def composition(
    compile_view: RecordingCompile | None = None, clock: StubClockStore | None = None
) -> CompilerComposition:
    return CompilerComposition(
        compile_view=compile_view or RecordingCompile(),  # type: ignore[arg-type]
        authorization=RecordingAuthorization(),  # type: ignore[arg-type]
        clock_store=clock or StubClockStore(),
        scope=ScopedLogicalClock(),
    )


# -- the closed dispatch --------------------------------------------------------------------


def test_the_compiler_answers_exactly_three_operations() -> None:
    assert {COMPILE_OPERATION, ACQUIRE_OPERATION, RELEASE_OPERATION} == ACCEPTED_OPERATIONS


async def test_an_unknown_operation_is_refused_without_touching_the_compiler() -> None:
    compile_view = RecordingCompile()
    graph = composition(compile_view)
    result_body = await run(
        {"operation": "CompileEverything", "payload": encode_compile_request(command())},
        built=graph,
    )

    assert result_body == {"status": "REFUSED", "reason_code": MALFORMED_REQUEST}
    assert compile_view.commands == []


@pytest.mark.parametrize(
    "event",
    [
        pytest.param("not an object", id="not-an-object"),
        pytest.param({"payload": {}}, id="no-operation"),
        pytest.param({"operation": COMPILE_OPERATION}, id="no-payload"),
    ],
)
async def test_a_malformed_envelope_is_refused(event: object) -> None:
    compile_view = RecordingCompile()
    assert (await run(event, built=composition(compile_view)))["status"] == "REFUSED"
    assert compile_view.commands == []


# -- the compile round trip -------------------------------------------------------------------


def test_a_compile_request_round_trips_field_for_field() -> None:
    original = command()
    payload = encode_compile_request(original)

    assert payload["schema"] == COMPILE_REQUEST_SCHEMA
    assert decode_compile_request(payload) == original


def test_a_compiled_view_round_trips_field_for_field() -> None:
    original = result()
    body = encode_compile_response(original)

    assert body["schema"] == COMPILE_RESPONSE_SCHEMA
    assert decode_compile_response(body) == original


async def test_a_valid_request_reaches_the_use_case_and_returns_its_answer() -> None:
    compile_view = RecordingCompile()
    original = command()
    body = await run(
        {"operation": COMPILE_OPERATION, "payload": encode_compile_request(original)},
        built=composition(compile_view),
    )

    assert compile_view.commands == [original]
    assert decode_compile_response(body) == compile_view.answer


# -- P1: the compiler reads the authoritative clock once, and never falls back ------------


async def test_the_compiler_reads_the_clock_before_compiling() -> None:
    compile_view = RecordingCompile()
    clock = StubClockStore(instant=GENERATED + timedelta(days=1))
    await run(
        {"operation": COMPILE_OPERATION, "payload": encode_compile_request(command())},
        built=composition(compile_view, clock),
    )
    assert clock.reads == 1


async def test_an_unreadable_clock_fails_the_invocation_before_compiling() -> None:
    """P1 + P2-6: an infrastructure outage fails the invocation, not the answer."""

    compile_view = RecordingCompile()
    with pytest.raises(InvocationFailedError):
        await run(
            {"operation": COMPILE_OPERATION, "payload": encode_compile_request(command())},
            built=composition(compile_view, StubClockStore(instant=None)),
        )
    assert compile_view.commands == []


async def test_the_clock_is_also_read_before_a_fence_operation() -> None:
    """Every operation shares the one bound reading; none of the three branches on it."""

    with pytest.raises(InvocationFailedError):
        await run(
            {"operation": ACQUIRE_OPERATION, "payload": {}},
            built=composition(clock=StubClockStore(instant=None)),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda body: body.__setitem__("schema", "compile-request/v9"), id="schema"),
        pytest.param(lambda body: body.__setitem__("namespace", "not a namespace"), id="namespace"),
        pytest.param(lambda body: body.__setitem__("case_id", "not-a-uuid"), id="case"),
        pytest.param(lambda body: body.__setitem__("requested_facts", "nope"), id="facts"),
        pytest.param(lambda body: body.__setitem__("purpose", "SOMETHING_ELSE"), id="purpose"),
        pytest.param(lambda body: body.pop("destination"), id="no-destination"),
    ],
)
async def test_an_incomplete_request_never_reaches_the_use_case(mutate: Any) -> None:
    body = encode_compile_request(command())
    mutate(body)
    with pytest.raises(CompileRequestError):
        decode_compile_request(body)

    compile_view = RecordingCompile()
    refused = await run(
        {"operation": COMPILE_OPERATION, "payload": body}, built=composition(compile_view)
    )
    assert refused == {"status": "REFUSED", "reason_code": MALFORMED_REQUEST}
    assert compile_view.commands == []


async def test_a_refusal_body_carries_a_reason_code_and_nothing_else() -> None:
    """No traceback, no rejected field, and no part of a case, a fact, or an evidence item."""

    refused = await run({"operation": COMPILE_OPERATION, "payload": {}}, built=composition())
    assert set(refused) == {"status", "reason_code"}


# -- the remote adapter refuses to invent an answer ---------------------------------------------


async def test_the_remote_compile_parses_a_valid_answer() -> None:
    answer = result()

    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            assert operation == COMPILE_OPERATION
            return encode_compile_response(answer)

    assert await RemoteCompileView(invoker=Invoker()).execute(command()) == answer


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"schema": "compile-response/v9"}, id="wrong-schema"),
        pytest.param({"schema": COMPILE_RESPONSE_SCHEMA}, id="empty"),
        pytest.param(
            {"schema": COMPILE_RESPONSE_SCHEMA, "compile_id": "nope"}, id="bad-identifier"
        ),
    ],
)
async def test_an_unparseable_answer_raises_rather_than_becoming_an_empty_view(
    body: dict[str, Any],
) -> None:
    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    with pytest.raises(ExternalDependencyError):
        await RemoteCompileView(invoker=Invoker()).execute(command())


# -- the fence half -----------------------------------------------------------------------------


async def test_the_acquire_operation_reaches_the_authority() -> None:
    from chorus.infrastructure.compiler.send_authorization import (
        encode_authorization_request,
    )
    from chorus.ports.send_authorization import SendAuthorizationRequest

    request = SendAuthorizationRequest(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        case_id=CASE,
        action_id=ActionId(uuid4()),
        execution_id=ExecutionId(uuid4()),
        approval_id=ApprovalId(uuid4()),
        proposal_hash=DIGEST,
        view_id=ViewId(uuid4()),
        view_hash=DIGEST,
        authorization_version=3,
        policy_version="policy/v1",
        compiler_version="compiler/v1",
        policy_build_hash=DIGEST,
        destination_id=DestinationId("property_manager:demo"),
        destination_registry_version=1,
        routing_token=UUID("00000000-0000-4000-8000-000000000009"),
        purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        authorization_snapshot_hash=DIGEST,
        requested_at=GENERATED,
    )
    graph = composition()
    body = await run(
        {"operation": ACQUIRE_OPERATION, "payload": encode_authorization_request(request)},
        built=graph,
    )

    assert body["outcome"] == "DENIED"
    assert graph.authorization.authorized[0] == request  # type: ignore[attr-defined]


def test_a_denial_carries_reason_codes_and_no_fence() -> None:
    body = encode_fence_outcome(SendAuthorizationDenied(reason_codes=("CASE_NOT_AUTHORIZED",)))
    assert body == {"outcome": "DENIED", "reason_codes": ["CASE_NOT_AUTHORIZED"]}


# -- P2-4 / P2-9: a typed compile denial travels as itself, never as a generic 503 ----------


class DenyingCompile:
    """Stands in for the local compile path raising a typed denial after a model-free check."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.commands: list[CompileViewCommand] = []

    async def execute(self, command: CompileViewCommand) -> CompileViewResult:
        self.commands.append(command)
        raise self.error


async def test_a_policy_denial_is_returned_as_a_normal_failure_payload() -> None:
    """The compiler must not let a denial become an uncaught ``FunctionError``: that is what
    turned every compile denial into a generic 503 before this repair."""

    denying = DenyingCompile(PolicyDeniedError(("SCOPE_INTERNAL_ONLY",)))
    body = await run(
        {"operation": COMPILE_OPERATION, "payload": encode_compile_request(command())},
        built=composition(denying),  # type: ignore[arg-type]
    )

    assert body["status"] == "FAILED"
    assert body["kind"] == CompileFailureKind.APPLICATION.value
    assert body["code"] == ApplicationErrorCode.POLICY_DENIED.value
    assert body["reason_codes"] == ["SCOPE_INTERNAL_ONLY"]


@pytest.mark.parametrize(
    ("error", "kind", "code"),
    [
        pytest.param(
            PolicyDeniedError(("SCOPE_INTERNAL_ONLY",)),
            CompileFailureKind.APPLICATION,
            ApplicationErrorCode.POLICY_DENIED,
            id="policy-denied",
        ),
        pytest.param(
            StaleAuthorizationError(),
            CompileFailureKind.APPLICATION,
            ApplicationErrorCode.STALE_AUTHORIZATION,
            id="stale-authorization",
        ),
        pytest.param(
            SendAuthorizationInProgressError(),
            CompileFailureKind.APPLICATION,
            ApplicationErrorCode.SEND_AUTHORIZATION_IN_PROGRESS,
            id="fence-in-progress",
        ),
        pytest.param(
            DomainError(DomainErrorCode.VALIDATION_ERROR),
            CompileFailureKind.DOMAIN,
            DomainErrorCode.VALIDATION_ERROR,
            id="domain-validation",
        ),
        pytest.param(
            PersistenceError(PersistenceErrorCode.PERSISTENCE_CONFLICT, "COMPILE"),
            CompileFailureKind.PERSISTENCE,
            PersistenceErrorCode.PERSISTENCE_CONFLICT,
            id="persistence-conflict",
        ),
    ],
)
async def test_the_local_and_remote_compile_denial_classify_identically(
    error: Exception, kind: CompileFailureKind, code: Any
) -> None:
    """Parity: a local ``CompileView.execute()`` raise and a remote round trip through the
    Lambda boundary must produce the *identical externally observable classification* -- the
    same base exception family and the same ``.code`` -- so the API's registered exception
    handlers, which dispatch on that base family and read only ``.code``/``.reason_codes``,
    answer the caller identically either way. The reconstruction is deliberately the *base*
    ``ApplicationError``/``DomainError``, never the specific local subclass: the wire protocol
    carries a code, not a Python class identity, and the handler never asks for one.
    """

    body = await run(
        {"operation": COMPILE_OPERATION, "payload": encode_compile_request(command())},
        built=composition(DenyingCompile(error)),  # type: ignore[arg-type]
    )
    assert body["kind"] == kind.value

    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    # ``ApplicationError`` subclasses (``PolicyDeniedError`` and friends) are reconstructed as
    # the base class; ``DomainError``/``PersistenceError`` here are already the base class.
    base = ApplicationError if isinstance(error, ApplicationError) else type(error)
    with pytest.raises(base) as raised:
        await RemoteCompileView(invoker=Invoker()).execute(command())
    assert raised.value.code == code  # type: ignore[attr-defined]
    if isinstance(error, ApplicationError):
        assert raised.value.reason_codes == error.reason_codes  # type: ignore[attr-defined]


async def test_an_ambiguous_persistence_outcome_stays_ambiguous_across_the_wire() -> None:
    """The single most important line here: an unknown outcome must never settle as definite."""

    ambiguous = PersistenceError(PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "COMPILE")
    body = await run(
        {"operation": COMPILE_OPERATION, "payload": encode_compile_request(command())},
        built=composition(DenyingCompile(ambiguous)),  # type: ignore[arg-type]
    )
    assert body["kind"] == CompileFailureKind.UNKNOWN_OUTCOME.value

    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    with pytest.raises(PersistenceError) as raised:
        await RemoteCompileView(invoker=Invoker()).execute(command())
    assert raised.value.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME


async def test_an_unmapped_error_is_re_raised_as_a_function_error_not_disguised() -> None:
    """A bug must surface as a bug, not as a settled compile denial with an invented code."""

    unmapped = DenyingCompile(RuntimeError("nobody classified this"))
    with pytest.raises(RuntimeError):
        await run(
            {"operation": COMPILE_OPERATION, "payload": encode_compile_request(command())},
            built=composition(unmapped),  # type: ignore[arg-type]
        )


def test_a_compile_failure_body_carries_no_traceback_or_exception_repr() -> None:
    body = encode_compile_failure(PolicyDeniedError(("SCOPE_INTERNAL_ONLY",)))
    assert set(body) == {"schema", "status", "kind", "code", "reason_codes", "retryable"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"schema": COMPILE_RESPONSE_SCHEMA, "status": "FAILED"}, id="no-kind"),
        pytest.param(
            {
                "schema": COMPILE_RESPONSE_SCHEMA,
                "status": "FAILED",
                "kind": "INVENTED",
                "code": "X",
                "reason_codes": [],
            },
            id="unknown-kind",
        ),
        pytest.param(
            {
                "schema": COMPILE_RESPONSE_SCHEMA,
                "status": "FAILED",
                "kind": "APPLICATION",
                "code": "INVENTED_CODE",
                "reason_codes": [],
            },
            id="unknown-application-code",
        ),
    ],
)
async def test_an_unparseable_failure_body_raises_the_opaque_dependency_error(
    body: dict[str, Any],
) -> None:
    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    with pytest.raises(ExternalDependencyError):
        await RemoteCompileView(invoker=Invoker()).execute(command())
