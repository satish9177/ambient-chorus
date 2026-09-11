"""One place where a Lambda ``Function`` and its environment are shaped, for all five functions.

The compute batch (deployment contract SS 14) adds five ``Function`` resources across four
identity stacks. Rather than scatter a literal ``environment=`` dict and a hand-written
``lambda.Function(...)`` through each stack, both come from here:

* :func:`chorus_lambda` builds a ``Function`` from the same ``functions/<name>/lambda.toml`` the
  packaging tool reads -- so the runtime, architecture, handler, memory and timeout a template
  test asserts are the ones the artifact was actually built for, and cannot drift (SS 44). Its
  **physical name is derived from the configured environment string**, never parsed back out of
  a CloudFormation token (review P2-1);
* the ``*_environment`` builders assemble each function's ``CHORUS_*`` environment from one
  typed, non-secret path (SS 28). Environment values are resource names, ARNs of secret
  identities, table/bucket names, function/runtime target ARNs, namespace, and non-secret
  identifiers -- never a bearer token, a cursor HMAC key, a destination address, private data,
  secret JSON, or an AWS key. Only the worker carries the three AgentCore runtime-endpoint ARNs,
  because only the worker invokes an agent (review P2-8).

Code asset resolution -- deployment mode vs. offline mode (review P2-3)
--------------------------------------------------------------------
``lambda.Code.from_asset`` needs a path that exists at synth time. In **deployment-capable**
mode :func:`lambda_asset_code` requires ``build/lambda/<name>.zip`` and raises
:class:`LambdaArtifactMissingError` if it is absent -- there is no silent placeholder fallback,
so a normal ``python infra/cdk/app.py`` or ``cdk deploy`` cannot publish the wrong asset. In
**offline** mode it uses the clearly-named :data:`OFFLINE_PLACEHOLDER_CODE_DIR` fixture, whose
asset identity is not a real ZIP's.

Logging (SS 10)
---------------
Each identity stack already creates a dedicated ``logs.LogGroup`` and each execution role
already carries the exact ``logs:CreateLogStream`` / ``PutLogEvents`` statements scoped to it.
:func:`chorus_lambda` wires the function to that existing group via ``log_group=`` and never
lets Lambda create a ``/aws/lambda/<name>`` default. With an explicit ``role=``, CDK attaches no
``AWSLambdaBasicExecutionRole`` and adds no permissions of its own (SS 43).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct
from tools.build_lambda_artifacts import (
    DEFAULT_OUTPUT_ROOT as LAMBDA_BUILD_OUTPUT_ROOT,
)
from tools.build_lambda_artifacts import (
    LambdaManifest,
    load_lambda_manifest,
)

from infra.cdk.config import PHASE_11_REGION, CdkBuildConfig, DeploymentIdentities

RUNTIME_BY_PYTHON: Final = {"3.12": lambda_.Runtime.PYTHON_3_12}
ARCHITECTURE_BY_NAME: Final = {
    "x86_64": lambda_.Architecture.X86_64,
    "arm64": lambda_.Architecture.ARM_64,
}

OFFLINE_PLACEHOLDER_CODE_DIR: Final = Path(__file__).resolve().parent / "_lambda_placeholder"
"""Offline-review fixture code. Never selected in deployment mode (see module doc)."""

AGENT_MODE_AGENTCORE: Final = "agentcore"
DEMO_CLOCK_ENABLED: Final = "true"


class LambdaArtifactMissingError(RuntimeError):
    """A deployment-capable synth was attempted without the function's built ZIP (review P2-3)."""


def lambda_asset_code(function_name: str, *, offline: bool) -> lambda_.Code:
    """The built artifact, or -- only in offline mode -- the placeholder fixture."""

    built = LAMBDA_BUILD_OUTPUT_ROOT / f"{function_name}.zip"
    if built.is_file():
        return lambda_.Code.from_asset(str(built))
    if offline:
        return lambda_.Code.from_asset(str(OFFLINE_PLACEHOLDER_CODE_DIR))
    raise LambdaArtifactMissingError(
        f"deployment-capable synth needs {built} -- run "
        f"`uv run python -m tools.build_lambda_artifacts` first, or select offline mode "
        f"explicitly (-c offline_synth=true / CHORUS_CDK_OFFLINE_SYNTH=1)"
    )


