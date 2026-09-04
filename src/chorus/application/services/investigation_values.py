"""Map one stored typed fact value onto its agent-contract shape.

The reverse of ``monitor_validation.to_domain_value``, and written the same way: an explicit
branch per variant rather than a reflective conversion. A fact value is the thing policy is
applied to, so a new variant must not be able to reach an agent payload by merely existing --
adding one here is a decision somebody makes and a reviewer sees.

Two variants deliberately have no mapping. ``CONTRADICTION`` and ``COMMITMENT_TERM`` are
reserved and currently unreachable in V1: no component creates either, Phase 5 creates no
facts at all, and neither has a shape in the agent contract to be projected into. Encountering
one means stored state contains something no producer can have written, which is a lineage
failure and not a payload to be assembled.
"""

from __future__ import annotations

from chorus.contracts.monitor import (
    EvidenceDescriptionValue,
    HealthDetailValue,
    IdentityAttributeValue,
    IncidentOccurrenceValue,
    LocationAreaValue,
    ManagementStatementValue,
    MonitorFactValue,
    ServiceImpactValue,
    UnitLocationValue,
)
from chorus.domain.entities import FactType
from chorus.domain.facts import (
    EvidenceDescription,
    Fact,
    HealthDetail,
    IdentityAttribute,
    IncidentOccurrence,
    LocationArea,
    ManagementStatement,
    ServiceImpact,
    UnitLocation,
)


class UnprojectableFactError(ValueError):
    """A stored fact has no agent-contract shape, so the payload is refused."""


def to_contract_value(fact: Fact) -> MonitorFactValue:
    """Return the wire value for one stored fact, or refuse the whole projection."""

    value = fact.value
    match value:
        case IncidentOccurrence():
            return IncidentOccurrenceValue(
                fact_type=FactType.INCIDENT_OCCURRENCE,
                occurred_at=value.occurred_at,
                equipment="ELEVATOR",
                failure_mode=value.failure_mode,
            )
        case ServiceImpact():
            return ServiceImpactValue(
                fact_type=FactType.SERVICE_IMPACT,
                impact_code=value.impact_code,
                summary=value.summary,
            )
        case LocationArea():
            return LocationAreaValue(fact_type=FactType.LOCATION_AREA, area=value.area)
        case IdentityAttribute():
            return IdentityAttributeValue(
                fact_type=FactType.IDENTITY_ATTRIBUTE, display_name=value.display_name
            )
        case UnitLocation():
            return UnitLocationValue(fact_type=FactType.UNIT_LOCATION, unit_label=value.unit_label)
        case HealthDetail():
            return HealthDetailValue(
                fact_type=FactType.HEALTH_DETAIL,
                subject_relation=value.subject_relation,
                detail=value.detail,
            )
        case ManagementStatement():
            return ManagementStatementValue(
                fact_type=FactType.MANAGEMENT_STATEMENT,
                statement=value.statement,
                speaker_org=value.speaker_org,
                stated_at=value.stated_at,
            )
        case EvidenceDescription():
            return EvidenceDescriptionValue(
                fact_type=FactType.EVIDENCE_DESCRIPTION,
                description=value.description,
                media_kind=value.media_kind,
            )
        case _:
            raise UnprojectableFactError("stored fact type has no agent-contract shape")
