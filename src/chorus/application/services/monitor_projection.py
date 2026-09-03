"""Build the exact bounded payload the Monitor is authorized to reason over.

Projection is a privacy decision, not a serialization convenience. What is *not* here matters
as much as what is: no contributor name, no email address, no unit label, no S3 key, no
presigned URL, no mandate record, no other community, and no case the application did not
deliberately summarize. The Monitor cannot ask for more, because it has no tool with which to
ask.

Message text is carried verbatim because the Monitor's whole job is to read it. It is carried
as *data*: the runtime renders it inside explicit untrusted-data delimiters, and no validated
field of the answer can be satisfied by text found inside a message.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.contracts.monitor import (
    MAX_MESSAGES_PER_BATCH,
    IssueType,
    MonitorAttachmentDescriptor,
    MonitorCandidateSummary,
    MonitorInput,
    MonitorMessage,
)
from chorus.domain.entities import CommunityMessage, SensitivityCategory
from chorus.domain.ids import ContributorId, EvidenceItemId

DECLARED_SENSITIVE_CATEGORIES: tuple[SensitivityCategory, ...] = (
    SensitivityCategory.IDENTITY,
    SensitivityCategory.CONTACT,
    SensitivityCategory.UNIT_LOCATION,
    SensitivityCategory.HEALTH,
    SensitivityCategory.MINOR,
    SensitivityCategory.PRIVATE_QUOTE,
)
"""The categories the Monitor is asked to flag when it sees them in a message.

Naming them is not permission to disclose them. Flagging exists so a contributor's mandate
thread can later show exactly which sensitive detail a decision is about.
"""


class ProjectionError(ValueError):
    """A message could not be projected safely; the batch is refused rather than trimmed."""


class UnattributableBatchError(ProjectionError):
    """Nothing in this batch could be attributed to a contributor.

    Kept separate from every other projection failure because it is not a fault. A batch of
    messages whose authors are unknown is ambient noise: the Monitor's report proposals are
    contributor-owned, so there is nothing here that could ever become a report somebody owns
    or a mandate somebody can decide. The honest outcome is a run that succeeds having changed
    nothing, not a crashed worker and an operation stranded in RUNNING.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorProjection:
    """The payload plus the lookups deterministic validation will need afterwards.

    The validator is given the same projection object the runtime was given, so "the model
    cited something that was in its input" is checked against the input that actually
    existed, not a reconstruction of it.
    """

    payload: MonitorInput
    contributor_by_pseudonym: dict[str, ContributorId]
    skipped_unattributed_message_ids: tuple[UUID, ...]


def project_monitor_input(
    *,
    messages: tuple[CommunityMessage, ...],
    pseudonyms: dict[ContributorId, str],
    attachments: dict[EvidenceItemId, MonitorAttachmentDescriptor],
    candidate_summaries: tuple[MonitorCandidateSummary, ...] = (),
) -> MonitorProjection:
    """Project stored messages into the bounded Monitor payload.

    A message whose contributor is unknown is left out rather than attributed to a placeholder.
    The Monitor's report proposals are contributor-owned, so an unattributable message could
    only ever produce a report nobody owns -- and a mandate nobody can decide.
    """

    if not messages:
        raise ProjectionError("a Monitor batch requires at least one message")
    if len(messages) > MAX_MESSAGES_PER_BATCH:
        raise ProjectionError("a Monitor batch exceeds the frozen message bound")

    projected: list[MonitorMessage] = []
    skipped: list[UUID] = []
    used_pseudonyms: dict[str, ContributorId] = {}
    for message in messages:
        contributor_id = message.contributor_id
        pseudonym = None if contributor_id is None else pseudonyms.get(contributor_id)
        if contributor_id is None or pseudonym is None:
            skipped.append(message.message_id.value)
            continue
        existing = used_pseudonyms.setdefault(pseudonym, contributor_id)
        if existing != contributor_id:
            # Two contributors sharing one pseudonym would make ownership unprovable: a
            # report citing that pseudonym could belong to either person.
            raise ProjectionError("pseudonyms must identify exactly one contributor")
        projected.append(
            MonitorMessage(
                message_id=message.message_id.value,
                channel_message_id=message.channel_message_id,
                contributor_pseudonym_id=pseudonym,
                sent_at=message.sent_at,
                text=message.raw_text.reveal(),
                attachment_descriptors=tuple(
                    _descriptor(attachments, evidence_id) for evidence_id in message.attachment_ids
                ),
            )
        )

    if not projected:
        raise UnattributableBatchError("no message in this batch could be attributed")

    payload = MonitorInput(
        messages=tuple(projected),
        candidate_case_summaries=candidate_summaries,
        known_sensitive_categories=DECLARED_SENSITIVE_CATEGORIES,
        allowed_issue_types=(IssueType.ELEVATOR_FAILURE, IssueType.OTHER),
    )
    return MonitorProjection(
        payload=payload,
        contributor_by_pseudonym=dict(used_pseudonyms),
        skipped_unattributed_message_ids=tuple(skipped),
    )


def _descriptor(
    attachments: dict[EvidenceItemId, MonitorAttachmentDescriptor], evidence_id: EvidenceItemId
) -> MonitorAttachmentDescriptor:
    descriptor = attachments.get(evidence_id)
    if descriptor is None:
        # Failing closed here keeps an attachment the application cannot describe safely from
        # reaching an agent as a bare identifier it might then cite.
        raise ProjectionError("attachment descriptor is unavailable")
    return descriptor
