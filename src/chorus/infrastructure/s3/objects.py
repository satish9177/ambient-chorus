"""The S3 object-store adapter: two buckets, three calls, and no caller-chosen key.

It implements the raw operations only. The create-or-verify rule and the unknown-outcome
resolution live above the port, in ``chorus.application.services.safe_evidence``, so the
in-memory store and this adapter cannot disagree about them -- a difference in *semantics*
between two adapters is the failure the contract suite exists to prevent, and the way to
prevent it is to have only one implementation of the semantics.

Every SDK exception is translated here, once, into the closed persistence taxonomy. Nothing
below this module ever sees a ``ClientError``, and no error message from botocore reaches a log
line or an API response: the codes are ours and they carry an entity reference, never a key, a
bucket name, or a response body.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from botocore.exceptions import BotoCoreError, ClientError

from chorus.domain.ids import CaseId, CommunityId, EvidenceItemId, Namespace, Sha256Digest
from chorus.infrastructure.s3.client import S3Client
from chorus.ports.errors import (
    ExternalDependencyError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.objects import (
    MAX_EVIDENCE_SOURCE_BYTES,
    ExportObjectDescriptor,
    export_evidence_key,
    private_evidence_key,
)

_ABSENT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_ALREADY_EXISTS_CODES = frozenset({"PreconditionFailed", "412"})
"""What S3 answers when ``If-None-Match: *`` finds an object already at the key.

It is a *conflict*, not a failure: the write did not happen because something is already
there, which is exactly the outcome a create-only condition exists to produce. The caller
reads the key and decides whether what it found is the same object.
"""
_DEFINITE_CODES = frozenset(
    {"AccessDenied", "InvalidRequest", "EntityTooLarge", "KMS.AccessDeniedException"}
)

DIGEST_METADATA_KEY = "sha256"
"""The one allowlisted object metadata entry, and it is a digest of the object's own bytes.

