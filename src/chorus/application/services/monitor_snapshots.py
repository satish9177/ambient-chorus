"""Immutable private snapshots of the two frozen stages of a Monitor invocation.

A Monitor operation has three durable stages, not one: the **frozen input** the model was
handed, the **validated apply plan** its answer produced, and the **apply progress** of that
plan. The middle stage is the one this module exists for, because it is what makes the
central rule enforceable rather than aspirational:

    a partially applied Monitor operation must never need another model invocation.

Once the plan snapshot lands, the invocation is permanently complete. A redelivered worker
loads it, re-proves it deterministically against the frozen input, and finishes the steps it
still owes. It never rebuilds context, never re-projects private text, and never calls a
model. And because the input is frozen too, the same ``invocation_id`` always means the same
``MonitorInput``: a completed or half-applied invocation can never be quietly reinterpreted
against a community that has moved on since.

What a snapshot is, and is not
------------------------------
It is private Core state under the operation's own partition. It is **not** a log, **not**
shareable, and **not** reachable through the API. It may hold structured material derived
from private community text -- that is the point of it, and Core is the authorized private
zone -- but it holds only CHORUS contract data: the strict input envelope, the strict
validated output envelope, and identifiers. No provider response body, no completion text,
no chain of thought, and no prompt.

Why chunks
----------
One DynamoDB item cannot exceed 400 KiB and the frozen application payload bound is 1 MiB, so
a snapshot at the contract maxima cannot be one item. It is therefore an immutable manifest
plus deterministically ordered chunks of RFC 8785 canonical UTF-8 bytes, stored as string
attributes -- DynamoDB stores strings as UTF-8, so there is nothing to base64-expand and
nothing to gain by doing so. Chunks are cut only on character boundaries, and the manifest
carries the digest of the whole byte string, so reassembly is *checked* rather than assumed:
a missing chunk, a wrong count, or a digest mismatch is an integrity failure that quotes
nothing.

Writing is replay-safe. Manifest and chunks commit in one bounded create-only transaction; a
conditional failure means the snapshot is already there, and the writer then proves it is the
*same* snapshot by comparing digests. A stored digest that differs is a conflict, never a
second opinion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from chorus.application.services.monitor_apply import ApplyStepDescriptor, plan_hash_of
from chorus.contracts.common import (
    MONITOR_PROMPT_VERSION,
    AgentInputEnvelope,
    AgentResultEnvelope,
)
from chorus.contracts.monitor import MonitorInput, MonitorOutput
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import (
    CommunityId,
    ContributorId,
    OperationId,
    SensitiveStr,
    Sha256Digest,
)
from chorus.domain.time import epoch_seconds_ceiling, format_utc, parse_utc
from chorus.ports.agents import MonitorInvocation, MonitorResult
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.records import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_CHUNK_BYTES,
    MAX_SNAPSHOT_CHUNKS,
    MonitorSnapshotChunk,
    MonitorSnapshotKind,
    MonitorSnapshotManifest,
)
from chorus.ports.repositories import CoreRepositoryPort
from chorus.ports.scopes import OperationScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import canonical_bytes, hash_value

INPUT_SNAPSHOT_SCHEMA: Final = "monitor-frozen-input/v1"
PLAN_SNAPSHOT_SCHEMA: Final = "monitor-validated-plan/v1"
PLAN_PROVENANCE_SCHEMA: Final = "monitor-plan-provenance/v1"

SNAPSHOT_TRANSACTION: Final = "persist-monitor-snapshot"


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenMonitorInput:
    """The exact bounded payload one invocation was given, and the hash that names it.

    ``contributor_by_pseudonym`` travels with the payload because it is part of the frozen
    input rather than a lookup that can be rebuilt: it is the mapping the ownership check
    resolves a report's claimed author through, and rebuilding it later against a community
    whose contributors have changed would let a validated answer be re-proved against a
    different set of people than the one it was reasoned about.

    ``command_message_ids`` records *which command* this input was frozen for. It is not part
    of ``input_hash`` -- that names the payload the model saw and nothing else -- but a
    redelivery naming a different set of new messages under the same invocation identity is
    still reusing one identity for two pieces of work, and the snapshot is what makes that
    detectable rather than silently absorbed.
    """

    invocation: MonitorInvocation
    contributor_by_pseudonym: dict[str, ContributorId]
    command_message_ids: tuple[UUID, ...]
    input_hash: Sha256Digest


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorPlanProvenance:
    """Everything that says *which* answer a validated plan is, in one hashable object.

    A plan snapshot is only safe to apply if it is provably the plan this operation's own
    invocation produced from this operation's own frozen input under the Monitor prompt this
    process is running. Those facts were previously spread across a manifest and a document
    that merely agreed with each other, which is not a check: both come back from the same
    partition, and anything able to rewrite one is able to rewrite the other.

    Gathering them into one object with one digest makes the agreement checkable in a single
    place, and makes adding a field to the identity impossible to do by halves.
    """

    operation_id: OperationId
    invocation_id: UUID
    input_hash: Sha256Digest
    output_hash: Sha256Digest
    plan_hash: Sha256Digest
    prompt_version: str
    model_profile_hash: Sha256Digest

    @property
    def digest(self) -> Sha256Digest:
        return hash_value(
            {
                "schema": PLAN_PROVENANCE_SCHEMA,
                "operation_id": str(self.operation_id),
                "invocation_id": str(self.invocation_id),
                "input_hash": self.input_hash.value,
                "output_hash": self.output_hash.value,
                "plan_hash": self.plan_hash.value,
                "prompt_version": self.prompt_version,
                "model_profile_hash": self.model_profile_hash.value,
            }
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenMonitorPlan:
    """One validated answer plus the ordered plan it produced, frozen before step one.

    ``planned_at`` is carried rather than re-read from the clock. Every entity timestamp and
    the audit row's own sort key derive from it, so a resumed attempt that used a fresh
    instant would re-stage the same decision at a *different* address and append a second
    audit record for it.
    """

    result: MonitorResult
    steps: tuple[ApplyStepDescriptor, ...]
    planned_at: datetime
    input_hash: Sha256Digest
    output_hash: Sha256Digest
    plan_hash: Sha256Digest


def chunk_canonical_bytes(raw: bytes) -> tuple[str, ...]:
    """Cut canonical UTF-8 bytes into ordered chunks that are each valid UTF-8.

    Cutting on a fixed byte budget alone would split a multi-byte character across two items,
    so each cut backs up off any continuation byte first. The budget is a fixed safe maximum
    well under the item limit rather than a tuned one: no legitimate snapshot may ever produce
    an item storage would refuse.
    """

    if not raw:
        raise ValueError("a snapshot has content")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("a snapshot exceeds the frozen payload bound")
    chunks: list[str] = []
    start = 0
    while start < len(raw):
        end = min(start + MAX_SNAPSHOT_CHUNK_BYTES, len(raw))
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        if end == start:  # pragma: no cover - unreachable for well-formed UTF-8
            raise ValueError("a snapshot chunk could not be cut on a character boundary")
        chunks.append(raw[start:end].decode("utf-8"))
        start = end
    if len(chunks) > MAX_SNAPSHOT_CHUNKS:
        raise ValueError("a snapshot exceeds the frozen chunk bound")
    return tuple(chunks)


@dataclass(slots=True)
class MonitorSnapshots:
    """Read and write the two immutable snapshots one Monitor invocation owns."""

    core: CoreRepositoryPort
    unit_of_work: UnitOfWork

    # -- frozen input ------------------------------------------------------------------

    async def load_input(
        self, scope: OperationScope, invocation_id: UUID
    ) -> FrozenMonitorInput | None:
        """Return the frozen input for this invocation, or ``None`` if none was ever built."""

        document = await self._load(scope, MonitorSnapshotKind.MONITOR_INPUT, invocation_id)
        if document is None:
            return None
        manifest, payload = document
        _require(payload.get("schema") == INPUT_SNAPSHOT_SCHEMA, "MONITOR_INPUT:schema")
        _require_scope(payload, scope, invocation_id, manifest.community_id)
        invocation = _parse(AgentInputEnvelope[MonitorInput], payload.get("envelope"))
        _require(invocation.invocation_id == invocation_id, "MONITOR_INPUT:invocation")
        # Recomputed rather than taken on trust. The manifest is a separate item, so its
        # ``input_hash`` is not covered by the digest of the reassembled document; deriving it
        # again is what proves the two describe the same frozen input.
        _require(
            _input_hash(_frozen_document(payload)) == manifest.input_hash,
            "MONITOR_INPUT:input_hash",
        )
        return FrozenMonitorInput(
            invocation=invocation,
            contributor_by_pseudonym=_read_pseudonyms(payload),
            command_message_ids=_read_message_ids(payload),
            input_hash=manifest.input_hash,
        )

    async def freeze_input(
        self,
        scope: OperationScope,
        *,
        community_id: CommunityId,
        invocation: MonitorInvocation,
        contributor_by_pseudonym: dict[str, ContributorId],
        command_message_ids: tuple[UUID, ...],
        prompt_version: str,
        now: datetime,
        expires_at_epoch: int,
    ) -> FrozenMonitorInput:
        """Persist the frozen input once, or recover the identical one already there."""

        frozen: dict[str, object] = {
            "schema": INPUT_SNAPSHOT_SCHEMA,
            "namespace": scope.namespace.value,
            "community_id": str(community_id),
            "operation_id": str(scope.operation_id),
            "invocation_id": str(invocation.invocation_id),
            "envelope": invocation.model_dump_json(),
            "contributor_by_pseudonym": {
                pseudonym: str(contributor_id)
                for pseudonym, contributor_id in sorted(contributor_by_pseudonym.items())
            },
        }
        document = {
            **frozen,
            "command_message_ids": sorted(str(value) for value in command_message_ids),
        }
        raw = canonical_bytes(document)
        content_sha256 = hash_value(document)
        input_hash = _input_hash(frozen)
        await self._write(
            scope,
            manifest=MonitorSnapshotManifest(
                invocation_id=invocation.invocation_id,
                operation_id=scope.operation_id,
                namespace=scope.namespace,
                community_id=community_id,
                kind=MonitorSnapshotKind.MONITOR_INPUT,
                content_sha256=content_sha256,
                byte_length=len(raw),
                chunk_count=len(chunk_canonical_bytes(raw)),
                input_hash=input_hash,
                prompt_version=prompt_version,
                created_at=now,
                expires_at_epoch=expires_at_epoch,
            ),
            raw=raw,
            expires_at_epoch=expires_at_epoch,
        )
        return FrozenMonitorInput(
            invocation=invocation,
            contributor_by_pseudonym=dict(contributor_by_pseudonym),
            command_message_ids=tuple(sorted(command_message_ids, key=str)),
            input_hash=input_hash,
        )

    # -- validated plan ----------------------------------------------------------------

    async def has_plan(self, scope: OperationScope, invocation_id: UUID) -> bool:
        """Whether a validated plan exists at all, without reassembling or proving it.

        Used for exactly one decision: a plan whose *frozen input* is missing can never be
        re-proved, so the caller refuses it before anything is loaded. Answering that from the
        manifest alone keeps the refusal cheap and keeps :meth:`load_plan` able to demand the
        input it needs rather than treating it as optional.
        """

        manifest = await self.core.load_monitor_snapshot_manifest(
            scope, kind=MonitorSnapshotKind.MONITOR_PLAN, invocation_id=invocation_id
        )
        return manifest is not None

    async def load_plan(
        self, scope: OperationScope, invocation_id: UUID, *, frozen_input: FrozenMonitorInput
    ) -> FrozenMonitorPlan | None:
        """Return the validated plan for this invocation, having proved it is that plan.

        Everything below is a *recomputation*, not a comparison of two stored copies. The
        earlier version checked the chunks against the manifest digest and the steps against
        the manifest plan hash, and took the rest of the identity -- which operation, which
        invocation, which input, which answer, which prompt, which model -- from metadata it
        never re-derived. So a manifest field and its twin inside the document agreed because
        nothing had disagreed with them yet, and an edit that moved both was invisible.

        The order is deliberate, cheapest disqualifier first, and every failure is a closed
        ``IntegrityError`` that quotes nothing:

        1. the chunks reassemble to the manifest's byte length and digest (in :meth:`_load`);
        2. the document is this scope's -- namespace, community, operation, invocation;
        3. the manifest and the document agree on the whole provenance, and that provenance
           hashes to the digest the manifest carries;
        4. the output hash is recomputed from the stored answer, so metadata cannot describe
           content it does not match;
        5. the plan hash is recomputed from the stored steps, for the same reason;
        6. the answer's own envelope names this invocation, this prompt version, and this
           model profile;
        7. the prompt version is the one *this process* runs, so a plan frozen under different
           Monitor instructions is never applied by a build that would have asked differently;
        8. the plan's input hash is the hash of the frozen input snapshot that is actually
           there -- which is what makes swapping a plan between two invocations detectable.

        Any mismatch fails closed: no apply, no model retry, and no silent repair.
        """

        document = await self._load(scope, MonitorSnapshotKind.MONITOR_PLAN, invocation_id)
        if document is None:
            return None
        manifest, payload = document
        _require(payload.get("schema") == PLAN_SNAPSHOT_SCHEMA, "MONITOR_PLAN:schema")
        _require_scope(payload, scope, invocation_id, manifest.community_id)
        stated = _manifest_provenance(manifest)
        _require(stated == _document_provenance(payload), "MONITOR_PLAN:provenance")
        _require(stated.digest == manifest.provenance_hash, "MONITOR_PLAN:provenance_hash")

        result = _parse(AgentResultEnvelope[MonitorOutput], payload.get("result"))
        try:
            steps = tuple(
                ApplyStepDescriptor.from_json(item) for item in _as_list(payload.get("steps"))
            )
            planned_at = parse_utc(str(payload["planned_at"]))
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("MONITOR_PLAN:steps") from error

        _require(output_hash_of(result) == stated.output_hash, "MONITOR_PLAN:output_hash")
        _require(plan_hash_of(steps) == stated.plan_hash, "MONITOR_PLAN:plan_hash")
        _require(result.invocation_id == stated.invocation_id, "MONITOR_PLAN:invocation")
        _require(result.prompt_version == stated.prompt_version, "MONITOR_PLAN:prompt_version")
        _require(
            result.model_profile_arn_hash == stated.model_profile_hash.value,
            "MONITOR_PLAN:model_profile",
        )
        _require(stated.prompt_version == MONITOR_PROMPT_VERSION, "MONITOR_PLAN:prompt_version")
        _require(stated.operation_id == scope.operation_id, "MONITOR_PLAN:operation")
        _require(stated.input_hash == frozen_input.input_hash, "MONITOR_PLAN:input_hash")
        return FrozenMonitorPlan(
            result=result,
            steps=steps,
            planned_at=planned_at,
            input_hash=stated.input_hash,
            output_hash=stated.output_hash,
            plan_hash=stated.plan_hash,
        )

    async def freeze_plan(
        self,
        scope: OperationScope,
        *,
        community_id: CommunityId,
        result: MonitorResult,
        steps: tuple[ApplyStepDescriptor, ...],
        planned_at: datetime,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        prompt_version: str,
        now: datetime,
        expires_at_epoch: int,
    ) -> FrozenMonitorPlan:
        """Persist the validated plan, after which the model is never called again.

        The provenance object is built once and is the *only* source for both the document and
        the manifest, so the two cannot be written into disagreement by an edit that touched
        one of them. Everything a later load re-derives -- the output hash, the plan hash, the
        prompt version, the model profile -- is written here from the same values it will be
        re-derived from.
        """

        provenance = MonitorPlanProvenance(
            operation_id=scope.operation_id,
            invocation_id=result.invocation_id,
            input_hash=input_hash,
            output_hash=output_hash,
            plan_hash=plan_hash_of(steps),
            prompt_version=prompt_version,
            model_profile_hash=Sha256Digest(result.model_profile_arn_hash),
        )
        document = {
            "schema": PLAN_SNAPSHOT_SCHEMA,
            "namespace": scope.namespace.value,
            "community_id": str(community_id),
            "operation_id": str(provenance.operation_id),
            "invocation_id": str(provenance.invocation_id),
            "planned_at": format_utc(planned_at),
            "input_hash": provenance.input_hash.value,
            "output_hash": provenance.output_hash.value,
            "plan_hash": provenance.plan_hash.value,
            "prompt_version": provenance.prompt_version,
            "model_profile_hash": provenance.model_profile_hash.value,
            "result": result.model_dump_json(),
            "steps": [descriptor.as_json() for descriptor in steps],
        }
        raw = canonical_bytes(document)
        await self._write(
            scope,
            manifest=MonitorSnapshotManifest(
                invocation_id=provenance.invocation_id,
                operation_id=provenance.operation_id,
                namespace=scope.namespace,
                community_id=community_id,
                kind=MonitorSnapshotKind.MONITOR_PLAN,
                content_sha256=hash_value(document),
                byte_length=len(raw),
                chunk_count=len(chunk_canonical_bytes(raw)),
                input_hash=provenance.input_hash,
                output_hash=provenance.output_hash,
                plan_hash=provenance.plan_hash,
                model_profile_hash=provenance.model_profile_hash,
                provenance_hash=provenance.digest,
                prompt_version=provenance.prompt_version,
                created_at=now,
                expires_at_epoch=expires_at_epoch,
            ),
            raw=raw,
            expires_at_epoch=expires_at_epoch,
        )
        return FrozenMonitorPlan(
            result=result,
            steps=steps,
            planned_at=planned_at,
            input_hash=input_hash,
            output_hash=output_hash,
            plan_hash=provenance.plan_hash,
        )

    # -- storage -----------------------------------------------------------------------

    async def _write(
        self,
        scope: OperationScope,
        *,
        manifest: MonitorSnapshotManifest,
        raw: bytes,
        expires_at_epoch: int,
    ) -> None:
        """Commit manifest and chunks together, treating an identical snapshot as written.

        A snapshot is immutable, so "already there" is only safe if it is *the same thing*.
        The digest the manifest carries is what settles that: an identical digest is a replay
        of this exact write, and a different one means two different frozen inputs or plans
        are claiming one invocation identity, which is a conflict rather than a merge.
        """

        chunks = chunk_canonical_bytes(raw)
        operations = [
            self.core.stage_create_monitor_snapshot_manifest(scope, manifest),
            *(
                self.core.stage_create_monitor_snapshot_chunk(
                    scope,
                    MonitorSnapshotChunk(
                        invocation_id=manifest.invocation_id,
                        operation_id=manifest.operation_id,
                        namespace=manifest.namespace,
                        community_id=manifest.community_id,
                        kind=manifest.kind,
                        index=index,
                        content=SensitiveStr(content),
                        expires_at_epoch=expires_at_epoch,
                    ),
                )
                for index, content in enumerate(chunks)
            ),
        ]
        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name=SNAPSHOT_TRANSACTION,
                    operations=tuple(operations),
                    audit_required=False,
                )
            )
        except PersistenceConflictError:
            stored = await self.core.load_monitor_snapshot_manifest(
                scope, kind=manifest.kind, invocation_id=manifest.invocation_id
            )
            if stored is None or stored.content_sha256 != manifest.content_sha256:
                raise IntegrityError(f"{manifest.kind.value}:conflict") from None

    async def _load(
        self, scope: OperationScope, kind: MonitorSnapshotKind, invocation_id: UUID
    ) -> tuple[MonitorSnapshotManifest, dict[str, object]] | None:
        manifest = await self.core.load_monitor_snapshot_manifest(
            scope, kind=kind, invocation_id=invocation_id
        )
        if manifest is None:
            return None
        chunks = await self.core.load_monitor_snapshot_chunks(scope, manifest)
        raw = "".join(chunk.content.reveal() for chunk in chunks).encode("utf-8")
        if len(raw) != manifest.byte_length:
            raise IntegrityError(f"{kind.value}:byte_length")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            # The exception is never chained into a message and never logged: what failed to
            # parse is material derived from private community text.
            raise IntegrityError(f"{kind.value}:decode") from error
        if not isinstance(payload, dict):
            raise IntegrityError(f"{kind.value}:decode")
        if hash_value(payload) != manifest.content_sha256:
            raise IntegrityError(f"{kind.value}:digest")
        return manifest, payload


def output_hash_of(result: MonitorResult) -> Sha256Digest:
    """The canonical digest of one validated Monitor answer's output.

    Defined once, here, because the value has to be computed identically at the moment the
    answer arrives and at every later load that re-proves it. Two spellings of "the hash of
    the output" would make the recomputation check pass for the wrong reason.
    """

    return hash_value(result.output.model_dump(mode="json"))


def _manifest_provenance(manifest: MonitorSnapshotManifest) -> MonitorPlanProvenance:
    """Read the provenance the manifest states, refusing a plan manifest missing any of it."""

    if (
        manifest.output_hash is None
        or manifest.plan_hash is None
        or manifest.model_profile_hash is None
        or manifest.provenance_hash is None
    ):
        raise IntegrityError("MONITOR_PLAN:provenance")
    return MonitorPlanProvenance(
        operation_id=manifest.operation_id,
        invocation_id=manifest.invocation_id,
        input_hash=manifest.input_hash,
        output_hash=manifest.output_hash,
        plan_hash=manifest.plan_hash,
        prompt_version=manifest.prompt_version,
        model_profile_hash=manifest.model_profile_hash,
    )


def _document_provenance(payload: dict[str, object]) -> MonitorPlanProvenance:
    """Read the provenance the reassembled document states, in its own right."""

    try:
        return MonitorPlanProvenance(
            operation_id=OperationId(UUID(str(payload["operation_id"]))),
            invocation_id=UUID(str(payload["invocation_id"])),
            input_hash=Sha256Digest(str(payload["input_hash"])),
            output_hash=Sha256Digest(str(payload["output_hash"])),
            plan_hash=Sha256Digest(str(payload["plan_hash"])),
            prompt_version=str(payload["prompt_version"]),
            model_profile_hash=Sha256Digest(str(payload["model_profile_hash"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError("MONITOR_PLAN:provenance") from error


def snapshot_expiry(now: datetime, retention_seconds: int) -> int:
    """The demo TTL a snapshot inherits, so it never outlives its own operation."""

    return epoch_seconds_ceiling(now) + retention_seconds


def _parse[EnvelopeT: BaseModel](model: type[EnvelopeT], raw: object) -> EnvelopeT:
    """Re-validate one strict contract envelope from the exact bytes that were stored.

    The envelope is kept as its own serialized JSON string inside the snapshot document
    rather than as a nested object, because these contracts are validated in *strict* mode:
    a UUID has to arrive as a UUID and a tuple as a tuple, which a decoded JSON object cannot
    provide. Storing the model's own serialization and re-parsing it means the value that
    comes back is the value that went in, proved by the same validator that accepted it the
    first time.
    """

    if not isinstance(raw, str):
        raise IntegrityError("SNAPSHOT:envelope")
    try:
        return model.model_validate_json(raw)
    except PydanticValidationError as error:
        # Never chained into a message and never logged: what failed to validate is derived
        # from private community text.
        raise IntegrityError("SNAPSHOT:envelope") from error


_FROZEN_INPUT_FIELDS: Final = (
    "schema",
    "namespace",
    "community_id",
    "operation_id",
    "invocation_id",
    "envelope",
    "contributor_by_pseudonym",
)
"""Exactly the fields ``input_hash`` covers.

