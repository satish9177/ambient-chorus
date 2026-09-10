"""Runtime artifact resolution and location configuration for AgentCore runtimes.

Mirroring :mod:`infra.cdk.lambda_support` in spirit: one place that resolves the AgentCore
runtime direct-code artifact, fail-closed in deployment mode, clearly-named placeholder in
offline mode.

Why the digest is the object key
--------------------------------
The final scanned ZIP is the release authority, so the object an immutable runtime version
points at is named by the exact bytes that passed the gates (deployment contract §§ 5, 12, 16):
``{agent}/{sha256}.zip``. A name-addressed key (such as ``{agent}/latest.zip``) would let a
different build silently occupy the same object in S3, destroying reproducible deployments and
version rollback guarantees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tools.build_runtime_artifacts import (
    DEFAULT_OUTPUT_ROOT as AGENTCORE_BUILD_OUTPUT_ROOT,
)
from tools.build_runtime_artifacts import (
    load_manifest,
)

RUNTIME_AGENTS: Final = ("monitor", "investigator", "action")
ARTIFACT_MANIFEST_NAME: Final = "artifacts.json"
ARTIFACT_MANIFEST_SCHEMA: Final = "agentcore-artifacts/v1"
OFFLINE_PLACEHOLDER_OBJECT_KEY: Final = "{agent}/OFFLINE-SYNTH-PLACEHOLDER-NOT-DEPLOYABLE.zip"

_SHA256_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class RuntimeArtifactMissingError(RuntimeError):
    """A deployment-capable synth was attempted without the built runtime artifact or manifest."""


class RuntimeArtifactManifestError(RuntimeError):
    """The runtime artifact manifest is malformed, invalid, or carries an unsupported schema."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeArtifactLocation:
    """Location and cryptographic digest of a packaged AgentCore direct-code runtime artifact."""

    agent: str
    bucket_name: str
    object_key: str
    sha256: str  # bare 64-char lowercase hex, no "sha256:" prefix
    deployed_name: str


def runtime_artifact_location(
    agent: str,
    *,
    bucket_name: str,
    offline: bool,
    output_root: Path = AGENTCORE_BUILD_OUTPUT_ROOT,
) -> RuntimeArtifactLocation:
    """Resolve the S3 location for an AgentCore runtime direct-code artifact.

    In offline mode, returns the clearly-named offline placeholder object key without reading
    the artifact manifest on disk. This ensures offline synthesis on clean checkouts (such as CI)
    is reproducible even when no ``build/agentcore/`` directory exists. In this case, ``sha256``
    is the empty string.

    In deployment mode, reads ``<output_root>/artifacts.json`` and fails closed if the file is
    missing, if the schema does not equal ``agentcore-artifacts/v1``, if no record matches the
    agent's manifest deployed name, or if the recorded sha256 digest is not a valid 64-character
    hexadecimal string.
    """
    if agent not in RUNTIME_AGENTS:
        raise ValueError(f"unknown agent runtime {agent!r}; expected one of {RUNTIME_AGENTS}")

    agent_manifest = load_manifest(agent)
    deployed_name = agent_manifest.deployed_name

    if offline:
        return RuntimeArtifactLocation(
            agent=agent,
            bucket_name=bucket_name,
            object_key=OFFLINE_PLACEHOLDER_OBJECT_KEY.format(agent=agent),
            sha256="",
            deployed_name=deployed_name,
        )

    manifest_file = output_root / ARTIFACT_MANIFEST_NAME
    if not manifest_file.is_file():
        raise RuntimeArtifactMissingError(
            f"deployment-capable synth needs {manifest_file} -- run "
            "`uv run python -m tools.build_runtime_artifacts` first, or select offline mode "
            "explicitly (-c offline_synth=true / CHORUS_CDK_OFFLINE_SYNTH=1)"
        )

    try:
        content = manifest_file.read_text(encoding="utf-8")
        document = json.loads(content)
    except Exception as err:
        raise RuntimeArtifactManifestError(
            f"failed to read or parse manifest document {manifest_file}: {err}"
        ) from err

    if not isinstance(document, dict):
        raise RuntimeArtifactManifestError(
            f"manifest document in {manifest_file} must be a JSON object"
        )

    schema = document.get("schema")
    if schema != ARTIFACT_MANIFEST_SCHEMA:
        raise RuntimeArtifactManifestError(
            f"unsupported manifest schema {schema!r} in {manifest_file}; "
            f"expected {ARTIFACT_MANIFEST_SCHEMA!r}"
        )

    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeArtifactManifestError(
            f"'artifacts' field in {manifest_file} must be a list of records"
        )

    matched: dict[str, object] | None = None
    for item in artifacts:
        if isinstance(item, dict) and item.get("deployed_name") == deployed_name:
            matched = item
            break

    if matched is None:
        raise RuntimeArtifactMissingError(
            f"no artifact record found for deployed_name {deployed_name!r} in {manifest_file}"
        )

    raw_sha = matched.get("sha256")
    if not isinstance(raw_sha, str):
        raise RuntimeArtifactManifestError(
            f"record for {deployed_name!r} in {manifest_file} has missing or non-string sha256"
        )

    digest = raw_sha.removeprefix("sha256:").lower()
    if not _SHA256_HEX_RE.match(digest):
        raise RuntimeArtifactManifestError(
            f"invalid sha256 digest {raw_sha!r} for {deployed_name!r} in {manifest_file}; "
            "must be a 64-character lowercase hex string"
        )

    object_key = f"{agent}/{digest}.zip"
    return RuntimeArtifactLocation(
        agent=agent,
        bucket_name=bucket_name,
        object_key=object_key,
        sha256=digest,
        deployed_name=deployed_name,
    )


__all__ = [
    "AGENTCORE_BUILD_OUTPUT_ROOT",
    "ARTIFACT_MANIFEST_NAME",
    "ARTIFACT_MANIFEST_SCHEMA",
    "OFFLINE_PLACEHOLDER_OBJECT_KEY",
    "RUNTIME_AGENTS",
    "RuntimeArtifactLocation",
    "RuntimeArtifactManifestError",
    "RuntimeArtifactMissingError",
    "runtime_artifact_location",
]