def artifact_is_built(function_name: str) -> bool:
    """Whether the real Lambda zip exists on disk."""

    return (LAMBDA_BUILD_OUTPUT_ROOT / f"{function_name}.zip").is_file()


def chorus_lambda(
    scope: Construct,
    construct_id: str,
    *,
    config: CdkBuildConfig,
    manifest: LambdaManifest,
    role: iam.IRole,
    environment: dict[str, str],
    log_group: logs.ILogGroup,
    offline_synth: bool,
    current_version_options: lambda_.VersionOptions | None = None,
    vpc: ec2.IVpc | None = None,
    vpc_subnets: ec2.SubnetSelection | None = None,
    security_groups: list[ec2.ISecurityGroup] | None = None,
) -> lambda_.Function:
    """Build one production Lambda from its manifest and its pre-existing identity.

    ``function_name`` is ``{manifest.name}-{config.environment}`` -- taken straight from the
    configured environment string, never parsed out of a token (review P2-1). ``role`` is the
    stack's already-synthesized execution role, so CDK attaches no managed policy (SS 43) --
    **including** ``AWSLambdaVPCAccessExecutionRole`` when ``vpc`` is set: with an explicit
    ``role=`` CDK adds no VPC-access policy, and the exact inline ENI permissions come from
    :func:`infra.cdk.network_support.vpc_eni_policy_statements` on the caller's role instead
    (deployment contract §§ 14-17). ``vpc`` / ``vpc_subnets`` / ``security_groups`` are supplied
    only for the three VPC-attached functions (worker, compiler, sender); the API and watcher
    pass none and synthesize with no ``VpcConfig`` (§ 2).
    """

    runtime = RUNTIME_BY_PYTHON.get(manifest.python_version)
    if runtime is None:  # pragma: no cover - guarded by the manifest schema
        raise ValueError(f"unsupported Lambda python version {manifest.python_version!r}")
    architecture = ARCHITECTURE_BY_NAME.get(manifest.architecture)
    if architecture is None:  # pragma: no cover - guarded by the manifest schema
        raise ValueError(f"unsupported Lambda architecture {manifest.architecture!r}")

    return lambda_.Function(
        scope,
        construct_id,
        function_name=f"{manifest.name}-{config.environment}",
        description=manifest.description,
        runtime=runtime,
        architecture=architecture,
        handler=manifest.handler,
        code=lambda_asset_code(manifest.name, offline=offline_synth),
        role=role,
        environment=dict(sorted(environment.items())),
        timeout=Duration.seconds(manifest.timeout_seconds),
        memory_size=manifest.memory_mb,
        log_group=log_group,
        current_version_options=current_version_options,
        vpc=vpc,
        vpc_subnets=vpc_subnets if vpc is not None else None,
        security_groups=security_groups if vpc is not None else None,
        allow_public_subnet=False,
    )


# ---------------------------------------------------------------------------------------------
# Environment builders -- one non-secret path (deployment contract SS 28)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceNames:
    """The non-secret resource names every function's environment is built from.

    Deterministic literals derived from the environment token -- the same shape every stack in
    this repository already uses for a cross-resource name -- so the value in a function's
    environment is legible in the synthesized template.
    """

    core_table: str
    shareable_table: str
    audit_table: str
    private_evidence_bucket: str
    export_evidence_bucket: str
    ses_configuration_set: str
    scheduler_group: str

    @classmethod
    def for_config(cls, config: CdkBuildConfig) -> ResourceNames:
        env = config.environment
        return cls(
            core_table=f"chorus-core-{env}",
            shareable_table=f"chorus-shareable-{env}",
            audit_table=f"chorus-audit-{env}",
            private_evidence_bucket=f"chorus-private-evidence-{env}",
            export_evidence_bucket=f"chorus-export-evidence-{env}",
            ses_configuration_set=f"chorus-{env}",
            scheduler_group=f"chorus-{env}",
        )


