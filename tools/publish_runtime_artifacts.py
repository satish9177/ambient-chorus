"""AgentCore artifact publication planning (offline wiring only).

Produces a deterministic publication plan describing what a later (Macro C) upload step will
do, and re-runs the final security gate against the finished ZIPs on disk.

NOTHING UPLOADS HERE.
There is no boto3, no s3, and no credentials in this module. The actual byte upload is Macro C
and is gated on this plan plus ``aws sts get-caller-identity`` proving a non-root principal.

Rollback retention note:
The artifact bucket has NO expiry rule by design (already established in data.py), because a
rollback in AgentCore is "repoint the `live` endpoint at the previous version" and expiring the
object that version was built from would delete the ability to roll back.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from infra.cdk.runtime_support import runtime_artifact_location

from tools.build_runtime_artifacts import (
    ARTIFACT_SCANNER_VERSION,
    DEFAULT_OUTPUT_ROOT,
    REPOSITORY_ROOT,
    RUNTIME_NAMES,
    inspect_archive,
    load_manifest,
)

PLAN_SCHEMA: Final = "agentcore-artifact-publication/v1"
MANIFEST_SCHEMA: Final = "agentcore-artifacts/v1"
_SHA256_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")
ENTRYPOINT_COMMAND: Final = ("python", "main.py")


class ArtifactPublicationError(RuntimeError):
    """The publication plan cannot be constructed safely."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactPublicationEntry:
    agent: str
    deployed_name: str
    archive_path: str  # repo-relative path to the built .zip
    sha256: str  # bare 64-hex, no "sha256:" prefix
    size_bytes: int
    target_bucket: str  # "chorus-agent-artifacts-{env}"
    target_key: str  # "{agent}/{sha256}.zip" -- MUST equal runtime_support's key
    python_runtime: str  # "PYTHON_3_12"
    target_platform: str  # "aarch64-manylinux2014"
    entrypoint: tuple[str, ...]  # ("python", "main.py")
    final_zip_scan: str  # the scanner id the re-scan actually ran, e.g. "artifact-secret-scan/v1"

    def as_record(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "deployed_name": self.deployed_name,
            "archive_path": self.archive_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "target_bucket": self.target_bucket,
            "target_key": self.target_key,
            "python_runtime": self.python_runtime,
            "target_platform": self.target_platform,
            "entrypoint": list(self.entrypoint),
            "final_zip_scan": self.final_zip_scan,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactPublicationPlan:
    schema: str = PLAN_SCHEMA
    environment: str
    entries: tuple[
        ArtifactPublicationEntry, ...
    ]  # exactly 3, ordered monitor, investigator, action

    def as_record(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "environment": self.environment,
            "entries": [entry.as_record() for entry in self.entries],
        }


def build_publication_plan(
    *,
    environment: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> ArtifactPublicationPlan:
    """Build a deterministic publication plan from the build manifest and finished archives."""
    manifest_path = output_root / "artifacts.json"
    if not manifest_path.is_file():
        raise ArtifactPublicationError(
            f"runtime artifact manifest missing: {manifest_path} -- run "
            "`uv run python -m tools.build_runtime_artifacts` first"
        )

    try:
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as err:
        raise ArtifactPublicationError(
            f"failed to read or parse manifest {manifest_path}: {err}"
        ) from err

    if not isinstance(manifest_doc, dict):
        raise ArtifactPublicationError(
            f"manifest document in {manifest_path} must be a JSON object"
        )

    schema = manifest_doc.get("schema")
    if schema != MANIFEST_SCHEMA:
        raise ArtifactPublicationError(
            f"unsupported manifest schema {schema!r} in {manifest_path}; "
            f"expected {MANIFEST_SCHEMA!r}"
        )

    artifacts_list = manifest_doc.get("artifacts")
    if not isinstance(artifacts_list, list):
        raise ArtifactPublicationError(f"'artifacts' field in {manifest_path} must be a list")

    entries: list[ArtifactPublicationEntry] = []
    for agent in RUNTIME_NAMES:
        agent_manifest = load_manifest(agent, root=repository_root / "runtimes")
        deployed_name = agent_manifest.deployed_name

        matched: dict[str, object] | None = None
        for item in artifacts_list:
            if isinstance(item, dict) and (
                item.get("deployed_name") == deployed_name
                or item.get("runtime") == agent_manifest.name
            ):
                matched = item
                break

        if matched is None:
            raise ArtifactPublicationError(
                f"no artifact record found for agent {agent!r} "
                f"({deployed_name!r}) in {manifest_path}"
            )

        archive_filename = matched.get("archive")
        if not isinstance(archive_filename, str) or not archive_filename:
            raise ArtifactPublicationError(
                f"record for {agent!r} in {manifest_path} is missing 'archive' filename"
            )

        archive_file = output_root / archive_filename
        if not archive_file.is_file():
            raise ArtifactPublicationError(
                f"built zip for agent {agent!r} is missing at {archive_file}"
            )

        # 2b: Re-run the final-ZIP security gate against that finished ZIP on disk
        problems = inspect_archive(
            archive_file,
            target_platform=agent_manifest.target_platform,
            first_party=agent_manifest.archive_first_party,
        )
        if problems:
            raise ArtifactPublicationError(
                f"{agent} archive {archive_filename} failed final security scan: "
                + "; ".join(problems)
            )

        final_zip_scan = str(matched.get("security_scan", ARTIFACT_SCANNER_VERSION))

        # 2c: strip any sha256: prefix from manifest digest; assert ^[0-9a-f]{64}$ or raise
        raw_sha = matched.get("sha256")
        if not isinstance(raw_sha, str):
            raise ArtifactPublicationError(
                f"record for {agent!r} in {manifest_path} has missing or non-string 'sha256'"
            )
        digest = raw_sha.removeprefix("sha256:").lower()
        if not _SHA256_HEX_RE.match(digest):
            raise ArtifactPublicationError(
                f"invalid sha256 digest {raw_sha!r} for agent {agent!r} in {manifest_path}; "
                "must be a 64-character lowercase hex string"
            )

        # 2d: target_key and target_bucket
        target_bucket = f"chorus-agent-artifacts-{environment}"
        target_key = f"{agent}/{digest}.zip"

        # Assert this equals what runtime_support.runtime_artifact_location returns
        cdk_location = runtime_artifact_location(
            agent,
            bucket_name=target_bucket,
            offline=False,
            output_root=output_root,
        )
        if cdk_location.object_key != target_key:
            raise ArtifactPublicationError(
                f"target_key mismatch for {agent}: publication plan computed {target_key!r} "
                f"but runtime_support computed {cdk_location.object_key!r}"
            )

        try:
            rel_archive_path = archive_file.relative_to(repository_root).as_posix()
        except ValueError:
            rel_archive_path = archive_file.as_posix()

        python_runtime = str(
            matched.get(
                "python_runtime", f"PYTHON_{agent_manifest.python_version.replace('.', '_')}"
            )
        )
        target_platform = str(matched.get("target_platform", agent_manifest.target_platform))
        size_bytes = archive_file.stat().st_size

        entries.append(
            ArtifactPublicationEntry(
                agent=agent,
                deployed_name=deployed_name,
                archive_path=rel_archive_path,
                sha256=digest,
                size_bytes=size_bytes,
                target_bucket=target_bucket,
                target_key=target_key,
                python_runtime=python_runtime,
                target_platform=target_platform,
                entrypoint=ENTRYPOINT_COMMAND,
                final_zip_scan=final_zip_scan,
            )
        )

    plan = ArtifactPublicationPlan(
        schema=PLAN_SCHEMA,
        environment=environment,
        entries=tuple(entries),
    )

    output_root.mkdir(parents=True, exist_ok=True)
    plan_file = output_root / f"publication-plan-{environment}.json"
    plan_file.write_text(
        json.dumps(plan.as_record(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment", default="demo", help="Target deployment environment (default: demo)"
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT_ROOT, help="AgentCore build output root"
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help="Repository root path")
    args = parser.parse_args(argv)

    try:
        plan = build_publication_plan(
            environment=args.environment,
            output_root=args.output,
            repository_root=args.root,
        )
    except ArtifactPublicationError as err:
        sys.stderr.write(f"publication planning failed: {err}\n")
        return 1

    for entry in plan.entries:
        sys.stdout.write(
            f"{entry.agent} {entry.target_bucket}/{entry.target_key} "
            f"{entry.size_bytes}B scan={entry.final_zip_scan}\n"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
