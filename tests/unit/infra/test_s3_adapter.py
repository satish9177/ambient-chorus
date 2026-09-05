"""The S3 adapter against botocore's Stubber: exact keys, exact translation, no live AWS.

Two things are worth proving here and nowhere else. First, the keys: the adapter derives them
from typed identifiers and there is no parameter through which a caller could supply one, so
the stub asserting the exact request parameters *is* the key-grammar test. Second, the error
translation: a caller above this module must never be able to tell a botocore exception from
any other, and the direction an unmapped error is translated in decides whether a write's
outcome is treated as settled.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from uuid import UUID

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
from botocore.stub import Stubber

from chorus.domain.ids import CaseId, CommunityId, EvidenceItemId, Namespace, Sha256Digest
from chorus.infrastructure.s3.client import SINGLE_ATTEMPT_RETRIES, create_s3_client
from chorus.infrastructure.s3.objects import DIGEST_METADATA_KEY, S3ObjectStore
from chorus.ports.errors import (
    ExternalDependencyError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.objects import export_evidence_key, private_evidence_key

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin dummy credentials so client construction never consults a real provider.

    A stubbed client still builds a credential resolver, and on a developer machine that
    resolver can reach a configured login provider. These tests must describe the adapter,
    not the machine running them, so the environment is fixed here rather than assumed.
    """

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


NAMESPACE = Namespace("DEMO")
COMMUNITY = CommunityId(UUID(int=1))
CASE = CaseId(UUID(int=2))
EVIDENCE = EvidenceItemId(UUID(int=3))
PRIVATE_BUCKET = "chorus-private-evidence-test"
EXPORT_BUCKET = "chorus-export-evidence-test"
CONTENT = b"\x89PNG\r\n\x1a\n-safe-derivative"
DIGEST = Sha256Digest(f"sha256:{sha256(CONTENT).hexdigest()}")


@pytest.fixture
def store() -> tuple[S3ObjectStore, Stubber]:
    client = create_s3_client(region_name="us-east-1")
    stubber = Stubber(client)
    adapter = S3ObjectStore(
        client=client, private_bucket=PRIVATE_BUCKET, export_bucket=EXPORT_BUCKET
    )
    return adapter, stubber


def _private_key() -> str:
    return private_evidence_key(
        namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, evidence_id=EVIDENCE
    )


def _export_key() -> str:
    return export_evidence_key(
        namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, derivative_sha256=DIGEST
    )


def test_the_client_disables_sdk_retrying() -> None:
    """One adapter call is one attempt, so this system resolves its own unknown outcomes."""

    assert SINGLE_ATTEMPT_RETRIES == {"mode": "standard", "total_max_attempts": 1}


def test_the_client_protocol_exposes_no_delete_list_copy_or_presign() -> None:
    """A method that is not on the protocol is one no adapter can reach by mistake."""

    from chorus.infrastructure.s3.client import S3Client

    declared = {name for name in vars(S3Client) if not name.startswith("_")}
    assert declared == {"get_object", "head_object", "put_object"}


async def test_a_private_read_uses_the_derived_key(store: tuple[S3ObjectStore, Stubber]) -> None:
    adapter, stubber = store
    stubber.add_response(
        "get_object",
        {"Body": BytesIO(CONTENT), "ContentLength": len(CONTENT)},
        {"Bucket": PRIVATE_BUCKET, "Key": _private_key()},
    )

    with stubber:
        content = await adapter.load_private_evidence(
            namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, evidence_id=EVIDENCE
        )

    assert content == CONTENT
    stubber.assert_no_pending_responses()