def _base_environment(config: CdkBuildConfig) -> dict[str, str]:
    """The safe identifiers every function carries: environment, namespace, region.

    ``CHORUS_AWS_REGION`` is ``config.aws_region`` -- the same frozen region every stack is
    created for. ``AWS_REGION`` is reserved by the Lambda runtime (which populates it with the
    function's own region) and CDK refuses a manual value, so it is not set here.
    """

    return {
        "CHORUS_ENVIRONMENT": config.environment,
        "CHORUS_NAMESPACE": config.namespace,
        "CHORUS_AWS_REGION": config.aws_region,
    }


def _demo_agent_mode(config: CdkBuildConfig) -> dict[str, str]:
    """The one demo-wide invariant every function's ``Settings`` still validates.

    ``Settings.validate_environment_contract`` requires ``agent_mode=agentcore`` in ``demo``.
    That is *all* it requires now -- the "and all six runtime/profile ARNs" clause moved into
    the worker's own composition mapper (review P2-8) -- so the non-agent functions carry only
    this flag, not runtime or profile ARNs their IAM denies anyway.
    """

    if config.environment != "demo":
        return {}
    return {"CHORUS_AGENT_MODE": AGENT_MODE_AGENTCORE}


def _tables(names: ResourceNames, *, include_core: bool = True) -> dict[str, str]:
    tables = {
        "CHORUS_SHAREABLE_TABLE": names.shareable_table,
        "CHORUS_AUDIT_TABLE": names.audit_table,
    }
    if include_core:
        tables["CHORUS_CORE_TABLE"] = names.core_table
    return tables


def api_environment(
    *,
    config: CdkBuildConfig,
    names: ResourceNames,
    worker_function_arn: str,
    compiler_function_arn: str,
    watcher_alias_arn: str,
    demo_access_secret_arn: str,
    cursor_signing_secret_arn: str,
) -> dict[str, str]:
    """The API request path (deployment contract SS 11).

    No bucket, KMS, or AgentCore variable -- the deployed API has no S3 client, no safe-evidence
    service, and invokes no agent (review P2-8). The watcher ARN is the qualified ``:live``
    alias (SS 38). ``demo_access_secret_arn`` and ``cursor_signing_secret_arn`` are passed
    explicitly -- the same strings the API role's two ``GetSecretValue`` grants name (SS 40).
    ``CHORUS_DYNAMODB_ENDPOINT`` is deliberately absent -- ``api_settings`` refuses to construct
    when it is set at all.
    """

    return {
        **_base_environment(config),
        **_tables(names),
        **_demo_agent_mode(config),
        "CHORUS_DEMO_CLOCK_ENABLED": DEMO_CLOCK_ENABLED,
        "CHORUS_DEMO_ACCESS_SECRET_ARN": demo_access_secret_arn,
        "CHORUS_CURSOR_SIGNING_SECRET_ARN": cursor_signing_secret_arn,
        "CHORUS_WORKER_FUNCTION_ARN": worker_function_arn,
        "CHORUS_COMPILER_FUNCTION_ARN": compiler_function_arn,
        "CHORUS_WATCHER_FUNCTION_ARN": watcher_alias_arn,
    }