The frozen metadata allowlist admits digests, media types, and identifiers. This adapter writes
only the digest, because for an export derivative the key already *is* the digest and the
metadata exists so a head can confirm the two agree without fetching the body.
"""


def _error_code(error: ClientError) -> str:
    response = getattr(error, "response", {})
    return str(response.get("Error", {}).get("Code", ""))


def _translate(error: Exception, entity_ref: str) -> Exception:
    """Map one SDK failure onto the closed taxonomy, keeping nothing it said.

    An unmapped ``ClientError`` becomes a *retryable* dependency error rather than a definite
    one, and that direction is deliberate: for a write, "definite failure" licenses a caller to
    conclude nothing happened. The safe default when the SDK's meaning is unknown is to treat
    the outcome as unresolved and let the content-address head settle it.
    """

    if isinstance(error, ClientError):
        code = _error_code(error)
        if code in _ABSENT_CODES:
            return NotFoundError(entity_ref)
        if code in _DEFINITE_CODES:
            return ExternalDependencyError(entity_ref, retryable=False)
        return ExternalDependencyError(entity_ref)
    if isinstance(error, BotoCoreError):
        # A connection or timeout failure. The request may or may not have reached the service.
        return ExternalDependencyError(entity_ref)
    return ExternalDependencyError(entity_ref)  # pragma: no cover - defensive


@dataclass(slots=True)
class S3ObjectStore:
    """Reads private evidence, heads and writes export derivatives. Nothing else."""

    client: S3Client
    private_bucket: str
    export_bucket: str

    async def load_private_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        evidence_id: EvidenceItemId,
    ) -> bytes:
        """Read one source object, refusing anything past the frozen source bound.

        The length is checked from the response header *before* the body is read, so an object
        larger than V1 accepts is refused rather than streamed into memory first.
        """

        key = private_evidence_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            evidence_id=evidence_id,
        )
        try:
            response = self.client.get_object(Bucket=self.private_bucket, Key=key)
        except (ClientError, BotoCoreError) as error:
            raise _translate(error, "PRIVATE_EVIDENCE_OBJECT") from error
        declared = response.get("ContentLength")
        if declared is not None and declared > MAX_EVIDENCE_SOURCE_BYTES:
            raise ExternalDependencyError("PRIVATE_EVIDENCE_OBJECT", retryable=False)
        body = response.get("Body")
        if body is None:  # pragma: no cover - the service always returns one
            raise ExternalDependencyError("PRIVATE_EVIDENCE_OBJECT", retryable=False)
        try:
            content = body.read(MAX_EVIDENCE_SOURCE_BYTES + 1)
        except (ClientError, BotoCoreError) as error:
            raise _translate(error, "PRIVATE_EVIDENCE_OBJECT") from error
        if len(content) > MAX_EVIDENCE_SOURCE_BYTES:
            raise ExternalDependencyError("PRIVATE_EVIDENCE_OBJECT", retryable=False)
        return bytes(content)

    async def head_export_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        derivative_sha256: Sha256Digest,
    ) -> ExportObjectDescriptor | None:
        """Describe the object at this content address without fetching its body.

        The digest is read back from the object's own metadata rather than assumed from the key.
        The key and the metadata are written together, so a disagreement between them is
        corruption -- and reporting the key's digest would hide exactly that.
        """

        key = export_evidence_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            derivative_sha256=derivative_sha256,
        )
        try:
            response = self.client.head_object(Bucket=self.export_bucket, Key=key)
        except ClientError as error:
            if _error_code(error) in _ABSENT_CODES:
                return None
            raise _translate(error, "EXPORT_EVIDENCE_OBJECT") from error
        except BotoCoreError as error:
            raise _translate(error, "EXPORT_EVIDENCE_OBJECT") from error
        stored = response.get("Metadata", {}).get(DIGEST_METADATA_KEY)
        return ExportObjectDescriptor(
            media_type=response.get("ContentType", ""),
            byte_length=int(response.get("ContentLength", 0)),
            sha256=Sha256Digest(stored) if stored else Sha256Digest("sha256:" + "0" * 64),
        )

    async def put_export_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        derivative_sha256: Sha256Digest,
        content: bytes,
        media_type: str,
    ) -> None:
        """Create one derivative at its content address, or report that one is already there.

        The write is conditional on the key being absent, so it is a *create* rather than a
        put: an object already at that address raises ``PersistenceConflictError`` and this
        adapter never overwrites one. Content addressing means the existing object should be
        byte-identical, but "should be" is the caller's to verify, not this layer's to assume.

        ``ChecksumSHA256`` is supplied so the service verifies the bytes it received rather than
        taking this process's word for it, and the digest is repeated in the allowlisted
        metadata so a later head can confirm the object matches the address it sits at without
        downloading it.
        """

        if f"sha256:{sha256(content).hexdigest()}" != derivative_sha256.value:
            raise ExternalDependencyError("EXPORT_EVIDENCE_OBJECT", retryable=False)
        key = export_evidence_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            derivative_sha256=derivative_sha256,
        )
        try:
            self.client.put_object(
                Bucket=self.export_bucket,
                Key=key,
                Body=content,
                ContentType=media_type,
                ServerSideEncryption="aws:kms",
                ChecksumAlgorithm="SHA256",
                Metadata={DIGEST_METADATA_KEY: derivative_sha256.value},
                # Create-if-absent. A head-then-put pair is not a create: two compilers
                # sanitizing the same photograph can both observe an empty key and both
                # write. The precondition closes that window in the service rather than in
                # this process, so exactly one writer creates the object and the other is
                # told so.
                IfNoneMatch="*",
            )
        except ClientError as error:
            if _error_code(error) in _ALREADY_EXISTS_CODES:
                raise PersistenceConflictError("EXPORT_EVIDENCE_OBJECT") from error
            raise _translate(error, "EXPORT_EVIDENCE_OBJECT") from error
        except BotoCoreError as error:
            raise _translate(error, "EXPORT_EVIDENCE_OBJECT") from error
