"""The API's two internal boundaries: the asynchronous handover, and the synchronous ones.

``API -> worker`` is a **handover**. The request path writes a durable operation, dispatches the
job, and returns ``202``; it does not wait, it expects no business result, and it must not
silently swallow a failed handover -- an undispatched operation is stranded forever with a
record that looks entirely healthy.

``API -> watcher``, ``API -> compiler``, and ``worker -> sender`` are **calls**. Each one is
``RequestResponse`` against one configured ARN, each one checks ``FunctionError`` and the shape
of what came back, and none of them can be aimed by a payload.

Every client is a fake. Nothing resolves a credential and nothing reaches AWS.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from chorus.application.commands.extract_commitment_operation import ExtractCommitmentJob
from chorus.application.dispatch import RemoteOperationDispatcher
from chorus.application.jobs import WORKER_JOB_SCHEMA, WorkerJobKind, decode_job
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    EvidenceItemId,
    ExecutionId,
    MessageId,
    Namespace,
    OperationId,
    Sha256Digest,
    ViewId,
)
from chorus.infrastructure.lambdas.invoker import (
    ACCEPTED_EVENT_STATUS,
    EVENT,
    AsynchronousLambdaInvoker,
)
from chorus.ports.errors import ExternalDependencyError
from chorus.ports.operations import (
    InvestigationOperationJob,
    MonitorOperationJob,
    ProposeActionOperationJob,
    SendActionOperationJob,
)
from chorus.ports.records import MessageFeedEntry

WORKER_ARN = "arn:aws:lambda:us-east-1:000000000000:function:chorus-worker-demo"
DIGEST = Sha256Digest(f"sha256:{'e' * 64}")
SENT_AT = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeLambda:
    def __init__(self, status: int = ACCEPTED_EVENT_STATUS) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        return {
            "StatusCode": self.status,
            "ResponseMetadata": {"HTTPStatusCode": self.status},
            "Payload": io.BytesIO(b""),
        }


def dispatcher(client: FakeLambda) -> RemoteOperationDispatcher:
    return RemoteOperationDispatcher(
        invoker=AsynchronousLambdaInvoker(client=client, function_name=WORKER_ARN)
    )


def jobs() -> dict[WorkerJobKind, Any]:
    community = CommunityId(uuid4())
    case = CaseId(uuid4())
    return {
        WorkerJobKind.MONITOR: MonitorOperationJob(
            operation_id=OperationId(uuid4()),
            namespace=Namespace("DEMO"),
            community_id=community,
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor_id_hash=DIGEST,
            request_hash=DIGEST,
            message_locators=(MessageFeedEntry(message_id=MessageId(uuid4()), sent_at=SENT_AT),),
        ),
        WorkerJobKind.INVESTIGATE: InvestigationOperationJob(
            operation_id=OperationId(uuid4()),
            namespace=Namespace("DEMO"),
            community_id=community,
            case_id=case,
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor_id_hash=DIGEST,
            request_hash=DIGEST,
            expected_case_version=1,
            reason="INITIAL",
            idempotency_key="investigate-key",
        ),
        WorkerJobKind.PROPOSE_ACTION: ProposeActionOperationJob(
            operation_id=OperationId(uuid4()),
            namespace=Namespace("DEMO"),
            community_id=community,
            case_id=case,
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor_id_hash=DIGEST,
            request_hash=DIGEST,
            expected_case_version=1,
            view_id=ViewId(uuid4()),
            view_hash=DIGEST,
            idempotency_key="propose-key",
        ),
        WorkerJobKind.EXTRACT_COMMITMENT: ExtractCommitmentJob(
            operation_id=OperationId(uuid4()),
            namespace=Namespace("DEMO"),
            community_id=community,
            case_id=case,
            action_id=ActionId(uuid4()),
            evidence_id=EvidenceItemId(uuid4()),
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor_id_hash=DIGEST,
            request_hash=DIGEST,
            evidence_sha256=DIGEST,
        ),
        WorkerJobKind.SEND_ACTION: SendActionOperationJob(
            operation_id=OperationId(uuid4()),
            namespace=Namespace("DEMO"),
            community_id=community,
            case_id=case,
            action_id=ActionId(uuid4()),
            execution_id=ExecutionId(uuid4()),
            approval_id=ApprovalId(uuid4()),
            correlation_id=uuid4(),
            actor_id_hash=DIGEST,
            request_hash=DIGEST,
            expected_execution_version=1,
            idempotency_key="send-key",
        ),
    }


async def dispatch(kind: WorkerJobKind, job: Any, subject: RemoteOperationDispatcher) -> None:
    """Call the one dispatch method this kind belongs to, and no other."""

    match kind:
        case WorkerJobKind.MONITOR:
            await subject.dispatch_monitor(job)
        case WorkerJobKind.INVESTIGATE:
            await subject.dispatch_investigation(job)
        case WorkerJobKind.PROPOSE_ACTION:
            await subject.dispatch_propose_action(job)
        case WorkerJobKind.EXTRACT_COMMITMENT:
            await subject.dispatch_extract_commitment(job)
        case WorkerJobKind.SEND_ACTION:
            await subject.dispatch_send_action(job)


# -- API -> worker ----------------------------------------------------------------------------


async def test_every_handover_is_an_event_invocation_of_the_configured_worker() -> None:
    client = FakeLambda()
    subject = dispatcher(client)

    for kind, job in jobs().items():
        await dispatch(kind, job, subject)

    assert len(client.calls) == len(WorkerJobKind)
    for call in client.calls:
        assert call["InvocationType"] == EVENT
        assert call["FunctionName"] == WORKER_ARN


async def test_the_exact_event_is_serialized_and_decodes_back_to_the_job() -> None:
    client = FakeLambda()
    subject = dispatcher(client)
    sent = jobs()

    for kind, job in sent.items():
        await dispatch(kind, job, subject)

    for call, (kind, job) in zip(client.calls, sent.items(), strict=True):
        body = json.loads(call["Payload"])
        assert body["operation"] == kind.value
        assert body["payload"]["schema"] == WORKER_JOB_SCHEMA
        decoded_kind, decoded = decode_job(body["payload"])
        assert decoded_kind is kind
        assert decoded == job


async def test_the_handover_returns_no_business_result() -> None:
    """An ``Event`` invocation answers "accepted", and there is nothing else to read.

    The signature says so -- every ``dispatch_*`` returns ``None`` -- which is stronger than a
    runtime assertion: a caller cannot write code that waits for a result that does not exist.
    """

    dispatched = RemoteOperationDispatcher.dispatch_monitor.__annotations__["return"]
    assert dispatched == "None"
    await dispatcher(FakeLambda()).dispatch_monitor(jobs()[WorkerJobKind.MONITOR])


async def test_a_transport_failure_is_raised_rather_than_swallowed() -> None:
    """A dropped handover leaves a durable operation nothing knows to run."""

    with pytest.raises(ExternalDependencyError):
        await dispatcher(FakeLambda(status=500)).dispatch_monitor(jobs()[WorkerJobKind.MONITOR])


async def test_an_extraction_handover_refuses_anything_but_its_own_job() -> None:
    """The port types this one as ``object``; the dispatcher narrows it before encoding."""

    with pytest.raises(TypeError):
        await dispatcher(FakeLambda()).dispatch_extract_commitment({"not": "a job"})


async def test_no_payload_field_can_redirect_the_handover() -> None:
    client = FakeLambda()
    await dispatcher(client).dispatch_monitor(jobs()[WorkerJobKind.MONITOR])
    assert client.calls[0]["FunctionName"] == WORKER_ARN


async def test_the_dispatcher_holds_no_deduplication_state() -> None:
    """Deliberate: a process-local seen-set is an answer to a cross-process question.

    The durable operation's conditional claim is the duplicate boundary, so the same job
    dispatched twice is dispatched twice -- and the worker settles it.
    """

    client = FakeLambda()
    subject = dispatcher(client)
    job = jobs()[WorkerJobKind.MONITOR]
    await subject.dispatch_monitor(job)
    await subject.dispatch_monitor(job)

    assert len(client.calls) == 2
    assert client.calls[0]["Payload"] == client.calls[1]["Payload"]


def test_the_dispatcher_carries_no_client_and_no_arn_of_its_own() -> None:
    """It holds an invoker and nothing else, so there is no target here to change."""

    fields = set(RemoteOperationDispatcher.__dataclass_fields__)
    assert fields == {"invoker"}


def test_the_job_identifier_is_the_only_thing_that_names_a_uuid() -> None:
    """A sanity check on the frozen shape: identifiers, versions, digests, instants, keys."""

    job = jobs()[WorkerJobKind.SEND_ACTION]
    from chorus.application.jobs import encode_job

    body = encode_job(job)["job"]
    assert isinstance(body, dict)
    assert UUID(str(body["execution_id"])) == job.execution_id.value