async def test_an_oversized_private_object_is_refused_from_its_header(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """Refused before the body is read, so an oversize object never reaches memory."""

    adapter, stubber = store
    stubber.add_response(
        "get_object",
        {"Body": BytesIO(b""), "ContentLength": 10_000_001},
        {"Bucket": PRIVATE_BUCKET, "Key": _private_key()},
    )

    with stubber, pytest.raises(ExternalDependencyError) as error:
        await adapter.load_private_evidence(
            namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, evidence_id=EVIDENCE
        )

    assert error.value.retryable is False


async def test_an_absent_private_object_is_not_found(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    adapter, stubber = store
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

    with stubber, pytest.raises(NotFoundError):
        await adapter.load_private_evidence(
            namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, evidence_id=EVIDENCE
        )


async def test_an_export_head_returns_the_stored_digest(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    adapter, stubber = store
    stubber.add_response(
        "head_object",
        {
            "ContentLength": len(CONTENT),
            "ContentType": "image/png",
            "Metadata": {DIGEST_METADATA_KEY: DIGEST.value},
        },
        {"Bucket": EXPORT_BUCKET, "Key": _export_key()},
    )

    with stubber:
        descriptor = await adapter.head_export_evidence(
            namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, derivative_sha256=DIGEST
        )

    assert descriptor is not None
    assert descriptor.sha256 == DIGEST
    assert descriptor.media_type == "image/png"
    assert descriptor.byte_length == len(CONTENT)


async def test_an_absent_export_object_heads_as_none(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """Absent is an answer, not a failure: it is what licenses the identical repeat."""

    adapter, stubber = store
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)

    with stubber:
        descriptor = await adapter.head_export_evidence(
            namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, derivative_sha256=DIGEST
        )

    assert descriptor is None


async def test_an_export_write_is_a_conditional_create_with_encryption_and_its_digest(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """The stub asserts the exact request, so ``IfNoneMatch`` is pinned as a wire fact.

    Without it the write is a put and two writers can both land bytes on one content
    address; with it the service decides which one creates the object.
    """

    adapter, stubber = store
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": EXPORT_BUCKET,
            "Key": _export_key(),
            "Body": CONTENT,
            "ContentType": "image/png",
            "ServerSideEncryption": "aws:kms",
            "ChecksumAlgorithm": "SHA256",
            "Metadata": {DIGEST_METADATA_KEY: DIGEST.value},
            "IfNoneMatch": "*",
        },
    )

    with stubber:
        await adapter.put_export_evidence(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            case_id=CASE,
            derivative_sha256=DIGEST,
            content=CONTENT,
            media_type="image/png",
        )

    stubber.assert_no_pending_responses()


async def test_a_write_whose_content_disagrees_with_its_address_is_refused_locally(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """The key is the digest. Writing other bytes there would corrupt the address itself."""

    adapter, _ = store

    with pytest.raises(ExternalDependencyError) as error:
        await adapter.put_export_evidence(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            case_id=CASE,
            derivative_sha256=DIGEST,
            content=b"different bytes",
            media_type="image/png",
        )

    assert error.value.retryable is False


async def test_a_denied_write_is_a_definite_failure(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    adapter, stubber = store
    stubber.add_client_error("put_object", service_error_code="AccessDenied", http_status_code=403)

    with stubber, pytest.raises(ExternalDependencyError) as error:
        await adapter.put_export_evidence(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            case_id=CASE,
            derivative_sha256=DIGEST,
            content=CONTENT,
            media_type="image/png",
        )

    assert error.value.retryable is False


async def test_an_unmapped_service_error_is_treated_as_unresolved(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """The safe direction. "Definite" would license a caller to conclude nothing happened."""

    adapter, stubber = store
    stubber.add_client_error("put_object", service_error_code="SlowDown", http_status_code=503)

    with stubber, pytest.raises(ExternalDependencyError) as error:
        await adapter.put_export_evidence(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            case_id=CASE,
            derivative_sha256=DIGEST,
            content=CONTENT,
            media_type="image/png",
        )

    assert error.value.retryable is True


async def test_a_transport_failure_becomes_a_dependency_error(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """A timeout is the ambiguous case: the bytes may have landed, and the head decides."""

    adapter, _ = store

    class TimingOut:
        def put_object(self, **kwargs: object) -> object:
            raise ConnectTimeoutError(endpoint_url="https://s3.example")

        def get_object(self, **kwargs: object) -> object:  # pragma: no cover - unused
            raise AssertionError

        def head_object(self, **kwargs: object) -> object:  # pragma: no cover - unused
            raise AssertionError

    adapter.client = TimingOut()  # type: ignore[assignment]

    with pytest.raises(ExternalDependencyError) as error:
        await adapter.put_export_evidence(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            case_id=CASE,
            derivative_sha256=DIGEST,
            content=CONTENT,
            media_type="image/png",
        )

    assert error.value.retryable is True


async def test_no_sdk_message_survives_translation(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """A bucket name in an error string is a bucket name in a log line."""

    adapter, stubber = store
    stubber.add_client_error(
        "head_object",
        service_error_code="InternalError",
        service_message=f"failure reading {EXPORT_BUCKET}/{_export_key()}",
        http_status_code=500,
    )

    with stubber, pytest.raises(ExternalDependencyError) as error:
        await adapter.head_export_evidence(
            namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE, derivative_sha256=DIGEST
        )

    assert EXPORT_BUCKET not in str(error.value)
    assert _export_key() not in str(error.value)


@pytest.mark.parametrize(
    "error_class",
    [ClientError],
    ids=["client-error"],
)
def test_the_taxonomy_is_closed(error_class: type[Exception]) -> None:
    """Every failure this adapter raises is a persistence error, never an SDK one."""

    from chorus.ports.errors import PersistenceError

    assert issubclass(NotFoundError, PersistenceError)
    assert issubclass(ExternalDependencyError, PersistenceError)
    assert not issubclass(error_class, PersistenceError)


async def test_an_existing_object_makes_the_create_a_conflict_not_an_overwrite(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """S3 answers ``PreconditionFailed``; the adapter calls that a conflict, not a failure.

    The distinction matters to the caller: a conflict means "something is already at this
    address, go and look at it", while a dependency error would mean "the outcome is unknown".
    Confusing the two would turn a resolvable race into a retry.
    """

    adapter, stubber = store
    stubber.add_client_error(
        "put_object", service_error_code="PreconditionFailed", http_status_code=412
    )

    with stubber, pytest.raises(PersistenceConflictError):
        await adapter.put_export_evidence(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            case_id=CASE,
            derivative_sha256=DIGEST,
            content=CONTENT,
            media_type="image/png",
        )


async def test_a_precondition_conflict_is_not_reported_as_a_dependency_error(
    store: tuple[S3ObjectStore, Stubber],
) -> None:
    """A conflict is resolvable by reading; a dependency error is not. They must not blur."""

    adapter, stubber = store
    stubber.add_client_error(
        "put_object", service_error_code="PreconditionFailed", http_status_code=412
    )

    with stubber:
        try:
            await adapter.put_export_evidence(
                namespace=NAMESPACE,
                community_id=COMMUNITY,
                case_id=CASE,
                derivative_sha256=DIGEST,
                content=CONTENT,
                media_type="image/png",
            )
        except PersistenceConflictError as error:
            assert not isinstance(error, ExternalDependencyError)
        else:  # pragma: no cover - the stub always raises
            raise AssertionError("the conditional create should have conflicted")


def test_the_pinned_sdk_exposes_the_conditional_create_parameter() -> None:
    """The mechanism has to exist in the *pinned* SDK, not merely in the current API docs.

    ``If-None-Match`` on ``PutObject`` is what makes the write a create. If a future pin dropped
    it, the adapter would silently become a last-writer-wins put, so the capability is asserted
    against the shipped service model rather than assumed.
    """

    import boto3

    model = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="local", aws_secret_access_key="local"
    ).meta.service_model
    members = model.operation_model("PutObject").input_shape.members

    assert "IfNoneMatch" in members
