"""Non-secret CDK build configuration, the frozen region, and the deployment-identity contract.

Two synthesis modes exist and they are chosen **explicitly**, never by a hidden fallback
(review P2-3, P2-4):

* **deployment-capable** (the default) -- every Lambda artifact must be built and every required
  deployment identity must be a real, validated ARN. A missing artifact or a
  placeholder/synthetic identity is a hard failure *before* synth, so a normal ``cdk deploy``
  or ``python infra/cdk/app.py`` can never publish a wrong asset or a sentinel ARN.
* **offline review** -- selected deliberately with ``offline=True`` (or ``-c offline_synth=true``
  / ``CHORUS_CDK_OFFLINE_SYNTH=1``). Uses the placeholder Lambda code fixture and clearly-typed
  synthetic identity fixtures. Its output is never deploy-ready and the placeholder asset's
  identity differs from any real ZIP's.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

PHASE_11_REGION = "us-east-1"
"""The single region Phase 11 deploys into (deployment contract SS 3).

Frozen here so ``CdkBuildConfig`` can reject any other value and every stack is created for it
through ``Environment(region=...)`` -- no stack ever synthesizes as ``unknown-region`` and no
helper hard-codes the string independently (review P2-2).
"""

DISPOSABLE_ENVIRONMENTS = frozenset({"development", "test"})
"""Environments whose data may be destroyed with the stack.

Anything else is treated as durable. The default is deliberately fail-safe: an environment
name nobody anticipated keeps deletion protection and point-in-time recovery enabled.
"""

_SCHEDULER_ENVIRONMENT = {"development": "dev", "test": "test", "demo": "demo"}
"""The short ``{env}`` token used inside the frozen EventBridge Scheduler name.

``chorus-{env}-{namespace_hash8}-{commitment_id}-{generation}`` spends 56 of Scheduler's 64
name characters on its fixed parts, so the environment word has exactly eight -- and
``development`` is eleven. This maps the deployment environment onto the bounded token
``Settings.scheduler_environment`` expects; an unrecognised environment is truncated to eight
characters rather than guessed at.
"""

_ACCOUNT_RE = re.compile(r"^\d{12}$")
_SENTINEL_ACCOUNT = "000000000000"
_ARN_RE = re.compile(
    r"^arn:aws:(?P<service>[a-z0-9-]+):(?P<region>[a-z0-9-]*):(?P<account>\d{12}|):(?P<rest>.+)$"
)
_PLACEHOLDER_MARKERS = ("PLACEHOLDER", "-000000000000:", "unknown-account")

# The **resource portion** each identity's ARN must carry -- not merely the service prefix
# (review R1). AWS's own ARN shapes:
_SECRET_RESOURCE_RE = re.compile(r"^secret:.+$")
"""``secret:<name>[-<suffix>]`` -- a Secrets Manager *secret* resource with a non-empty id.

Rejects ``not-a-secret``, ``parameter/foo``, ``secret`` alone, and ``secret:`` with no id. The
human-readable name is not constrained further because the deployment contract freezes no exact
secret name.
"""
_RUNTIME_ENDPOINT_RESOURCE_RE = re.compile(r"^runtime/[^/\s]+/runtime-endpoint/[^/\s]+$")
"""``runtime/<runtime-id>/runtime-endpoint/<endpoint-id>`` -- the full AgentCore **endpoint**
resource, both components non-empty.

