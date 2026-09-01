"""The mirrored stored view shapes must never drift from the frozen compiler DTOs.

``chorus.ports`` may not import ``chorus.privacy``, so the stored shapes are restated. That is
only safe while the field sets stay identical, which is what these tests enforce: adding a
field to a compiler DTO without adding it to the stored record fails here.
"""

from __future__ import annotations

from dataclasses import fields

from chorus.ports import records
from chorus.privacy import compiler, policy, transformations


def field_names(cls: type) -> set[str]:
    return {item.name for item in fields(cls)}


def test_stored_view_mirrors_the_compiled_view() -> None:
    assert field_names(records.StoredShareableView) == field_names(compiler.ShareableCaseView)


def test_stored_safe_fact_mirrors_the_compiled_fact() -> None:
    assert field_names(records.StoredShareableFact) == field_names(transformations.ShareableFact)


def test_stored_safe_evidence_ref_mirrors_the_compiled_ref() -> None:
    assert field_names(records.StoredSafeEvidenceRef) == field_names(
        transformations.ShareableEvidenceRef
    )


def test_stored_destination_mirrors_the_registry_entry() -> None:
    assert field_names(records.StoredSafeDestination) == field_names(policy.SafeDestination)


def test_stored_mandate_version_ref_mirrors_the_compiler_ref() -> None:
    assert field_names(records.StoredMandateVersionRef) == field_names(compiler.MandateVersionRef)


def test_transformation_kinds_mirror_the_policy_enum() -> None:
    assert {member.value for member in records.TransformationKind} == {
        member.value for member in policy.TransformationKind
    }
