"""P1-1: both AWS composition roots must bind the evidence bucket KMS key ARNs.

I7 made ``S3ObjectStore`` require ``private_kms_key_id`` and ``export_kms_key_id`` -- each is
sent as ``SSEKMSKeyId`` on every ``put_object`` and the deployed bucket policy denies a write
that omits or misnames the key. The compiler and inbound-mail composition roots construct the
adapter, so each must read the two ARNs from configuration and pass them through, and must fail
closed -- at construction, the way the deployed sender refuses a missing compiler ARN -- when
either is absent.

These tests exercise the real composition boundary: they build ``CompilerSettings`` /
``InboundMailSettings`` and call ``build_compile_view`` / ``build_inbound_mail``, then read the
constructed object graph. Nothing calls AWS.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
from functions.compiler.composition import CompilerSettings, build_compile_view
from functions.inbound_mail.composition import InboundMailSettings, build_inbound_mail

from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId, EvidenceItemId, Namespace, Sha256Digest
from chorus.infrastructure.s3.objects import S3ObjectStore
from chorus.ports.evidence_review import EvidenceReviewInput
from chorus.ports.records import SafeInboundMailConfiguration, StoredSafeDestination

PRIVATE_KEY_ARN = "arn:aws:kms:us-east-1:111111111111:key/11111111-1111-1111-1111-111111111111"
EXPORT_KEY_ARN = "arn:aws:kms:us-east-1:111111111111:key/22222222-2222-2222-2222-222222222222"
_DIGEST = Sha256Digest("sha256:" + "0" * 64)


@pytest.fixture(autouse=True)
def _offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a composition constructs boto3 clients, which resolve credentials.

    Placeholder values so the object graph is exercised without reaching a credential provider.
    No test here calls AWS.
    """

    for name, value in (
        ("AWS_ACCESS_KEY_ID", "local"),
        ("AWS_SECRET_ACCESS_KEY", "local"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_EC2_METADATA_DISABLED", "true"),
        ("AWS_CONFIG_FILE", os.devnull),
        ("AWS_SHARED_CREDENTIALS_FILE", os.devnull),
    ):
        monkeypatch.setenv(name, value)


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


class _NoReviews:
    """A review registry that curates nothing. Construction is what is under test."""

    def review_for(self, evidence_id: EvidenceItemId) -> EvidenceReviewInput | None:
        return None


class _NoRawMessages:
    """An inbound raw-message reader that would raise if used."""

    async def read(self, *, bucket: str, key: str) -> bytes:
        raise AssertionError("no object is read during composition")


def _destination() -> StoredSafeDestination:
    return StoredSafeDestination(
        destination_id=DestinationId("property_manager:demo"),
        kind=DestinationKind.PROPERTY_MANAGER,
        registry_version=1,
        routing_token=UUID("00000000-0000-0000-0000-000000000000"),
        display_label="Property Management",
    )


def _compiler_settings(
    *, private_key_arn: str | None = PRIVATE_KEY_ARN, export_key_arn: str | None = EXPORT_KEY_ARN
) -> CompilerSettings:
    return CompilerSettings(
        region="us-east-1",
        namespace="DEMO",
        core_table="chorus-core-demo",
        shareable_table="chorus-shareable-demo",
        audit_table="chorus-audit-demo",
        private_evidence_bucket="chorus-private-evidence-demo",
        export_evidence_bucket="chorus-export-evidence-demo",
        community_public_label="A Community",
        destination=_destination(),
        cursor_secret=b"0" * 32,
        private_evidence_key_arn=private_key_arn,
        export_evidence_key_arn=export_key_arn,
    )


def _inbound_settings(
    *, private_key_arn: str | None = PRIVATE_KEY_ARN, export_key_arn: str | None = EXPORT_KEY_ARN
) -> InboundMailSettings:
    return InboundMailSettings(
        region="us-east-1",
        namespace=Namespace("DEMO"),
        core_table="chorus-core-demo",
        shareable_table="chorus-shareable-demo",
        audit_table="chorus-audit-demo",
        private_evidence_bucket="chorus-private-evidence-demo",
        export_evidence_bucket="chorus-export-evidence-demo",
        inbound_transport="aws:ses-receipt",
        inbound_source_arn="arn:aws:ses:us-east-1:111111111111:receipt-rule-set/rs:receipt-rule/r",
        inbound_config=SafeInboundMailConfiguration(
            destination=_destination(),
            destination_address_digest=_DIGEST,
            inbound_address_digest=_DIGEST,
        ),
        from_identity_id="chorus-demo-sender",
        cursor_secret=b"0" * 32,
        private_evidence_key_arn=private_key_arn,
        export_evidence_key_arn=export_key_arn,
    )


# -- the compiler composition -----------------------------------------------------------


def test_the_compiler_composition_binds_both_evidence_key_arns() -> None:
    """A + B: the configured ARNs reach ``S3ObjectStore`` as the two ``SSEKMSKeyId`` values."""

    compile_view = build_compile_view(
        _compiler_settings(), clock=FrozenClock(), reviews=_NoReviews()
    )
    store = compile_view.evidence.objects

    assert isinstance(store, S3ObjectStore)
    assert store.private_kms_key_id == PRIVATE_KEY_ARN
    assert store.export_kms_key_id == EXPORT_KEY_ARN


@pytest.mark.parametrize("missing", ["private", "export"])
def test_the_compiler_composition_refuses_a_missing_evidence_key_arn(missing: str) -> None:
    """C + D: an absent private or export key ARN fails closed at construction."""

    kwargs: dict[str, str | None] = {
        "private_key_arn": PRIVATE_KEY_ARN,
        "export_key_arn": EXPORT_KEY_ARN,
    }
    kwargs[f"{missing}_key_arn"] = None

    with pytest.raises(ValueError, match="KMS key ARN"):
        build_compile_view(_compiler_settings(**kwargs), clock=FrozenClock(), reviews=_NoReviews())


def test_the_compiler_composition_refuses_an_empty_evidence_key_arn() -> None:
    """An empty string is as absent as ``None`` -- the check is for a usable value."""

    with pytest.raises(ValueError, match="KMS key ARN"):
        build_compile_view(
            _compiler_settings(private_key_arn=""), clock=FrozenClock(), reviews=_NoReviews()
        )


# -- the inbound-mail composition -----------------------------------------------------


def test_the_inbound_mail_composition_binds_both_evidence_key_arns() -> None:
    """A + B: both ARNs are passed even though this entry point writes private evidence only."""

    composition = build_inbound_mail(
        _inbound_settings(), clock=FrozenClock(), raw_messages=_NoRawMessages()
    )
    store = composition.ingest.objects

    assert isinstance(store, S3ObjectStore)
    assert store.private_kms_key_id == PRIVATE_KEY_ARN
    assert store.export_kms_key_id == EXPORT_KEY_ARN


@pytest.mark.parametrize("missing", ["private", "export"])
def test_the_inbound_mail_composition_refuses_a_missing_evidence_key_arn(missing: str) -> None:
    """C + D: an absent private or export key ARN fails closed at construction."""

    kwargs: dict[str, str | None] = {
        "private_key_arn": PRIVATE_KEY_ARN,
        "export_key_arn": EXPORT_KEY_ARN,
    }
    kwargs[f"{missing}_key_arn"] = None

    with pytest.raises(ValueError, match="KMS key ARN"):
        build_inbound_mail(
            _inbound_settings(**kwargs), clock=FrozenClock(), raw_messages=_NoRawMessages()
        )
