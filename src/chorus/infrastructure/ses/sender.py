"""The SESv2 client boundary: build the request, call once, classify what came back.

One call, and the SDK is pinned so it stays one
------------------------------------------------
``total_max_attempts=1``. Without it botocore retries a throttle or a 5xx several times
underneath the single deliberate attempt this system believes it is making, and the whole safety
property is about the number of deliberate attempts. A retry inside the SDK is a second attempt
the caller cannot see and cannot record.

``Content.Simple``, never ``Content.Raw``
------------------------------------------
SES composes the ``multipart/alternative`` structure and every header itself, so this module
never builds a header line and header injection is structurally impossible rather than merely
defended against. There is no code path here that produces MIME.

No deployment happens in Phase 8. This artifact exists, is asserted, and is wired; the deployed
function and the live send belong to Phase 11.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chorus.infrastructure.ses.classification import classify_exception
from chorus.ports.sender import SesAccepted, SesEmailRequest, SesOutcome, SesUnknown

SESV2_SERVICE_NAME = "sesv2"
"""The boto3 client name. The IAM action it authorizes under is ``ses:SendEmail``.

Those differ, and the difference has bitten this repository once already: ``sesv2:`` names no
real IAM action, so a deny list using it is inert and an *allow* using it would be a silent
hole. The client name is ``sesv2``; the policy says ``ses:``.
"""


def build_send_email_arguments(request: SesEmailRequest) -> dict[str, Any]:
    """The frozen SESv2 ``SendEmail`` keyword arguments, field for field.

    Kept as a pure function so a golden test can assert the exact literal shape -- every key
    present, every omission absent -- without a client, credentials, or a network. The
    omissions are the interesting half: ``CcAddresses``, ``BccAddresses``,
    ``FeedbackForwardingEmailAddress``, ``ListManagementOptions``, ``Content.Raw``, and
    ``Content.Template`` do not appear, and there is no branch here that could add one.
    """

    return {
        "FromEmailAddress": request.from_email_address,
        "Destination": {"ToAddresses": list(request.to_addresses)},
        "ReplyToAddresses": list(request.reply_to_addresses),
        "ConfigurationSetName": request.configuration_set_name,
        "EmailTags": [{"Name": tag.name, "Value": tag.value} for tag in request.email_tags],
        "Content": {
            "Simple": {
                "Subject": {"Data": request.subject, "Charset": request.charset},
                "Body": {
                    "Text": {"Data": request.text_body, "Charset": request.charset},
                    "Html": {"Data": request.html_body, "Charset": request.charset},
                },
            }
        },
    }


@dataclass(slots=True)
class SesV2EmailSender:
    """Send one email through SESv2 and classify the outcome. Never retries, never raises."""

    client: Any
    """A boto3 ``sesv2`` client, injected rather than constructed.

    Injected because the composition root is where a client's region, endpoint, and retry
    configuration are decided, and because a class that built its own client could not be
    handed a stub in a test without patching a module.
    """

    async def send(self, request: SesEmailRequest) -> SesOutcome:
        """Make exactly one deliberate ``SendEmail`` call.

        Every exception is classified rather than propagated, which is the port's contract:
        an exception escaping here would have to be classified by the caller anyway, and two
        classifications of one event is one too many. The unknown side is the default, so an
        exception class nobody enumerated quarantines the execution rather than being read as
        a failure that never reached SES.
        """

        arguments = build_send_email_arguments(request)
        try:
            response = self.client.send_email(**arguments)
        except Exception as error:
            return classify_exception(error)
        message_id = (response or {}).get("MessageId")
        if not message_id:
            # A 200 with no identifier is not an acceptance this system can record, and it is
            # not proof of refusal either. Unknown, by the same rule as everything else that
            # is neither proof.
            return SesUnknown()
        return SesAccepted(message_id=str(message_id))


__all__ = ["SESV2_SERVICE_NAME", "SesV2EmailSender", "build_send_email_arguments"]
