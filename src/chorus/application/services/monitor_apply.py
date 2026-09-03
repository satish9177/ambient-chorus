"""Turn one validated Monitor answer into bounded, ordered, replay-safe apply steps.

Nothing here reads the model's output. It reads the *validated* output, which is a different
thing: by this point every citation has been proved to exist, every owner has been resolved to
a real contributor, and every typed value has been mapped onto a closed domain variant. What
remains is arithmetic -- derive identity, check the gates, build entities, and cut the work
into transactions that fit.

Why steps rather than one transaction
-------------------------------------
At the frozen Monitor maxima one answer implies up to 25 reports, 50 feed signals, and 100
facts, plus case rows, an invocation record, and an audit event. That is comfortably past
DynamoDB's hundred-operation limit, so a single transaction cannot exist at the contract
bounds -- and lowering the bounds until it fits would be changing the contract to suit the
implementation.

So the answer is applied as a deterministic ordered sequence of bounded steps, each one its
own transaction, each one advancing a durable progress record inside that same transaction.
The result is exactly the property the frozen design asks for: a storage failure may leave
partial durable *progress*, and a retry then completes only the missing steps, but no partial
acceptance of an invalid answer is possible because nothing at all is written until the whole
answer has passed validation and every gate below.

The gates, and why each one fails closed
----------------------------------------
* **candidate threshold.** A new case needs at least two related report proposals. A group
  that does not meet it produces no durable state at all -- not a case, not a report, not a
  fact -- because a report has no address outside a case partition and a lone proposal is not
  a discovered pattern. Its messages stay ordinary community messages and remain eligible for
  a later run's context window, so the observation is deferred rather than discarded.
* **case state.** Attaching a report to an existing case mutates it, so it is gated by state.
  A `RESOLVED` or `CLOSED_UNRESOLVED` case is reopened only by an explicit human command, and
  intake has no such authority; proposing one denies the whole apply and leaves that case's
  state, reason code, and version exactly as they were.
* **case version.** The link is denied unless the case is still at the version the agent was
  actually shown. Refreshing the expected version at apply time and proceeding would compare
  the case against itself and call that agreement, while the reasoning that produced the link
  had in fact read a case that no longer exists.
* **named subject.** A case is a merge -- the threshold above needs two reports -- and two
  reports may only be merged under an issue type that names what went wrong. ``OTHER`` names
  nothing, so no ``OTHER`` case is derived an address here and no stored case whose issue type
  names no subject may take a further report, whatever the answer proposes (ADR-012).
* **existing linkage.** A message already bound to one case may not be proposed for another.
  Phase-3 Monitor cannot relink; that is a correction, and corrections have their own
  authority. The refusal happens here, before any write is staged, so it is a domain rule and
  not a side effect of how a storage row happens to be conditioned.
* **fact slot.** Two proposals in one answer that resolve to the same slot make the answer
  ambiguous, and a settled slot re-proposed with different content is drift. Neither
  duplicates a fact and neither overwrites one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application.services.identity import (
    derive_candidate_case_id,
    derive_fact_slot_id,
    derive_report_id,
)
from chorus.application.services.monitor_validation import (
    ValidatedCandidateGroup,
    ValidatedMonitorOutput,
    ValidatedReport,
)
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    CommunityCase,
    EvidenceStatus,
    issue_type_names_a_subject,
)
from chorus.domain.errors import DomainError, DomainErrorCode
from chorus.domain.facts import Fact, FactStatus, Report, ReportStatus
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    FactId,
    MessageId,
    Namespace,
    ReportId,
    SensitiveStr,
    Sha256Digest,
)
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentOutputDriftError,
    AgentRejection,
)
from chorus.ports.errors import ModelLimitExceededError
from chorus.ports.limits import MAX_ACTIVE_FACTS_PER_CASE
from chorus.ports.records import FeedSignalProjection
from chorus.privacy.canonical import hash_value

MIN_REPORTS_FOR_NEW_CANDIDATE = 2
"""The frozen guard for creating a ``CANDIDATE`` case.

"At least 2 potentially related report proposals" is a *pattern* threshold, not corroboration
and not a privacy threshold. Corroboration needs two independent sources and aggregate
disclosure needs three distinct contributors; neither is satisfied by getting this far.
"""

MONITOR_LINKABLE_CASE_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.CANDIDATE,
        CaseState.AWAITING_MANDATES,
        CaseState.INVESTIGATING,
        CaseState.READY_FOR_ACTION,
        CaseState.ACTION_PROPOSED,
        CaseState.ACTIONED,
        CaseState.VERIFYING,
    }
)
"""States in which intake may append a report and leave the state alone.

``RESOLVED`` and ``CLOSED_UNRESOLVED`` are deliberately absent. The frozen state machine
reopens a terminal case only through an explicit human or demo reopen command, and no
automatic human authority may be invented to stand in for one. Anything not listed here is
denied rather than defaulted, so a state added later starts out refused.
"""

MONITOR_APPLY_ITEM_BUDGET = 40
"""How many report, fact, and signal writes one apply step may carry.