def worker_environment(
    *,
    config: CdkBuildConfig,
    names: ResourceNames,
    monitor_runtime_arn: str | None = None,
    investigator_runtime_arn: str | None = None,
    action_runtime_arn: str | None = None,
    compiler_function_arn: str,
    sender_function_arn: str,
    watcher_alias_arn: str,
    scheduler_role_arn: str,
    identities: DeploymentIdentities | None = None,
) -> dict[str, str]:
    """The asynchronous operation worker (deployment contract SS 14).

    The **only** function that carries the three AgentCore runtime-endpoint ARNs, because it is
    the only principal that invokes an agent (review P2-8). No model-profile ARN -- no worker
    adapter consumes one. No secret ARN of any kind -- the worker reads no secret and is denied
    every ``GetSecretValue`` (SS 40). The watcher ARN is the scheduler's ``:live`` target (SS 37).
    """

    if identities is not None:
        monitor_runtime_arn = monitor_runtime_arn or identities.monitor_runtime_arn
        investigator_runtime_arn = investigator_runtime_arn or identities.investigator_runtime_arn
        action_runtime_arn = action_runtime_arn or identities.action_runtime_arn

    return {
        **_base_environment(config),
        **_tables(names),
        **_demo_agent_mode(config),
        "CHORUS_MONITOR_RUNTIME_ARN": monitor_runtime_arn or "",
        "CHORUS_INVESTIGATOR_RUNTIME_ARN": investigator_runtime_arn or "",
        "CHORUS_ACTION_RUNTIME_ARN": action_runtime_arn or "",
        "CHORUS_AGENT_TIMEOUT_SECONDS": "90",
        "CHORUS_DEMO_CLOCK_ENABLED": DEMO_CLOCK_ENABLED,
        "CHORUS_SES_CONFIGURATION_SET": names.ses_configuration_set,
        "CHORUS_COMPILER_FUNCTION_ARN": compiler_function_arn,
        "CHORUS_SENDER_FUNCTION_ARN": sender_function_arn,
        "CHORUS_WATCHER_FUNCTION_ARN": watcher_alias_arn,
        "CHORUS_SCHEDULER_GROUP": names.scheduler_group,
        "CHORUS_SCHEDULER_ENVIRONMENT": config.scheduler_environment,
        "CHORUS_SCHEDULER_ROLE_ARN": scheduler_role_arn,
    }


def compiler_environment(
    *,
    config: CdkBuildConfig,
    names: ResourceNames,
    private_evidence_key_arn: str,
    export_evidence_key_arn: str,
) -> dict[str, str]:
    """The deterministic compiler (deployment contract SS 16).

    The two KMS key ARNs are required -- ``S3ObjectStore`` passes them as ``SSEKMSKeyId`` and a
    compiler with no key ARN fails every safe-evidence write under the bucket policy (SS 9). No
    SES, no scheduler, and **no AgentCore or model configuration at all** (review P2-8).
    """

    return {
        **_base_environment(config),
        **_tables(names),
        **_demo_agent_mode(config),
        "CHORUS_DEMO_CLOCK_ENABLED": DEMO_CLOCK_ENABLED,
        "CHORUS_PRIVATE_EVIDENCE_BUCKET": names.private_evidence_bucket,
        "CHORUS_EXPORT_EVIDENCE_BUCKET": names.export_evidence_bucket,
        "CHORUS_PRIVATE_EVIDENCE_KEY_ARN": private_evidence_key_arn,
        "CHORUS_EXPORT_EVIDENCE_KEY_ARN": export_evidence_key_arn,
    }


def sender_environment(
    *,
    config: CdkBuildConfig,
    names: ResourceNames,
    compiler_function_arn: str,
    destination_registry_secret_arn: str,
) -> dict[str, str]:
    """The external sender (deployment contract SS 18-19).

    ``destination_registry_secret_arn`` is passed explicitly -- the same string the sender role's
    ``GetSecretValue`` grant names (SS 40). No recipient address, no registry contents, no
    routing secret value; **no AgentCore or model configuration** (review P2-8). The compiler
    ARN is the fence operation the sender invokes.
    """

    return {
        **_base_environment(config),
        **_tables(names),
        **_demo_agent_mode(config),
        "CHORUS_DEMO_CLOCK_ENABLED": DEMO_CLOCK_ENABLED,
        "CHORUS_SES_CONFIGURATION_SET": names.ses_configuration_set,
        "CHORUS_DESTINATION_REGISTRY_SECRET_ARN": destination_registry_secret_arn,
        "CHORUS_COMPILER_FUNCTION_ARN": compiler_function_arn,
    }


