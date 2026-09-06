"""The two derived values and the one payload builder, in the only place either is defined.

Both derivations are deterministic functions of durable values, so a recovery path can
recompute either without having stored it -- which is what lets reconciliation match a
configuration-set event to an execution long after the process that sent it is gone
([ADR-025](../../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md) SS 7).

The payload builder is here rather than in the adapter because *what is sent* is an
application decision and *how it is transmitted* is an adapter one. Putting the field list in
the adapter would put the single-recipient rule, the charset, and the ``Content.Simple`` choice
behind a boto3 import, where a test would have to reach through a client to read them.
"""

from __future__ import annotations

from uuid import UUID

from chorus.domain.ids import ActionId, ExecutionId, Namespace, Sha256Digest
from chorus.ports.sender import (
    EmailTag,
    ResolvedDestination,
    SendingIdentity,
    SesEmailRequest,
)
from chorus.privacy.canonical import hash_value

SEND_CLAIM_OWNER_DOMAIN = "send-claim-owner/v1"
SES_REQUEST_TOKEN_DOMAIN = "ses-request-token/v1"  # noqa: S105 - a hash domain label
EXECUTION_TAG_DOMAIN = "execution-tag/v1"
EXECUTION_TAG_NAME = "chorus_execution"

SINGLE_ATTEMPT_NUMBER = 1
"""V1 permits exactly one attempt per execution, and the derivation says so out loud.

A literal rather than a field read off the execution, because the token must be recomputable
from durable values by a recovery path that has already decided it will not attempt again.
"""


def send_claim_owner_hash(
    *,
    namespace: Namespace,
    action_id: ActionId,
    execution_id: ExecutionId,
    claim_nonce: UUID,
) -> Sha256Digest:
    """Who owns the ``APPROVED -> SENDING`` claim, as a value only one attempt can produce.

    Every other derivation on this path is a pure function of durable values, and that is
    exactly why none of them can answer this question: two workers racing one execution compute
    identical values for all of them, including ``ses_request_token_hash`` and the send
    idempotency key. A shared commit proof therefore proves that *the execution* was claimed
    and says nothing about *which attempt* claimed it -- and only the second fact authorizes a
    sender to call SES.

    ``claim_nonce`` is minted per attempt, before the claim, and is written into the row in the
    same conditional write that moves the state. Recovery then reads the durable execution and
    compares: ``SENDING`` carrying this attempt's owner means this attempt may continue;
    ``SENDING`` carrying another's means it must not.

    The nonce is hashed rather than stored, for the reason every other identifier on a
    Shareable row is: what is persisted is a digest that proves a match, never a value that
    could be presented as a credential.
    """

    return hash_value(
        {
            "domain": SEND_CLAIM_OWNER_DOMAIN,
            "namespace": namespace.value,
            "action_id": str(action_id),
            "execution_id": str(execution_id),
            "claim_nonce": str(claim_nonce),
            "attempt_number": SINGLE_ATTEMPT_NUMBER,
        }
    )


def ses_request_token_hash(
    *,
    namespace: Namespace,
    action_id: ActionId,
    execution_id: ExecutionId,
    idempotency_key: str,
) -> Sha256Digest:
    """The correlation token written into the row immediately before the SES call.

    It is **not** an SES deduplication token, because ``SendEmail`` offers no such guarantee.
    Writing it down before the call is what lets an operator tie a CloudTrail entry to an
    execution when nothing else can, and treating it as a deduplication key would be a claim
    the API cannot support.
    """

    return hash_value(
        {
            "domain": SES_REQUEST_TOKEN_DOMAIN,
            "namespace": namespace.value,
            "action_id": str(action_id),
            "execution_id": str(execution_id),
            "idempotency_key": idempotency_key,
            "attempt_number": SINGLE_ATTEMPT_NUMBER,
        }
    )


def execution_tag_value(*, namespace: Namespace, execution_id: ExecutionId) -> str:
    """The ``chorus_execution`` email-tag value: 64 bare lowercase hex characters.

    Bare hex with **no** ``sha256:`` prefix, because SES email-tag values admit only
    ``[A-Za-z0-9_-]`` and a colon would be rejected at the API. The prefix is stripped here, in
    one place, rather than at each call site where somebody would eventually forget.
    """

    digest = hash_value(
        {
            "domain": EXECUTION_TAG_DOMAIN,
            "namespace": namespace.value,
            "execution_id": str(execution_id),
        }
    )
    return digest.value.removeprefix("sha256:")


def build_email_request(
    *,
    namespace: Namespace,
    execution_id: ExecutionId,
    identity: SendingIdentity,
    destination: ResolvedDestination,
    configuration_set: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> SesEmailRequest:
    """Assemble the frozen SESv2 ``SendEmail`` request and nothing more.

    Every value comes from exactly two places: the deterministic render output the approval
    bound, and the registry the human cannot see. There is no third source, and no field of the
    payload is editable between the preview-hash comparison and the call -- which is precisely
    what bounds the claim "approved bytes are sent bytes" to something provable.
    """

    return SesEmailRequest(
        from_email_address=identity.from_address,
        to_addresses=(destination.address,),
        reply_to_addresses=(identity.reply_to_address,),
        configuration_set_name=configuration_set,
        email_tags=(
            EmailTag(
                name=EXECUTION_TAG_NAME,
                value=execution_tag_value(namespace=namespace, execution_id=execution_id),
            ),
        ),
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


__all__ = [
    "EXECUTION_TAG_DOMAIN",
    "EXECUTION_TAG_NAME",
    "SEND_CLAIM_OWNER_DOMAIN",
    "SES_REQUEST_TOKEN_DOMAIN",
    "SINGLE_ATTEMPT_NUMBER",
    "build_email_request",
    "execution_tag_value",
    "send_claim_owner_hash",
    "ses_request_token_hash",
]