Each step also writes its case row, its progress advance, and its commit proof, and the first
step of a case adds an invocation record and an audit event -- so a step is at most 45
operations against a limit of 100. The headroom is deliberate: this bound is what keeps the
implementation correct at the *contract* maxima rather than at the sizes the demo happens to
produce.

It is also at least ``MAX_PROPOSED_REPORTS``, which is what makes a new case and the whole of
its initial report linkage land in one atomic step.
"""

CANDIDATE_REASON_CODE = "MONITOR_CANDIDATE_DETECTED"
CANDIDATE_EXTENDED_REASON_CODE = "MONITOR_CANDIDATE_EXTENDED"
BELOW_THRESHOLD_REASON_CODE = "CANDIDATE_BELOW_THRESHOLD"


class MonitorApplyDenial(StrEnum):
    """Why a validated answer could not be applied to the world as it currently stands.

    These are not contract violations: the answer was well formed and semantically valid. The
    world moved, or the answer asked for authority intake does not have. They are separate
    from :class:`~chorus.ports.agents.AgentRejection` for exactly that reason -- retrying the
    model would not help, and blaming the model would misdescribe what happened.
    """

    CASE_STATE_INELIGIBLE = "CASE_STATE_INELIGIBLE"
    CASE_VERSION_STALE = "CASE_VERSION_STALE"
    REPORT_ALREADY_LINKED = "REPORT_ALREADY_LINKED"
    CASE_SUBJECT_UNNAMED = "CASE_SUBJECT_UNNAMED"


class MonitorApplyDeniedError(DomainError):
    """One validated answer was refused at an apply gate; nothing was written.

    The stale-version denial maps onto ``STALE_VERSION`` and the others onto
    ``STATE_TRANSITION_ERROR``, so the closed frozen error taxonomy still describes every
    outcome and the API keeps answering ``409`` without a new status code. ``denial`` carries
    the exact gate, which is a safe enum value and never a stored value or a quotation.
    """

    __slots__ = ("denial",)

    denial: MonitorApplyDenial

    def __init__(self, denial: MonitorApplyDenial) -> None:
        code = (
            DomainErrorCode.STALE_VERSION
            if denial is MonitorApplyDenial.CASE_VERSION_STALE
            else DomainErrorCode.STATE_TRANSITION_ERROR
        )
        super().__init__(code, denial.value)
        self.denial = denial

    @property
    def safe_code(self) -> str:
        """The closed code an operation record and a log line may carry for this refusal."""

        return self.denial.value

    def __repr__(self) -> str:
        return f"{type(self).__name__}(denial={self.denial.value!r})"


PARTIAL_APPLY_CONFLICT_CODE = "PARTIAL_APPLY_CONFLICT"


class PartialApplyConflictError(DomainError):
    """A frozen apply plan can no longer legally finish, and part of it already committed.

    This is the one outcome that must not be dressed up as either success or a clean failure.
    The invocation was valid, its plan was frozen, and some of its steps are durable and
    correct -- and then the world moved underneath the remainder: a case reached a version the
    plan does not expect, or the plan the snapshot describes is no longer the plan the current
    state implies.

    Recomputing the plan under the old invocation is exactly what must not happen. The frozen
    answer was reasoned against state that no longer exists, so re-cutting its steps against
    the new state would apply reasoning to a world it never saw. Calling the model again is
    also refused: the invocation is complete, and a second answer would be a second opinion
    charged to an identity that already has one.

    So the operation fails with a safe code that says precisely this, and the state earlier
    steps committed stays exactly as it is. It is valid state; it is simply not all of the
    state the answer intended.
    """

    __slots__ = ()

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(DomainErrorCode.STATE_TRANSITION_ERROR, entity_ref)

    @property
    def safe_code(self) -> str:
        return PARTIAL_APPLY_CONFLICT_CODE


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalWrite:
    """One feed-signal projection write, and the row version it replaces.

    ``expected_version`` is ``None`` when no projection exists yet, which is a create. When a
    projection does exist for the same case, the write is a guarded replace at that version --
    the projection is display data, so a refreshed label must be able to land rather than
    being locked out by the row that is already there.
    """

    signal: FeedSignalProjection
    expected_version: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyStep:
    """One bounded transaction's worth of an apply, and the case row it leaves behind.

    The case row is written by *every* step of its case, always listing exactly the reports
    and facts that exist once that step commits. A partially applied answer therefore leaves a
    case whose lists are a prefix of the final ones, never a case naming rows that are not
    there yet -- which is the difference between a reader seeing less than the whole story and
    a reader hitting an unresolvable identifier.
    """

    case_id: CaseId
    case: CommunityCase
    case_expected_version: int | None
    reports: tuple[Report, ...]
    facts: tuple[Fact, ...]
    signals: tuple[SignalWrite, ...]
    first_for_case: bool

    @property
    def item_count(self) -> int:
        return len(self.reports) + len(self.facts) + len(self.signals)

    @property
    def operation_count(self) -> int:
        """Exactly how many DynamoDB operations this step's transaction will contain.

        Counted here rather than measured after the fact so a bound can be asserted against
        the contract maxima without building a storage adapter to count for us: the case row,
        the items, the progress advance, the commit proof, and -- only on a case's first step
        -- its invocation record and audit event.
        """

        return 1 + self.item_count + 2 + (2 if self.first_for_case else 0)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedCase:
    """Everything one validated group intends to write for one case."""

    case_id: CaseId
    existing_case: CommunityCase | None
    base_version: int
    step_count: int
    final_case: CommunityCase
    new_reports: tuple[Report, ...]
    new_facts: tuple[Fact, ...]
    signal_writes: tuple[SignalWrite, ...]
    created: bool

    def __post_init__(self) -> None:
        if self.step_count != _step_count(self.item_total):
            raise ValueError("planned step count disagrees with the work it covers")
        if self.step_count and self.final_case.version != self.base_version + self.step_count:
            raise ValueError("final case version disagrees with the planned step count")

    @property
    def item_total(self) -> int:
        return len(self.new_reports) + len(self.new_facts) + len(self.signal_writes)

    @property
    def has_durable_effect(self) -> bool:
        return self.step_count > 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyStepDescriptor:
    """Everything about one apply step that a frozen snapshot needs in order to name it.

    Identifiers and versions only: no entity, no summary, no typed value. The descriptor is
    what a resumed attempt compares its freshly derived plan against, so it captures exactly
    the things that must not have changed -- which case, at which version, writing which rows
    -- and nothing that is merely how those rows are spelled.

    ``first_for_case`` is carried but deliberately excluded from :attr:`content`. A resumed
    attempt legitimately derives a shorter plan in which the first *remaining* step of a case
    sits at index zero, and reading that as "this is the case's first step" would append a
    second audit event and a second invocation record for one decision. The frozen
    descriptor's answer is the authoritative one.
    """

    case_id: UUID
    case_expected_version: int | None
    case_version: int
    first_for_case: bool
    report_ids: tuple[UUID, ...]
    fact_ids: tuple[UUID, ...]
    signal_message_ids: tuple[UUID, ...]
    signal_expected_versions: tuple[int | None, ...]

    @property
    def content(self) -> tuple[object, ...]:
        """The part of a step a resumed attempt has to reproduce exactly."""

        return (
            str(self.case_id),
            self.case_expected_version,
            self.case_version,
            tuple(str(value) for value in self.report_ids),
            tuple(str(value) for value in self.fact_ids),
            tuple(str(value) for value in self.signal_message_ids),
            self.signal_expected_versions,
        )

    def as_json(self) -> dict[str, object]:
        return {
            "case_id": str(self.case_id),
            "expected_version": self.case_expected_version,
            "case_version": self.case_version,
            "first_for_case": self.first_for_case,
            "reports": [str(value) for value in self.report_ids],
            "facts": [str(value) for value in self.fact_ids],
            "signals": [
                {"message_id": str(message_id), "expected_version": expected}
                for message_id, expected in zip(
                    self.signal_message_ids, self.signal_expected_versions, strict=True
                )
            ],
        }

    @classmethod
    def from_json(cls, value: object) -> ApplyStepDescriptor:
        """Rebuild one descriptor from its snapshot form, refusing anything malformed."""

        if not isinstance(value, dict):
            raise ValueError("an apply step descriptor is an object")
        message_ids: list[UUID] = []
        expected_versions: list[int | None] = []
        for signal in _as_list(value.get("signals")):
            if not isinstance(signal, dict):
                raise ValueError("a signal write descriptor is an object")
            message_ids.append(UUID(str(signal["message_id"])))
            expected = signal.get("expected_version")
            expected_versions.append(None if expected is None else int(expected))
        expected_case_version = value.get("expected_version")
        return cls(
            case_id=UUID(str(value["case_id"])),
            case_expected_version=(
                None if expected_case_version is None else int(expected_case_version)
            ),
            case_version=int(str(value["case_version"])),
            first_for_case=bool(value["first_for_case"]),
            report_ids=tuple(UUID(str(item)) for item in _as_list(value.get("reports"))),
            fact_ids=tuple(UUID(str(item)) for item in _as_list(value.get("facts"))),
            signal_message_ids=tuple(message_ids),
            signal_expected_versions=tuple(expected_versions),
        )


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("an apply step descriptor names a list")
    return value


def describe_step(step: ApplyStep) -> ApplyStepDescriptor:
    """Reduce one planned step to the identifiers and versions a snapshot may hold."""

    return ApplyStepDescriptor(
        case_id=step.case_id.value,
        case_expected_version=step.case_expected_version,
        case_version=step.case.version,
        first_for_case=step.first_for_case,
        report_ids=tuple(report.report_id.value for report in step.reports),
        fact_ids=tuple(fact.fact_id.value for fact in step.facts),
        signal_message_ids=tuple(write.signal.message_id.value for write in step.signals),
        signal_expected_versions=tuple(write.expected_version for write in step.signals),
    )


def plan_hash_of(descriptors: tuple[ApplyStepDescriptor, ...]) -> Sha256Digest:
    """Hash one ordered plan description, wherever that description came from.

    Shared by the planner and by the snapshot reader on purpose: a resumed attempt has to
    compare its own plan against a stored one, and two hash functions that were supposed to
    agree are exactly the kind of thing that silently stops agreeing.
    """

    return hash_value(
        {
            "schema": "monitor-apply-plan/v1",
            "steps": [descriptor.as_json() for descriptor in descriptors],
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorApplicationPlan:
    """The complete intent of one validated answer, before any storage call."""

    cases: tuple[PlannedCase, ...]
    steps: tuple[ApplyStep, ...]
    skipped_below_threshold: int
    provisional_message_ids: tuple[MessageId, ...]

    @property
    def has_durable_effect(self) -> bool:
        return bool(self.steps)

    @property
    def max_operation_count(self) -> int:
        return max((step.operation_count for step in self.steps), default=0)

    @property
    def descriptors(self) -> tuple[ApplyStepDescriptor, ...]:
        return tuple(describe_step(step) for step in self.steps)

    @property
    def plan_hash(self) -> Sha256Digest:
        """Bind a resumed attempt to the exact ordered plan its progress describes.

        Progress says "three of five steps committed", which is only meaningful against the
        plan those five steps came from. A resumed attempt that derived a different plan -- a
        different grouping, different identities, a different base version -- must not skip
        three steps on the strength of somebody else's progress, so it compares hashes and
        starts over rather than guessing.
        """

        return plan_hash_of(self.descriptors)


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentApplyState:
    """Strongly read state the gates and the plan are decided against.

    Loaded once, before anything is written, and passed in rather than fetched here: this
    module stays a pure function of validated output plus observed state, which is what makes
    the whole plan -- including its step boundaries -- reproducible in a test without a
    storage driver.
    """

    cases: dict[CaseId, CommunityCase]
    signals: dict[MessageId, FeedSignalProjection]
    facts: dict[FactId, Fact]


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupIdentity:
    """Every durable identifier one validated group implies, derived without storage.

    Loading has to happen before planning, because every gate compares the answer against
    stored state -- but the identifiers to load are only knowable from the answer. This is
    that derivation, done once and shared, so the strong reads that follow are bounded and
    exact rather than a query over whatever the community happens to contain, and so the
    planner cannot possibly derive a different identifier than the loader looked for.

    ``case_id`` is ``None`` for a group that does not meet the candidate threshold. Such a
    group has no address: its reports stay provisional, nothing is loaded for it, and nothing
    is written.
    """

    group: ValidatedCandidateGroup
    members: tuple[ValidatedReport, ...]
    case_id: CaseId | None
    report_ids: dict[str, ReportId]
    fact_slot_ids: tuple[FactId, ...]
    message_ids: tuple[MessageId, ...]


def derive_identities(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    validated: ValidatedMonitorOutput,
) -> tuple[GroupIdentity, ...]:
    """Derive the case, report, fact-slot, and message identity of every validated group."""

    reports_by_ref = {report.client_ref: report for report in validated.reports}
    identities: list[GroupIdentity] = []
    for group in validated.groups:
        members = tuple(
            reports_by_ref[client_ref]
            for client_ref in group.report_client_refs
            if client_ref in reports_by_ref
        )
        if not members:
            continue
        report_ids = {
            member.client_ref: derive_report_id(
                namespace=namespace,
                community_id=community_id,
                contributor_id=member.contributor_id,
                issue_type=member.issue_type,
                source_message_ids=member.source_message_ids,
            )
            for member in members
        }
        case_id = group.existing_case_id
        if (
            case_id is None
            and len(set(report_ids.values())) >= MIN_REPORTS_FOR_NEW_CANDIDATE
            # ADR-012: a case is a merge, and a merge needs a subject the vocabulary names.
            # The validator has already refused a multi-member group under ``OTHER``, so
            # reaching here with one would mean the two disagreed; deriving no address is the
            # fail-closed half of that disagreement rather than the permissive half.
            and issue_type_names_a_subject(group.issue_type)
        ):
            case_id = derive_candidate_case_id(
                namespace=namespace,
                community_id=community_id,
                issue_type=group.issue_type,
                report_ids=tuple(sorted(set(report_ids.values()), key=str)),
            )
        slots = tuple(
            sorted(
                {
                    derive_fact_slot_id(
                        namespace=namespace,
                        community_id=community_id,
                        report_id=report_ids[fact.report_client_ref],
                        fact_type=fact.fact_type,
                        source_message_ids=fact.source_message_ids,
                        evidence_ids=fact.evidence_ids,
                    )
                    for fact in validated.facts
                    if fact.report_client_ref in report_ids
                },
                key=str,
            )
        )
        identities.append(
            GroupIdentity(
                group=group,
                members=members,
                case_id=case_id,
                report_ids=report_ids,
                fact_slot_ids=slots,
                message_ids=tuple(
                    sorted(
                        {
                            message_id
                            for member in members
                            for message_id in member.source_message_ids
                        },
                        key=str,
                    )
                ),
            )
        )
    return tuple(identities)


def intended_case_ids(identities: tuple[GroupIdentity, ...]) -> tuple[CaseId, ...]:
    """Every case identity these groups could address, in a stable order."""

    return tuple(
        sorted(
            {identity.case_id for identity in identities if identity.case_id is not None}, key=str
        )
    )


def intended_message_ids(identities: tuple[GroupIdentity, ...]) -> tuple[MessageId, ...]:
    """Every message an addressable group would attach a signal to, in a stable order."""

    return tuple(
        sorted(
            {
                message_id
                for identity in identities
                if identity.case_id is not None
                for message_id in identity.message_ids
            },
            key=str,
        )
    )


def intended_fact_slot_ids(identities: tuple[GroupIdentity, ...]) -> tuple[FactId, ...]:
    """Every fact slot an addressable group could occupy, in a stable order."""

    return tuple(
        sorted(
            {
                fact_id
                for identity in identities
                if identity.case_id is not None
                for fact_id in identity.fact_slot_ids
            },
            key=str,
        )
    )


def plan_monitor_application(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    validated: ValidatedMonitorOutput,
    identities: tuple[GroupIdentity, ...],
    current: CurrentApplyState,
    now: datetime,
) -> MonitorApplicationPlan:
    """Compose the durable intent of one validated Monitor answer, or refuse it."""

    planned: list[PlannedCase] = []
    skipped = 0
    provisional: list[MessageId] = []

    for identity in identities:
        if identity.case_id is None:
            # Provisional, not lost. Nothing durable is written, and the cited messages stay
            # ordinary community messages that a later run's recent-message window will show
            # the Monitor again alongside whatever corroborates them.
            skipped += 1
            provisional.extend(identity.message_ids)
            continue
        existing = current.cases.get(identity.case_id)
        _check_case_eligibility(group=identity.group, existing=existing)
        planned.append(
            _plan_case(
                namespace=namespace,
                community_id=community_id,
                identity=identity,
                validated=validated,
                existing=existing,
                current=current,
                now=now,
            )
        )

    _check_projected_case_capacity(planned)
    return MonitorApplicationPlan(
        cases=tuple(planned),
        steps=_cut_steps(planned),
        skipped_below_threshold=skipped,
        provisional_message_ids=tuple(sorted(set(provisional), key=str)),
    )


def _check_projected_case_capacity(planned: list[PlannedCase]) -> None:
    """Refuse a whole answer whose *resulting* case would exceed a frozen V1 bound.

    Checking only how much an answer proposes is not a capacity check. A case already holding
    ninety-seven facts and an answer proposing four more is within every per-output bound and
    still lands a case at a hundred and one, which the storage layer would then reject
    mid-apply -- after earlier steps had already committed. So the bound is evaluated here,
    against the state the plan would actually leave behind, before the plan is frozen and
    before the first mutation.

    One hundred active facts is the only frozen per-case bound an intake apply can approach;
    views, actions, and commitments are not intake's to create.

    The totals are counted from the *derived* identifiers rather than from proposal counts, so a
    replayed answer whose reports and fact slots already occupy their addresses counts them
    once rather than twice. An exact replay of an answer that fit therefore still fits.

    Refusal is whole-output, like every other gate: no report, no fact, no signal, no case
    mutation, and no progress row.
    """

    for case in planned:
        if len(case.final_case.fact_ids) > MAX_ACTIVE_FACTS_PER_CASE:
            raise ModelLimitExceededError("CASE_FACTS")


def _check_case_eligibility(
    *, group: ValidatedCandidateGroup, existing: CommunityCase | None
) -> None:
    """Refuse before planning anything when the world will not accept this link."""

    if group.existing_case_id is None:
        # A derived candidate identifier that already names a stored case means this exact
        # grouping was applied before, so the case is a replay target rather than a new one.
        # It still has to be in a state that accepts reports.
        if existing is not None and existing.state not in MONITOR_LINKABLE_CASE_STATES:
            raise MonitorApplyDeniedError(MonitorApplyDenial.CASE_STATE_INELIGIBLE)
        return
    if existing is None:
        # The summary named a case that the strong read cannot find. Treating that as "create
        # it" would let a stale summary manufacture a case at an identifier the model chose.
        raise MonitorApplyDeniedError(MonitorApplyDenial.CASE_VERSION_STALE)
    if existing.state not in MONITOR_LINKABLE_CASE_STATES:
        raise MonitorApplyDeniedError(MonitorApplyDenial.CASE_STATE_INELIGIBLE)
    if not issue_type_names_a_subject(existing.issue_type):
        # ADR-012, decided against the *stored* case rather than against the answer. The
        # validator already refuses this link, and it refuses it from the candidate summary
        # the agent was shown. This gate asks the case itself, so a case whose issue type
        # names no subject cannot take a second report however it came to exist -- an earlier
        # release, a seed, a fixture, or a summary that no longer matches the row.
        raise MonitorApplyDeniedError(MonitorApplyDenial.CASE_SUBJECT_UNNAMED)
    if existing.version != group.expected_case_version:
        raise MonitorApplyDeniedError(MonitorApplyDenial.CASE_VERSION_STALE)


def _plan_case(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    identity: GroupIdentity,
    validated: ValidatedMonitorOutput,
    existing: CommunityCase | None,
    current: CurrentApplyState,
    now: datetime,
) -> PlannedCase:
    """Plan one case in the one order that has no circular dependency.

    Reports, then facts, then the *decisions* about which signals need writing -- none of
    which depend on the case version. Only then is the amount of work known, which fixes the
    step count, which fixes the version the case ends at, which is what the signal rows
    finally record. Deciding signals against a version that the signals themselves help
    determine would be a loop with no fixed point.
    """

    group = identity.group
    members = identity.members
    report_ids = identity.report_ids
    case_id = identity.case_id
    assert case_id is not None
    known_report_ids = set(existing.report_ids) if existing is not None else set()
    known_fact_ids = set(existing.fact_ids) if existing is not None else set()

    new_reports = _plan_reports(
        namespace=namespace,
        community_id=community_id,
        members=members,
        report_ids=report_ids,
        known_report_ids=known_report_ids,
        case_id=case_id,
        now=now,
    )
    new_facts = _plan_facts(
        namespace=namespace,
        community_id=community_id,
        members=members,
        report_ids=report_ids,
        validated=validated,
        case_id=case_id,
        known_fact_ids=known_fact_ids,
        current=current,
        now=now,
    )

    related_messages = identity.message_ids
    # The label and state a refreshed signal would carry are the *existing* case's when there
    # is one, and the proposed group's otherwise. Neither depends on the version, which is
    # what keeps the decision independent of the step count it helps determine.
    decisions = _plan_signal_decisions(
        case_id=case_id,
        case_title=group.title if existing is None else existing.title,
        case_state=CaseState.CANDIDATE if existing is None else existing.state,
        related_messages=related_messages,
        current=current,
    )

    base_version = 0 if existing is None else existing.version
    step_count = _step_count(len(new_reports) + len(new_facts) + len(decisions))
    final_version = base_version + step_count

    all_report_ids = tuple(
        sorted(known_report_ids | {report.report_id for report in new_reports}, key=str)
    )
    all_fact_ids = tuple(sorted(known_fact_ids | {fact.fact_id for fact in new_facts}, key=str))

    if existing is None:
        final_case = CommunityCase(
            case_id=case_id,
            community_id=community_id,
            namespace=namespace,
            title=group.title,
            issue_type=group.issue_type,
            state=CaseState.CANDIDATE,
            report_ids=all_report_ids,
            fact_ids=all_fact_ids,
            assessment_id=None,
            current_view_id=None,
            current_action_id=None,
            # Independence is recomputed by the Investigator from evidence roots and
            # contributors. Intake claims nothing about it.
            corroboration_source_count=0,
            state_reason_code=CANDIDATE_REASON_CODE,
            version=final_version,
            created_at=now,
            updated_at=now,
        )
    elif step_count == 0:
        # Nothing new: the case is left byte for byte as it was. Bumping its version here
        # would stale every authorization artifact bound to it and write a reason code
        # describing a linkage that did not happen.
        final_case = existing
    else:
        final_case = replace(
            existing,
            report_ids=all_report_ids,
            fact_ids=all_fact_ids,
            state_reason_code=CANDIDATE_EXTENDED_REASON_CODE,
            version=final_version,
            updated_at=now,
        )

    signal_writes = tuple(
        SignalWrite(
            signal=FeedSignalProjection(
                namespace=namespace,
                community_id=community_id,
                message_id=decision.message_id,
                case_id=case_id,
                case_version=final_case.version,
                label=final_case.title,
                related_message_count=len(related_messages),
                case_state=final_case.state,
                detected_at=decision.detected_at or now,
                version=1 if decision.expected_version is None else decision.expected_version + 1,
            ),
            expected_version=decision.expected_version,
        )
        for decision in decisions
    )

    return PlannedCase(
        case_id=case_id,
        existing_case=existing,
        base_version=base_version,
        step_count=step_count,
        final_case=final_case,
        new_reports=new_reports,
        new_facts=new_facts,
        signal_writes=signal_writes,
        created=existing is None,
    )


def _step_count(item_total: int) -> int:
    """How many bounded transactions ``item_total`` writes need. Zero work needs zero steps."""

    return -(-item_total // MONITOR_APPLY_ITEM_BUDGET)


def _plan_reports(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    members: tuple[ValidatedReport, ...],
    report_ids: dict[str, ReportId],
    known_report_ids: set[ReportId],
    case_id: CaseId,
    now: datetime,
) -> tuple[Report, ...]:
    new_reports: list[Report] = []
    for member in sorted(members, key=lambda item: str(report_ids[item.client_ref])):
        report_id = report_ids[member.client_ref]
        if report_id in known_report_ids:
            continue
        new_reports.append(
            Report(
                report_id=report_id,
                case_id=case_id,
                community_id=community_id,
                contributor_id=member.contributor_id,
                namespace=namespace,
                source_message_ids=member.source_message_ids,
                issue_type=member.issue_type,
                private_summary=SensitiveStr(member.summary),
                occurred_at=member.occurred_at,
                location_area=member.location_area,
                evidence_ids=member.evidence_ids,
                status=ReportStatus.ACTIVE,
                duplicate_of_report_id=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
    return tuple(new_reports)


def _plan_facts(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    members: tuple[ValidatedReport, ...],
    report_ids: dict[str, ReportId],
    validated: ValidatedMonitorOutput,
    case_id: CaseId,
    known_fact_ids: set[FactId],
    current: CurrentApplyState,
    now: datetime,
) -> tuple[Fact, ...]:
    """Resolve each proposed fact onto its slot, refusing ambiguity and drift."""

    owners = {member.client_ref: member for member in members}
    new_facts: list[Fact] = []
    seen_slots: set[FactId] = set()

    for proposed in validated.facts:
        owning_report_id = report_ids.get(proposed.report_client_ref)
        if owning_report_id is None:
            continue
        owner = owners[proposed.report_client_ref]
        fact_id = derive_fact_slot_id(
            namespace=namespace,
            community_id=community_id,
            report_id=owning_report_id,
            fact_type=proposed.fact_type,
            source_message_ids=proposed.source_message_ids,
            evidence_ids=proposed.evidence_ids,
        )
        if fact_id in seen_slots:
            # One answer proposing two facts for one slot has not told us which of them the
            # report asserts. Choosing the first, the last, or the "better" one would be this
            # module inventing an answer the model did not give.
            raise AgentContractViolationError((AgentRejection.AMBIGUOUS_FACT_SLOT,))
        seen_slots.add(fact_id)

        candidate = Fact(
            fact_id=fact_id,
            case_id=case_id,
            report_id=owning_report_id,
            community_id=community_id,
            contributor_id=owner.contributor_id,
            namespace=namespace,
            fact_type=proposed.fact_type,
            value=proposed.value,
            sensitivity=proposed.sensitivity,
            evidence_ids=proposed.evidence_ids,
            # Intake never asserts corroboration. Every fact starts REPORTED and only the
            # Investigator's validated assessment may move it.
            evidence_status=EvidenceStatus.REPORTED,
            source_message_ids=proposed.source_message_ids,
            supersedes_fact_id=None,
            status=FactStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )
        if fact_id not in known_fact_ids:
            new_facts.append(candidate)
            continue
        stored = current.facts.get(fact_id)
        if stored is None or not _same_fact_content(stored, candidate):
            # The slot is settled and the answer disagrees with it. Writing would either
            # duplicate an immutable fact or overwrite one with a later model's opinion.
            raise AgentOutputDriftError()
    return tuple(new_facts)


def _same_fact_content(stored: Fact, proposed: Fact) -> bool:
    """Compare only the immutable assertion, not the row's own bookkeeping.

    ``version``, ``created_at``, ``updated_at``, and ``evidence_status`` are excluded on
    purpose: the first three are storage bookkeeping, and evidence status belongs to the
    Investigator. A fact the Investigator has since corroborated must still read as an exact
    replay of what intake asserted, or every later Monitor run would report drift.
    """

    return (
        stored.report_id == proposed.report_id
        and stored.contributor_id == proposed.contributor_id
        and stored.case_id == proposed.case_id
        and stored.fact_type is proposed.fact_type
        and stored.value == proposed.value
        and stored.sensitivity is proposed.sensitivity
        and stored.evidence_ids == proposed.evidence_ids
        and stored.source_message_ids == proposed.source_message_ids
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _SignalDecision:
    """One message that needs a signal write, and the row version the write replaces."""

    message_id: MessageId
    expected_version: int | None
    detected_at: datetime | None


def _plan_signal_decisions(
    *,
    case_id: CaseId,
    case_title: str,
    case_state: CaseState,
    related_messages: tuple[MessageId, ...],
    current: CurrentApplyState,
) -> tuple[_SignalDecision, ...]:
    """Decide, per message, whether to create, refresh, replay, or refuse a signal.

    Deliberately independent of the case version, so the amount of work is knowable before
    the version the work produces. An existing signal for a *different* case denies the whole
    apply here, on the domain rule, rather than through a create-only storage condition --
    that condition also blocked the legitimate later correction path, and a projection row is
    not allowed to be what makes valid domain state unreachable.

    It is also independent of *which* reports this attempt is still going to write. An earlier
    version skipped a message whose report already existed, on the reasoning that its signal
    should exist too. That is true of a message some earlier invocation linked -- and false of
    a message this very invocation linked in a step that has already committed. At the frozen
    bounds one case's reports, signals, and facts do not fit in one transaction, so a report
    committed in step one and its signal planned for step two is the ordinary case, not an
    exotic one; skipping it left a case member with no marker in the feed and no attempt that
    would ever write it.

    A linked message therefore always has a signal decision. The replay case is still free:
    a stored row that already displays exactly this produces no write at all, a step of no
    items, and no version bump.
    """

    decisions: list[_SignalDecision] = []
    for message_id in related_messages:
        stored = current.signals.get(message_id)
        if stored is not None and stored.case_id != case_id:
            raise MonitorApplyDeniedError(MonitorApplyDenial.REPORT_ALREADY_LINKED)
        if stored is None:
            decisions.append(
                _SignalDecision(message_id=message_id, expected_version=None, detected_at=None)
            )
            continue
        if (
            stored.label == case_title
            and stored.case_state is case_state
            and stored.related_message_count == len(related_messages)
        ):
            # The row already displays exactly this. Rewriting it would bump a version and
            # cost a transaction to change nothing, and would make an exact replay of a whole
            # answer look like an apply with a durable effect.
            continue
        decisions.append(
            _SignalDecision(
                message_id=message_id,
                expected_version=stored.version,
                detected_at=stored.detected_at,
            )
        )
    return tuple(decisions)


def _cut_steps(planned: list[PlannedCase]) -> tuple[ApplyStep, ...]:
    """Cut every planned case into bounded steps, in one deterministic order.

    Reports come first inside a case, so the first step of a new case always carries the case
    row together with the whole of its initial report linkage: ``MONITOR_APPLY_ITEM_BUDGET``
    is not smaller than the contract's report maximum, so they cannot be separated. Signals
    and then facts fill the remainder and spill into continuation steps.
    """

    steps: list[ApplyStep] = []
    for case in sorted(planned, key=lambda item: str(item.case_id)):
        if not case.has_durable_effect:
            continue
        items: list[Report | SignalWrite | Fact] = [
            *case.new_reports,
            *case.signal_writes,
            *case.new_facts,
        ]
        for index in range(case.step_count):
            window = items[
                index * MONITOR_APPLY_ITEM_BUDGET : (index + 1) * MONITOR_APPLY_ITEM_BUDGET
            ]
            expected = case.base_version + index
            steps.append(
                ApplyStep(
                    case_id=case.case_id,
                    case=_case_at_step(case, index=index),
                    case_expected_version=None if expected == 0 else expected,
                    reports=tuple(item for item in window if isinstance(item, Report)),
                    facts=tuple(item for item in window if isinstance(item, Fact)),
                    signals=tuple(item for item in window if isinstance(item, SignalWrite)),
                    first_for_case=index == 0,
                )
            )
    return tuple(steps)


def _case_at_step(case: PlannedCase, *, index: int) -> CommunityCase:
    """Return the case row exactly as it should stand once step ``index`` commits.

    Intermediate steps list a prefix of the final reports and facts, so a partially applied
    answer leaves a case that knows less than the whole story rather than one naming rows that
    are not there yet. The difference matters: a reader of the first is merely early, while a
    reader of the second hits an identifier that resolves to nothing and fails closed.
    """

    if index == case.step_count - 1:
        return case.final_case
    items: list[Report | SignalWrite | Fact] = [
        *case.new_reports,
        *case.signal_writes,
        *case.new_facts,
    ]
    covered = items[: (index + 1) * MONITOR_APPLY_ITEM_BUDGET]
    known_reports = set(case.existing_case.report_ids) if case.existing_case else set()
    known_facts = set(case.existing_case.fact_ids) if case.existing_case else set()
    for item in covered:
        if isinstance(item, Report):
            known_reports.add(item.report_id)
        elif isinstance(item, Fact):
            known_facts.add(item.fact_id)
    return replace(
        case.final_case,
        report_ids=tuple(sorted(known_reports, key=str)),
        fact_ids=tuple(sorted(known_facts, key=str)),
        version=case.base_version + index + 1,
    )


def candidate_audit_event(
    *,
    planned: PlannedCase,
    audit_event_id: UUID,
    namespace: Namespace,
    community_id: CommunityId,
    actor_id_hash: Sha256Digest,
    correlation_id: UUID,
    causation_id: UUID,
    input_hash: Sha256Digest,
    output_hash: Sha256Digest,
    now: datetime,
) -> AuditEvent:
    """Record that validated agent output changed durable state, with no content in it.

    The actor is ``SYSTEM`` rather than ``AGENT`` on purpose: the agent proposed, and the
    deterministic application decided. Attributing the decision to the model would misdescribe
    where authority actually sits.

    One event per case per invocation, written atomically with the first step that makes the
    linkage durable. Continuation steps are the same decision still being written down, not
    further decisions, so they do not append further events.
    """

    return AuditEvent(
        audit_event_id=audit_event_id,
        namespace=namespace,
        community_id=community_id,
        case_id=planned.case_id,
        actor_type=ActorType.SYSTEM,
        actor_id_hash=actor_id_hash,
        event_type="candidate.detected" if planned.created else "report.linked",
        occurred_at=now,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key_hash=None,
        entity_refs=(
            AuditEntityRef(
                entity_type="COMMUNITY_CASE",
                entity_id=planned.case_id.value,
                # The version this event's own transaction produces, not the version the
                # whole apply will end at. An audit row must describe a state that existed.
                version=planned.base_version + 1,
            ),
        ),
        decision=AuditDecision.ALLOW,
        reason_codes=(
            CANDIDATE_REASON_CODE if planned.created else CANDIDATE_EXTENDED_REASON_CODE,
        ),
        safe_details=AuditDetails(
            count=len(planned.new_reports) + len(planned.new_facts),
            rule_id="monitor-apply/v1",
        ),
        input_hash=input_hash,
        output_hash=output_hash,
    )


def planned_fact_ids(plan: MonitorApplicationPlan) -> tuple[FactId, ...]:
    """Every fact identity this plan intends to create, in a stable order."""

    return tuple(
        sorted(
            (fact.fact_id for planned in plan.cases for fact in planned.new_facts),
            key=str,
        )
    )