Rejects a bare ``runtime/<id>`` with no endpoint, ``not-a-runtime``, a wrong child segment,
and an empty runtime or endpoint component.
"""
_MODEL_PROFILE_RESOURCE_RE = re.compile(r"^application-inference-profile/[^/\s]+$")
"""``application-inference-profile/<id>`` -- a Bedrock *application* inference profile."""


@dataclass(frozen=True, slots=True)
class CdkBuildConfig:
    """Stable tags and the frozen region for the synthesis proof."""

    project: str = "ambient-chorus"
    environment: str = "development"
    namespace: str = "LOCAL"
    aws_region: str = PHASE_11_REGION
    account: str | None = None
    """The 12-digit deployment account, or ``None`` to synthesize account-agnostic.

    Never invented. Supplied through ``-c account=...`` (or ``CDK_DEFAULT_ACCOUNT``) for a real
    deployment; left ``None`` for offline synth, which the deploy CLI's identity check would
    reject anyway.
    """

    def __post_init__(self) -> None:
        if self.aws_region != PHASE_11_REGION:
            raise ValueError(
                f"Phase 11 is frozen to {PHASE_11_REGION!r}; refusing aws_region "
                f"{self.aws_region!r} (deployment contract SS 3)"
            )
        if self.account is not None and not _ACCOUNT_RE.match(self.account):
            raise ValueError(f"account must be 12 digits or None, not {self.account!r}")

    @property
    def is_disposable(self) -> bool:
        """Whether stored data in this environment may be destroyed with the stack."""

        return self.environment in DISPOSABLE_ENVIRONMENTS

    @property
    def scheduler_environment(self) -> str:
        """The bounded ``{env}`` token the frozen schedule name and ``Settings`` expect."""

        return _SCHEDULER_ENVIRONMENT.get(self.environment, self.environment[:8])


# ---------------------------------------------------------------------------------------------
# Deployment identities: real ARNs in deployment mode, typed fixtures in offline mode
# ---------------------------------------------------------------------------------------------


class MissingDeploymentIdentityError(ValueError):
    """A required deployment identity is absent, malformed, or a synthetic placeholder.

    Raised in deployment-capable mode before any stack is constructed, so a synthetic
    account-zero ARN can never reach CloudFormation (review P2-4).
    """


def _offline_secret_arn(environment: str, name: str) -> str:
    """A clearly-typed offline fixture for a pre-created Secrets Manager identity.

    An **ARN, never a value**, and deliberately shaped so :func:`_validate_identity` rejects it
    in deployment mode: it carries the sentinel account ``000000000000`` and a ``PLACEHOLDER``
    marker. It exists only so an explicit offline synth and the template tests have a concrete
    string to compare (deployment contract SS 40). The secret's contents are created out of band
    and are never synthesized, generated, or defaulted (SS 12, SS 13).
    """

    return (
        f"arn:aws:secretsmanager:{PHASE_11_REGION}:{_SENTINEL_ACCOUNT}:secret:"
        f"chorus-{environment}-{name}-PLACEHOLDER"
    )


def _offline_runtime_arn(environment: str, agent: str) -> str:
    """A clearly-typed offline fixture for an AgentCore runtime endpoint this batch does not create.

    The three AgentCore Runtime resources belong to a later Phase 11 batch (deployment contract
    SS 5, SS 16 stage 5). In deployment mode the worker's runtime targets must be real ARNs
    supplied through context; this fixture is refused there.
    """

    return (
        f"arn:aws:bedrock-agentcore:{PHASE_11_REGION}:{_SENTINEL_ACCOUNT}:runtime/"
        f"chorus_{agent}-PLACEHOLDER/runtime-endpoint/live"
    )


def _offline_model_profile_arn(environment: str, agent: str) -> str:
    """A clearly-typed offline fixture for a **discovered** application inference-profile ARN.

    Never constructed from a name in a real deployment (deployment contract SS 4). Optional even
    then -- no batch-5 component consumes it -- so it is validated for shape only when supplied.
    """

    return (
        f"arn:aws:bedrock:{PHASE_11_REGION}:{_SENTINEL_ACCOUNT}:application-inference-profile/"
        f"chorus-{agent}-{environment}-PLACEHOLDER"
    )


def _validate_identity(
    value: str,
    *,
    field_name: str,
    service: str,
    resource_re: re.Pattern[str],
    resource_kind: str,
    expect_region: bool = True,
) -> str:
    """Deployment-mode validation of one identity ARN (review P2-4, R1).

    Rejects: absent, a malformed ARN, wrong service, wrong region, the sentinel account
    ``000000000000``, any known placeholder/sentinel marker, **and a resource portion whose
    shape is not the expected ``resource_kind``** -- a Secrets Manager ARN that does not name a
    ``secret:`` resource, or an AgentCore ARN that stops at ``runtime/<id>`` with no
    ``/runtime-endpoint/<id>``. Returns the value unchanged when it passes.
    """

    if not value:
        raise MissingDeploymentIdentityError(
            f"{field_name} is required in deployment mode and was not supplied "
            f"(pass -c {field_name}=<arn> or use offline mode)"
        )
    if any(marker in value for marker in _PLACEHOLDER_MARKERS):
        raise MissingDeploymentIdentityError(
            f"{field_name}={value!r} is a synthetic placeholder; deployment mode needs a real ARN"
        )
    match = _ARN_RE.match(value)
    if match is None:
        raise MissingDeploymentIdentityError(f"{field_name}={value!r} is not a well-formed ARN")
    if match["service"] != service:
        raise MissingDeploymentIdentityError(
            f"{field_name}={value!r} names service {match['service']!r}, expected {service!r}"
        )
    if match["account"] in ("", _SENTINEL_ACCOUNT):
        raise MissingDeploymentIdentityError(f"{field_name}={value!r} carries no real account id")
    if expect_region and match["region"] != PHASE_11_REGION:
        raise MissingDeploymentIdentityError(
            f"{field_name}={value!r} is in region {match['region']!r}, expected {PHASE_11_REGION!r}"
        )
    if resource_re.match(match["rest"]) is None:
        raise MissingDeploymentIdentityError(
            f"{field_name}={value!r} resource {match['rest']!r} is not a valid {resource_kind}"
        )
    return value


@dataclass(frozen=True, slots=True)
class DeploymentIdentities:
    """The deployment-time secret and runtime ARNs the compute stacks consume.

    Every field is a non-secret ARN; no secret **value** ever appears here, in context, in a
    CloudFormation parameter, in an environment variable, or in an output (deployment contract
    SS 29).

    In **deployment mode** (:meth:`from_context` with ``offline=False``) each *required* identity
    -- the three secret ARNs and the three AgentCore runtime-endpoint ARNs -- must be a real,
    validated ARN from context; a missing or synthetic one raises before synth. The three
    model-profile ARNs are optional (no batch-5 component consumes one) and shape-checked only
    when supplied.

    In **offline mode** the fields are filled with clearly-typed synthetic fixtures
    (:func:`_offline_secret_arn` and friends) that deployment-mode validation would reject.
    """

    environment: str = "development"
    offline: bool = field(default=True, kw_only=True)
    demo_access_secret_arn: str = ""
    cursor_signing_secret_arn: str = ""
    destination_registry_secret_arn: str = ""
    monitor_runtime_arn: str = ""
    investigator_runtime_arn: str = ""
    action_runtime_arn: str = ""
    monitor_model_profile_arn: str = ""
    investigator_model_profile_arn: str = ""
    action_model_profile_arn: str = ""

    def __post_init__(self) -> None:
        if not self.offline:
            return
        fixtures = {
            "demo_access_secret_arn": _offline_secret_arn(self.environment, "demo-access"),
            "cursor_signing_secret_arn": _offline_secret_arn(self.environment, "cursor-signing"),
            "destination_registry_secret_arn": _offline_secret_arn(
                self.environment, "destination-registry"
            ),
            "monitor_runtime_arn": _offline_runtime_arn(self.environment, "monitor"),
            "investigator_runtime_arn": _offline_runtime_arn(self.environment, "investigator"),
            "action_runtime_arn": _offline_runtime_arn(self.environment, "action"),
            "monitor_model_profile_arn": _offline_model_profile_arn(self.environment, "monitor"),
            "investigator_model_profile_arn": _offline_model_profile_arn(
                self.environment, "investigator"
            ),
            "action_model_profile_arn": _offline_model_profile_arn(self.environment, "action"),
        }
        for name, value in fixtures.items():
            if not getattr(self, name):
                object.__setattr__(self, name, value)

    @classmethod
    def from_context(
        cls, environment: str, lookup: object, *, offline: bool
    ) -> DeploymentIdentities:
        """Build from CDK context. ``offline`` selects the mode (see class docstring)."""

        get = lookup if callable(lookup) else (lambda _key: None)

        def _ctx(key: str) -> str:
            value = get(key)
            return value if isinstance(value, str) and value else ""

        raw = cls(
            environment=environment,
            offline=offline,
            demo_access_secret_arn=_ctx("demo_access_secret_arn"),
            cursor_signing_secret_arn=_ctx("cursor_signing_secret_arn"),
            destination_registry_secret_arn=_ctx("destination_registry_secret_arn"),
            monitor_runtime_arn=_ctx("monitor_runtime_arn"),
            investigator_runtime_arn=_ctx("investigator_runtime_arn"),
            action_runtime_arn=_ctx("action_runtime_arn"),
            monitor_model_profile_arn=_ctx("monitor_model_profile_arn"),
            investigator_model_profile_arn=_ctx("investigator_model_profile_arn"),
            action_model_profile_arn=_ctx("action_model_profile_arn"),
        )
        if offline:
            return raw
        return raw._validated()

    def _validated(self) -> DeploymentIdentities:
        """Return self after asserting every required identity is a real ARN (deployment mode)."""

        for field_name in (
            "demo_access_secret_arn",
            "cursor_signing_secret_arn",
            "destination_registry_secret_arn",
        ):
            _validate_identity(
                getattr(self, field_name),
                field_name=field_name,
                service="secretsmanager",
                resource_re=_SECRET_RESOURCE_RE,
                resource_kind="Secrets Manager secret resource",
            )
        for agent in ("monitor", "investigator", "action"):
            _validate_identity(
                getattr(self, f"{agent}_runtime_arn"),
                field_name=f"{agent}_runtime_arn",
                service="bedrock-agentcore",
                resource_re=_RUNTIME_ENDPOINT_RESOURCE_RE,
                resource_kind="AgentCore runtime-endpoint resource "
                "(runtime/<id>/runtime-endpoint/<id>)",
            )
            profile = getattr(self, f"{agent}_model_profile_arn")
            if profile:  # optional; shape-checked only when supplied
                _validate_identity(
                    profile,
                    field_name=f"{agent}_model_profile_arn",
                    service="bedrock",
                    resource_re=_MODEL_PROFILE_RESOURCE_RE,
                    resource_kind="Bedrock application-inference-profile resource",
                )
        return self

    @property
    def agent_runtime_arns(self) -> tuple[str, str, str]:
        """The worker's three AgentCore runtime targets, Monitor / Investigator / Action."""

        return (
            self.monitor_runtime_arn,
            self.investigator_runtime_arn,
            self.action_runtime_arn,
        )


def offline_synth_requested(lookup: object) -> bool:
    """Whether an explicit offline-review synth was selected.

    ``lookup`` is ``App().node.try_get_context``. True when ``-c offline_synth=true`` or the
    ``CHORUS_CDK_OFFLINE_SYNTH`` environment variable is set to a truthy value. Anything else --
    including the absence of both -- means deployment-capable mode (review P2-3).
    """

    get = lookup if callable(lookup) else (lambda _key: None)
    ctx = get("offline_synth")
    if isinstance(ctx, str) and ctx.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(ctx, bool) and ctx:
        return True
    env = os.environ.get("CHORUS_CDK_OFFLINE_SYNTH", "")
    return env.strip().lower() in {"1", "true", "yes", "on"}
