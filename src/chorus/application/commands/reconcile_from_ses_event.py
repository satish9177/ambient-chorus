"""The trusted reconciliation caller: an authenticated delivery, one execution, and nothing else.

``ReconcileSendOutcome`` has two callers and this is the second one (ADR-025 SS 10). The first
is the application worker, which can only ever *quarantine* -- it holds no evidence, so
``SENDING -> SEND_UNKNOWN`` is the whole of what it can do. Resolving a quarantine needs proof,
and proof about an SES send is a thing only SES produces.

This module is where "only SES produces it" stops being a comment
------------------------------------------------------------------
An earlier version of this adapter took a raw mapping, decoded it, and handed the decoded fields
straight to the reconciliation command. Every check downstream passed for an envelope somebody
had typed by hand: the configuration set matched because the forger wrote the right string, the
execution tag matched because the derivation is public, and the message identifier was accepted
because there was nothing to compare it against. ``SEND_UNKNOWN -> SENT`` committed, carrying an
invented identifier -- T35, exactly as the threat register describes it.

Decoding proves *shape*. Correlation proves *aboutness*. Neither proves *origin*, and origin is
the only thing that makes a message identifier believable. So this adapter now holds a
:class:`chorus.application.services.ses_events.SesEventAttester` and the reconciliation command
holds the verifier that matches it: the delivery is authenticated **before** the envelope is
read, and the evidence that comes out cannot be produced by anything that did not go through
here.

What this entry point accepts, and what it structurally cannot
---------------------------------------------------------------
It accepts a **scope naming one execution** and a
:class:`chorus.ports.ses_events.SesEventTransportContext` -- how the notification arrived, what
resource it arrived through, and the body. That is the entire input surface. It does not accept
a configuration set, an execution tag, a message identifier, or an acceptance flag: every one of
those is read out of the envelope by the attester, so there is no parameter through which a
caller could supply one, and the values that *are* on the command -- the transport and the
source ARN -- are compared against what the deployment configured rather than believed.

That absence is the design. An endpoint taking ``{configuration_set, execution_tag,
message_id}`` from its caller would let anybody who could reach it resolve a quarantine to
``SENT`` by typing three strings -- T35, with the forger handed the pen. There is no such
parameter here and no such route in the API.

Cross-execution forgery is refused by the tag, not by the transport
--------------------------------------------------------------------
A genuine SES event about execution X, replayed against execution Y, is still a genuine event.
What refuses it is the ADR-025 SS 7 derivation: the tag is
``sha256({domain, namespace, execution_id})``, so it is recomputable for the execution being
reconciled and matches for exactly one. ``ReconcileSendOutcome`` performs that comparison, this
module hands it the values to compare, and neither of them trusts the delivery path to have got
the routing right. Authentication and correlation are two different questions and both are asked.

Where the subscription lives
-----------------------------
Phase 8 owns this boundary; **Phase 11 owns the wiring that feeds it** -- the event destination
on the ``chorus-{environment}`` configuration set, the authenticated transport that carries the
notification, and the one
:class:`chorus.ports.ses_events.SesEventTransportAuthenticator` that says whether a given
delivery really came off it. That is the same static-now, live-in-Phase-11 split as the sender
function, its role, and the configuration set itself. The boundary is the part that has to be
*right* before anything is deployed; the plumbing is the part that has to be deployed. Until it
is, an attester built with no authenticator refuses every delivery and the quarantine stands,
which is the correct answer rather than a degraded one.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.application.commands.reconcile_send_outcome import (
    ReconcileSendOutcome,
    ReconcileSendOutcomeCommand,
    ReconcileSendOutcomeResult,
)
from chorus.application.services.ses_events import SesEventAttester
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    ExecutionId,
    Namespace,
    Sha256Digest,
)
from chorus.ports.ses_events import SesEventTransportContext


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileFromSesEventCommand:
    """One execution to reconcile, and one delivery the transport says SES produced about it.

    Note what is **not** here: no ``message_id``, no ``execution_tag``, no
    ``configuration_set``, and no ``accepted``. Those are the four values that make an
    acceptance believable, and every one of them comes out of ``delivery.envelope`` after the
    delivery has been authenticated -- never off this command.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    execution_id: ExecutionId
    actor_id_hash: Sha256Digest
    correlation_id: UUID
    delivery: SesEventTransportContext


@dataclass(slots=True)
class ReconcileFromSesEvent:
    """Authenticate, decode, attest, then delegate. It decides nothing and it never calls SES."""

    reconcile: ReconcileSendOutcome
    attester: SesEventAttester

    async def execute(self, command: ReconcileFromSesEventCommand) -> ReconcileSendOutcomeResult:
        evidence = await self.attester.attest(command.delivery)
        return await self.reconcile.execute(
            ReconcileSendOutcomeCommand(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                action_id=command.action_id,
                execution_id=command.execution_id,
                actor_id_hash=command.actor_id_hash,
                correlation_id=command.correlation_id,
                evidence=evidence,
            )
        )


__all__ = ["ReconcileFromSesEvent", "ReconcileFromSesEventCommand"]
