"""The asynchronous work handover, expressed as data rather than as a call.

An agent operation is started by one process and executed by another. The handover carries
identifiers only: which namespace, which community, which invocation, which messages. No
message text, no projected payload, and no agent output crosses it, so a redelivered job
cannot become a second copy of private content sitting in a queue.

This is deliberately not a workflow contract. There is one job shape, and it names the work
precisely enough that the worker can reload everything it needs from the durable state that
already exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from chorus.domain.entities import ApplicationOperation
from chorus.domain.ids import CommunityId, Namespace, OperationId, Sha256Digest
from chorus.ports.records import MessageFeedEntry


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorOperationJob:
    """One Monitor run, addressed by identity alone.

    It names the *newly ingested* messages and nothing else. There is deliberately no field
    for candidate case identifiers: which cases a run may extend is discovered by the use case
    from the signals of its own bounded context window, so neither a queue message nor an HTTP
    caller can steer what the discovery is allowed to find.

    ``request_hash`` is carried so the worker can *bind* the job to the durable operation
    before it claims anything. A job is data on a queue, and data on a queue can be misrouted
    or replayed from another command family; without a binding, a worker would take the first
    operation it was pointed at on trust. It is a digest of the normalized command, so it
    names the request without carrying any part of it.
    """

    operation_id: OperationId
    namespace: Namespace
    community_id: CommunityId
    invocation_id: UUID
    correlation_id: UUID
    actor_id_hash: Sha256Digest
    request_hash: Sha256Digest
    message_locators: tuple[MessageFeedEntry, ...]

    def __post_init__(self) -> None:
        if not self.message_locators:
            raise ValueError("a Monitor job names at least one message")
        identifiers = tuple(locator.message_id for locator in self.message_locators)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("a Monitor job names each message once")


class OperationDispatchPort(Protocol):
    """Hand one job to whatever executes work outside the caller's request."""

    async def dispatch_monitor(self, job: MonitorOperationJob) -> None:
        """Deliver the job at least once.

        At-least-once is the contract on purpose: the durable operation record and the derived
        entity identity make a repeated delivery a no-op, so a dispatcher never has to promise
        exactly-once semantics it cannot actually provide.
        """


class MonitorJobRunner(Protocol):
    """Execute one Monitor job to a terminal operation status.

    Declared here so a dispatcher -- which is infrastructure -- can hold the thing it runs
    without importing the application layer that implements it.
    """

    async def execute(self, job: MonitorOperationJob) -> ApplicationOperation:
        """Run the job and return the operation as it now stands.

        Implementations record failure on the operation rather than raising, because an
        at-least-once dispatcher must not be told to retry an agent invocation implicitly.
        """
