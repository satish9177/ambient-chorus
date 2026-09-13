"""The send boundary: one command, one outcome, and a failure taxonomy that survives the wire.

The deployed sender is its own principal for a reason -- it holds a total Core deny, the only
SES grant, and the destination secret, and none of those may sit in the worker
([ADR-024](../../../docs/adr/ADR-024-execution-partition-and-sender-boundary.md) § 3). So the
worker's ``SEND_ACTION`` operation reaches it through one synchronous invocation, and this
module is that boundary: ``send-action-request/v1`` in, ``send-action-result/v1`` out.

The command carries no message
-------------------------------
Identifiers, a version, an actor digest, a correlation, and an idempotency key. **No recipient,
no subject, no body, no claim, no attachment, no template, and no retry flag** -- the same
absence :class:`~chorus.application.commands.send_action.SendActionCommand` already guarantees
as a property of its type. The recipient is resolved inside the sender from the safe destination
registry, so no invocation payload can name who receives an approved message.

The failure taxonomy is part of the contract, not an implementation detail
---------------------------------------------------------------------------
``SendActionOperationWorker`` branches on *which* failure a send had: a
:class:`~chorus.application.commands.send_action.SendDeniedError` is terminal, an
``UNKNOWN_TRANSACTION_OUTCOME`` leaves the operation recoverable rather than settled, and
everything else settles with a safe code. Collapsing those into one wire error would make a
deployed send behave differently from a local one at exactly the point where the difference is
"retry" versus "never retry". So a failure crosses as a *kind plus a safe code*, and
:class:`RemoteSendAction` re-raises the matching typed error before its caller ever sees it.

What never crosses: a message body, an address, a traceback, or an exception message. The kinds
are a closed set and the codes are the closed vocabularies the operation record already stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

from chorus.application.commands.send_action import (
    SendActionCommand,
    SendActionResult,
    SendDeniedError,
    SendReplayOutcome,
)
from chorus.application.errors import ApplicationError, ApplicationErrorCode
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
from chorus.ports.errors import (
    ExternalDependencyError,
    PersistenceError,
    PersistenceErrorCode,
)
from chorus.ports.invocation import SynchronousInvokerPort

SEND_OPERATION: Final = "SendAction"
SEND_REQUEST_SCHEMA: Final = "send-action-request/v1"
SEND_RESULT_SCHEMA: Final = "send-action-result/v1"


class SendFailureKind(StrEnum):
    """The closed set of failure shapes a send may report across the boundary."""

    DENIED = "DENIED"
    """The replay table refused this state. Terminal, and never retried."""

    DOMAIN = "DOMAIN"
    APPLICATION = "APPLICATION"
    PERSISTENCE = "PERSISTENCE"
    """A definite storage failure. Settled with its safe code."""

    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    """The write may or may not have committed. The operation stays recoverable and the next
    delivery resolves it by **reading** the execution, never by sending again."""


class SendRequestError(ValueError):
    """A delivered send request is not one this sender can run.

    Raised while parsing, before the execution is read and long before SES is reachable.
    """


def _unusable() -> ExternalDependencyError:
    return ExternalDependencyError(
        SEND_OPERATION, code=PersistenceErrorCode.DEPENDENCY_REJECTED, retryable=False
    )


class SendActionRunner(Protocol):
    """Run one send command to its durable outcome, wherever the sender happens to live."""

    async def execute(self, command: SendActionCommand) -> SendActionResult:
        """Return the durable outcome, or raise one of the frozen failure shapes."""


def encode_send_request(command: SendActionCommand) -> dict[str, Any]:
    """The frozen wire shape of one send command, field for field."""

    return {
        "schema": SEND_REQUEST_SCHEMA,
        "namespace": command.namespace.value,
        "community_id": str(command.community_id),
        "case_id": str(command.case_id),
        "action_id": str(command.action_id),
        "execution_id": str(command.execution_id),
        "approval_id": str(command.approval_id),
        "expected_execution_version": command.expected_execution_version,
        "actor_id_hash": command.actor_id_hash.value,
        "correlation_id": str(command.correlation_id),
        "idempotency_key": command.idempotency_key,
    }


def decode_send_request(payload: object) -> SendActionCommand:
    """Parse one delivered send command exactly, or refuse it before anything is read."""

    body = _object(payload)
    if body.get("schema") != SEND_REQUEST_SCHEMA:
        raise SendRequestError("a send request names an unknown schema version")
    try:
        return SendActionCommand(
            namespace=Namespace(_text(body, "namespace")),
            community_id=CommunityId(_uuid(body, "community_id")),
            case_id=CaseId(_uuid(body, "case_id")),
            action_id=ActionId(_uuid(body, "action_id")),
            execution_id=ExecutionId(_uuid(body, "execution_id")),
            approval_id=ApprovalId(_uuid(body, "approval_id")),
            expected_execution_version=_number(body, "expected_execution_version"),
            actor_id_hash=Sha256Digest(_text(body, "actor_id_hash")),
            correlation_id=_uuid(body, "correlation_id"),
            idempotency_key=_text(body, "idempotency_key"),
        )
    except SendRequestError:
        raise
    except (TypeError, ValueError) as error:
        raise SendRequestError("a send request is not well formed") from error


def encode_send_result(result: SendActionResult) -> dict[str, Any]:
    """The durable outcome, in identifiers and closed codes. Never a body, never an address."""

    return {
        "schema": SEND_RESULT_SCHEMA,
        "status": "COMPLETED",
        "execution_id": str(result.execution_id),
        "state": result.state.value,
        "version": result.version,
        "ses_message_id": result.ses_message_id,
        "failure_code": result.failure_code,
        "reason_codes": list(result.reason_codes),
        "ses_call_made": result.ses_call_made,
    }


def encode_send_failure(error: Exception) -> dict[str, Any]:
    """Classify one failed send into its frozen kind and safe code, and nothing else.

    The order matters and mirrors the worker's own ``except`` order: ``SendDeniedError`` is a
    ``DomainError`` and must be recognised first, and an ambiguous persistence outcome must be
    recognised before the definite ones, because settling it would record a definite failure for
    a write that may have committed.
    """

    if isinstance(error, SendDeniedError):
        return _failure(SendFailureKind.DENIED, error.safe_code, state=error.state.value)
    if isinstance(error, PersistenceError):
        kind = (
            SendFailureKind.UNKNOWN_OUTCOME
            if error.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME
            else SendFailureKind.PERSISTENCE
        )
        return _failure(kind, error.code.value)
    if isinstance(error, ApplicationError):
        return _failure(SendFailureKind.APPLICATION, error.code.value)
    if isinstance(error, DomainError):
        return _failure(SendFailureKind.DOMAIN, error.code.value)
    raise error


def _failure(kind: SendFailureKind, code: str, *, state: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"schema": SEND_RESULT_SCHEMA, "status": "FAILED", "kind": kind.value}
    body["code"] = code
    if state is not None:
        body["state"] = state
    return body


def decode_send_result(body: dict[str, Any]) -> SendActionResult:
    """Parse the sender's answer, or **raise the failure it reports**.

    A reported failure is re-raised as its own type here rather than returned, so the worker's
    branch on which failure happened is the identical branch it takes in a local run.
    """

    if body.get("schema") != SEND_RESULT_SCHEMA:
        raise _unusable()
    status = body.get("status")
    if status == "FAILED":
        raise _restore_failure(body)
    if status != "COMPLETED":
        raise _unusable()
    try:
        return SendActionResult(
            execution_id=ExecutionId(_uuid(body, "execution_id")),
            state=ActionExecutionState(_text(body, "state")),
            version=_number(body, "version"),
            ses_message_id=_optional_text(body, "ses_message_id"),
            failure_code=_optional_text(body, "failure_code"),
            reason_codes=tuple(_require_text(value) for value in _list(body, "reason_codes")),
            ses_call_made=_flag(body, "ses_call_made"),
        )
    except (SendRequestError, TypeError, ValueError) as error:
        raise _unusable() from error


def _restore_failure(body: dict[str, Any]) -> Exception:
    raw_kind = body.get("kind")
    code = body.get("code")
    if not isinstance(raw_kind, str) or not isinstance(code, str):
        return _unusable()
    try:
        kind = SendFailureKind(raw_kind)
    except ValueError:
        return _unusable()
    match kind:
        case SendFailureKind.DENIED:
            state = body.get("state")
            try:
                return SendDeniedError(SendReplayOutcome(code), ActionExecutionState(str(state)))
            except ValueError:
                return _unusable()
        case SendFailureKind.UNKNOWN_OUTCOME:
            # Deliberately reconstructed as the ambiguous type, so the caller leaves the
            # operation recoverable exactly as it would locally. Collapsing it into a definite
            # failure here would let a send whose outcome is unknown be reported as one that
            # definitely did not happen.
            return _persistence(PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME)
        case SendFailureKind.PERSISTENCE:
            try:
                return _persistence(PersistenceErrorCode(code))
            except ValueError:
                return _unusable()
        case SendFailureKind.APPLICATION:
            try:
                return ApplicationError(ApplicationErrorCode(code))
            except ValueError:
                return _unusable()
        case SendFailureKind.DOMAIN:
            try:
                return DomainError(DomainErrorCode(code))
            except ValueError:
                return _unusable()
    return _unusable()  # pragma: no cover - the kind set is closed


def _persistence(code: PersistenceErrorCode) -> PersistenceError:
    return PersistenceError(code, SEND_OPERATION, retryable=False)


@dataclass(frozen=True, slots=True)
class RemoteSendAction:
    """The worker's whole relationship with the sender: one synchronous invocation.

    It satisfies :class:`SendActionRunner`, so the send operation worker holds *the sender*
    rather than one of the two ways of reaching it, and branches on neither.
    """

    invoker: SynchronousInvokerPort

    async def execute(self, command: SendActionCommand) -> SendActionResult:
        body = await self.invoker.invoke(
            operation=SEND_OPERATION, payload=encode_send_request(command)
        )
        return decode_send_result(body)


def _object(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SendRequestError("a send request is not an object")
    return payload


def _list(body: dict[str, Any], name: str) -> list[Any]:
    value = body.get(name)
    if not isinstance(value, list):
        raise SendRequestError(f"a send field {name} is not a list")
    return value


def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise SendRequestError(f"a send field {name} is missing")
    return value


def _optional_text(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SendRequestError(f"a send field {name} is not a string")
    return value


def _require_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SendRequestError("a send list item is not a string")
    return value


def _number(body: dict[str, Any], name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SendRequestError(f"a send field {name} is not a number")
    return value


def _flag(body: dict[str, Any], name: str) -> bool:
    value = body.get(name)
    if not isinstance(value, bool):
        raise SendRequestError(f"a send field {name} is not a flag")
    return value


def _uuid(body: dict[str, Any], name: str) -> UUID:
    raw = _text(body, name)
    try:
        parsed = UUID(raw)
    except ValueError as error:
        raise SendRequestError(f"{name} is not a UUID") from error
    if str(parsed) != raw:
        raise SendRequestError(f"{name} is not canonical")
    return parsed


__all__ = [
    "SEND_OPERATION",
    "SEND_REQUEST_SCHEMA",
    "SEND_RESULT_SCHEMA",
    "RemoteSendAction",
    "SendActionRunner",
    "SendFailureKind",
    "SendRequestError",
    "decode_send_request",
    "decode_send_result",
    "encode_send_failure",
    "encode_send_request",
    "encode_send_result",
]