def demo_reset_environment(
    *,
    config: CdkBuildConfig,
    names: ResourceNames,
    private_evidence_key_arn: str,
    export_evidence_key_arn: str,
) -> dict[str, str]:
    """The dedicated demo reset function (deployment contract §§ 12, 19-26; review R5-D).

    All three tables and both evidence buckets (the reset principal purges every DEMO-namespace
    partition and every ``ns/DEMO/`` object prefix, and deterministically reseeds the two
    fixture evidence objects), the two evidence KMS key ARNs (``S3ObjectStore`` passes the
    private one as ``SSEKMSKeyId`` on the reseed write), the schedule group, and the demo clock
    flag. **No secret ARN of any kind** -- the reset principal reads no secret (§ 26), and no
    model / SES / worker / compiler ARN, because it invokes nothing.
    """

    return {
        **_base_environment(config),
        **_tables(names),
        **_demo_agent_mode(config),
        "CHORUS_DEMO_CLOCK_ENABLED": DEMO_CLOCK_ENABLED,
        "CHORUS_PRIVATE_EVIDENCE_BUCKET": names.private_evidence_bucket,
        "CHORUS_EXPORT_EVIDENCE_BUCKET": names.export_evidence_bucket,
        "CHORUS_PRIVATE_EVIDENCE_KEY_ARN": private_evidence_key_arn,
        "CHORUS_EXPORT_EVIDENCE_KEY_ARN": export_evidence_key_arn,
        "CHORUS_SCHEDULER_GROUP": names.scheduler_group,
        "CHORUS_SCHEDULER_ENVIRONMENT": config.scheduler_environment,
    }


def watcher_environment(
    *,
    config: CdkBuildConfig,
    names: ResourceNames,
) -> dict[str, str]:
    """The commitment watcher (deployment contract SS 21).

    No ``CHORUS_CORE_TABLE`` -- ``WatcherSettings`` has no ``core_table`` field and the role is
    denied Core outright. No scheduler client, no SES, **no AgentCore or model configuration**
    (review P2-8).
    """

    return {
        **_base_environment(config),
        **_tables(names, include_core=False),
        **_demo_agent_mode(config),
        "CHORUS_DEMO_CLOCK_ENABLED": DEMO_CLOCK_ENABLED,
    }


def inbound_environment(
    *,
    config: CdkBuildConfig,
    names: ResourceNames,
    private_evidence_key_arn: str,
    inbound_source_arn: str,
    inbound_receiving_address: str,
    inbound_topic_arn: str | None = None,
) -> dict[str, str]:
    """The inbound reply entry point (deployment contract § 10; ADR-026, ADR-030).

    Carries Core, Shareable, Audit tables and the private evidence bucket + key.
    No SES send, no Bedrock, no AgentCore, no Secrets Manager, no Scheduler variables.
    """

    env = {
        **_base_environment(config),
        **_tables(names),
        **_demo_agent_mode(config),
        "CHORUS_DEMO_CLOCK_ENABLED": DEMO_CLOCK_ENABLED,
        "CHORUS_PRIVATE_EVIDENCE_BUCKET": names.private_evidence_bucket,
        "CHORUS_PRIVATE_EVIDENCE_KEY_ARN": private_evidence_key_arn,
        "CHORUS_INBOUND_TRANSPORT": "aws:ses-receipt",
        "CHORUS_INBOUND_SOURCE_ARN": inbound_source_arn,
        "CHORUS_INBOUND_RECEIVING_ADDRESS": inbound_receiving_address,
    }
    if inbound_topic_arn:
        env["CHORUS_INBOUND_TOPIC_ARN"] = inbound_topic_arn
    return env


__all__ = [
    "OFFLINE_PLACEHOLDER_CODE_DIR",
    "PHASE_11_REGION",
    "LambdaArtifactMissingError",
    "ResourceNames",
    "api_environment",
    "artifact_is_built",
    "chorus_lambda",
    "compiler_environment",
    "demo_reset_environment",
    "inbound_environment",
    "lambda_asset_code",
    "load_lambda_manifest",
    "sender_environment",
    "watcher_environment",
    "worker_environment",
]
