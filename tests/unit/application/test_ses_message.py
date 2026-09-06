"""The frozen SESv2 payload, field for field, and the two derivations that feed it.

A golden test over the literal argument dictionary, because the omissions carry as much of the
contract as the fields do: ``Cc``, ``Bcc``, ``FeedbackForwardingEmailAddress``,
``ListManagementOptions``, ``Content.Raw``, and ``Content.Template`` must not appear, and a
test that only checked what *is* present would pass while any of them was added.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from chorus.application.services.ses_message import (
    EXECUTION_TAG_NAME,
    SINGLE_ATTEMPT_NUMBER,
    build_email_request,
    execution_tag_value,
    ses_request_token_hash,
)
from chorus.domain.entities import DestinationKind
from chorus.domain.ids import ActionId, DestinationId, ExecutionId, Namespace
from chorus.infrastructure.ses.sender import build_send_email_arguments
from chorus.ports.sender import ResolvedDestination, SendingIdentity, SesEmailRequest

NAMESPACE = Namespace("TEST_SES")
ACTION_ID = ActionId(UUID("11111111-1111-4111-8111-111111111111"))
EXECUTION_ID = ExecutionId(UUID("22222222-2222-4222-8222-222222222222"))
ROUTING_TOKEN = UUID("33333333-3333-4333-8333-333333333333")

IDENTITY = SendingIdentity(
    identity_id="chorus-demo-sender",
    from_address="chorus@chorus.invalid",
    reply_to_address="chorus-replies@chorus.invalid",
    identity_arn="arn:aws:ses:us-east-1:000000000000:identity/chorus-demo-sender",
)
DESTINATION = ResolvedDestination(
    destination_id=DestinationId("property_manager:demo"),
    kind=DestinationKind.PROPERTY_MANAGER,
    registry_version=1,
    routing_token=ROUTING_TOKEN,
    display_label="Property Management",
    address="property-manager@chorus.invalid",
)


def _request() -> SesEmailRequest:
    return build_email_request(
        namespace=NAMESPACE,
        execution_id=EXECUTION_ID,
        identity=IDENTITY,
        destination=DESTINATION,
        configuration_set="chorus-test",
        subject="Elevator repair request",
        text_body="Hello,\n\nPlease inspect the elevator.\n",
        html_body="<h1>Elevator repair request</h1>",
    )


def test_the_send_email_arguments_are_the_frozen_shape_exactly() -> None:
    """A literal golden. Every key, every nesting, both charsets, and nothing else."""

    arguments = build_send_email_arguments(_request())

    assert arguments == {
        "FromEmailAddress": "chorus@chorus.invalid",
        "Destination": {"ToAddresses": ["property-manager@chorus.invalid"]},
        "ReplyToAddresses": ["chorus-replies@chorus.invalid"],
        "ConfigurationSetName": "chorus-test",
        "EmailTags": [
            {
                "Name": EXECUTION_TAG_NAME,
                "Value": execution_tag_value(namespace=NAMESPACE, execution_id=EXECUTION_ID),
            }
        ],
        "Content": {
            "Simple": {
                "Subject": {
                    "Data": "Elevator repair request",
                    "Charset": "UTF-8",
                },
                "Body": {
                    "Text": {
                        "Data": "Hello,\n\nPlease inspect the elevator.\n",
                        "Charset": "UTF-8",
                    },
                    "Html": {"Data": "<h1>Elevator repair request</h1>", "Charset": "UTF-8"},
                },
            }
        },
    }


def test_the_payload_never_carries_raw_template_cc_or_bcc() -> None:
    """The omissions, asserted as omissions.

    ``Content.Simple`` means SES composes the ``multipart/alternative`` structure and every
    header itself, so header injection is structurally impossible rather than merely defended
    against. There is no branch in the builder that could produce ``Raw``.
    """

    arguments = build_send_email_arguments(_request())

    assert set(arguments["Content"]) == {"Simple"}
    assert set(arguments["Destination"]) == {"ToAddresses"}
    for absent in ("FeedbackForwardingEmailAddress", "ListManagementOptions"):
        assert absent not in arguments


@pytest.mark.parametrize(
    "recipients",
    [
        pytest.param((), id="none"),
        pytest.param(("a@example.invalid", "b@example.invalid"), id="two"),
    ],
)
def test_exactly_one_recipient_is_asserted_at_construction(
    recipients: tuple[str, ...],
) -> None:
    """The single-recipient rule, enforced where it can be without publishing an address.

    Narrowing the IAM grant with ``ses:Recipients`` would put the demo destination address into
    a synthesized CloudFormation template -- a build artifact that gets read and diffed -- so
    the rule lives here instead, in the payload type itself.
    """

    with pytest.raises(ValueError, match="exactly one recipient"):
        SesEmailRequest(
            from_email_address=IDENTITY.from_address,
            to_addresses=recipients,
            reply_to_addresses=(IDENTITY.reply_to_address,),
            configuration_set_name="chorus-test",
            email_tags=(),
            subject="s",
            text_body="t",
            html_body="h",
        )


def test_the_execution_tag_is_bare_hex_ses_will_accept() -> None:
    """64 lowercase hex characters and no ``sha256:`` prefix.

    SES email-tag values admit only ``[A-Za-z0-9_-]``, so a colon would be rejected at the API.
    Frozen here rather than discovered at the first live send.
    """

    value = execution_tag_value(namespace=NAMESPACE, execution_id=EXECUTION_ID)

    assert len(value) == 64
    assert ":" not in value
    assert value == value.lower()
    assert all(character in "0123456789abcdef" for character in value)


def test_the_execution_tag_is_deterministic_and_per_execution() -> None:
    """Recomputable by a recovery path from durable values, and distinct per execution."""

    other = ExecutionId(UUID("44444444-4444-4444-8444-444444444444"))

    assert execution_tag_value(
        namespace=NAMESPACE, execution_id=EXECUTION_ID
    ) == execution_tag_value(namespace=NAMESPACE, execution_id=EXECUTION_ID)
    assert execution_tag_value(
        namespace=NAMESPACE, execution_id=EXECUTION_ID
    ) != execution_tag_value(namespace=NAMESPACE, execution_id=other)


def test_the_request_token_binds_the_attempt_and_is_not_a_deduplication_key() -> None:
    """Deterministic over durable values, and pinned to attempt one.

    It is a correlation value: SES ``SendEmail`` offers no client token and no idempotent
    replay, so treating this as a deduplication key would be a claim the API cannot support.
    Writing it before the call is what lets an operator tie a CloudTrail entry to an execution.
    """

    first = ses_request_token_hash(
        namespace=NAMESPACE,
        action_id=ACTION_ID,
        execution_id=EXECUTION_ID,
        idempotency_key="send-key",
    )
    second = ses_request_token_hash(
        namespace=NAMESPACE,
        action_id=ACTION_ID,
        execution_id=EXECUTION_ID,
        idempotency_key="send-key",
    )
    different = ses_request_token_hash(
        namespace=NAMESPACE,
        action_id=ACTION_ID,
        execution_id=EXECUTION_ID,
        idempotency_key="another-key",
    )

    assert first == second
    assert first != different
    assert SINGLE_ATTEMPT_NUMBER == 1