The hash names *the payload the model was given*, so it is derived from the envelope and the
pseudonym mapping and from nothing else. Which command asked for it is recorded beside them
and checked separately: it is a fact about the request, not about the input.
"""


def _frozen_document(payload: dict[str, object]) -> dict[str, object]:
    return {name: payload[name] for name in _FROZEN_INPUT_FIELDS if name in payload}


def _input_hash(frozen: dict[str, object]) -> Sha256Digest:
    """The digest of the exact canonical bytes of one frozen ``MonitorInput`` envelope."""

    return hash_value(frozen)


def _read_message_ids(payload: dict[str, object]) -> tuple[UUID, ...]:
    raw = payload.get("command_message_ids")
    if not isinstance(raw, list):
        raise IntegrityError("MONITOR_INPUT:message_ids")
    try:
        return tuple(sorted((UUID(str(value)) for value in raw), key=str))
    except ValueError as error:
        raise IntegrityError("MONITOR_INPUT:message_ids") from error


def _require(condition: bool, entity_ref: str) -> None:
    if not condition:
        raise IntegrityError(entity_ref)


def _require_scope(
    payload: dict[str, object],
    scope: OperationScope,
    invocation_id: UUID,
    community_id: CommunityId,
) -> None:
    """Revalidate the scope fields the snapshot claims, after it has been reassembled.

    A partition key is not an authorization boundary here any more than it is anywhere else,
    so the document restates its namespace, community, operation, and invocation and is
    checked against the scope it was asked for.
    """

    _require(payload.get("namespace") == scope.namespace.value, "SNAPSHOT:namespace")
    _require(payload.get("community_id") == str(community_id), "SNAPSHOT:community")
    _require(payload.get("operation_id") == str(scope.operation_id), "SNAPSHOT:operation")
    _require(payload.get("invocation_id") == str(invocation_id), "SNAPSHOT:invocation")


def _read_pseudonyms(payload: dict[str, object]) -> dict[str, ContributorId]:
    raw = payload.get("contributor_by_pseudonym")
    if not isinstance(raw, dict):
        raise IntegrityError("MONITOR_INPUT:pseudonyms")
    try:
        return {str(pseudonym): ContributorId(UUID(str(value))) for pseudonym, value in raw.items()}
    except ValueError as error:
        raise IntegrityError("MONITOR_INPUT:pseudonyms") from error


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise IntegrityError("MONITOR_PLAN:steps")
    return value
