"""Each function's settings mapper validates exactly what *that* composition consumes.

Review P2-8: the global ``Settings.validate_environment_contract`` no longer forces the six
AgentCore ARNs on every ``demo`` process. Instead:

* ``worker_settings`` requires the three AgentCore **runtime endpoint** ARNs (and the compiler /
  sender / watcher / scheduler ARNs), and no model-profile ARN;
* ``api_settings`` requires the demo-access / cursor-signing secret ARNs and the worker /
  compiler / watcher ARNs, and **nothing AgentCore**;
* ``compiler_settings`` / ``sender_settings`` / ``watcher_settings`` require only their own
  storage / evidence / clock / SES / fence configuration, and **nothing AgentCore**.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from functions.api.composition import api_settings
from functions.commitment_watcher.handler import watcher_settings
from functions.compiler.handler import compiler_settings
from functions.sender.handler import sender_settings
from functions.worker.handler import worker_settings

from chorus.settings import Settings

_FN = "arn:aws:lambda:us-east-1:111122223333:function:{}"
_SECRET = "arn:aws:secretsmanager:us-east-1:111122223333:secret:{}"
_RUNTIME = (
    "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_{}/runtime-endpoint/live"
)
_KMS = "arn:aws:kms:us-east-1:111122223333:key/{}"
_ROLE = "arn:aws:iam::111122223333:role/{}"

_BASE = {
    "CHORUS_ENVIRONMENT": "demo",
    "CHORUS_NAMESPACE": "DEMO",
    "CHORUS_AWS_REGION": "us-east-1",
    "CHORUS_AGENT_MODE": "agentcore",
    "CHORUS_SHAREABLE_TABLE": "chorus-shareable-demo",
    "CHORUS_AUDIT_TABLE": "chorus-audit-demo",
    "CHORUS_DEMO_CLOCK_ENABLED": "true",
}

_API = {
    **_BASE,
    "CHORUS_CORE_TABLE": "chorus-core-demo",
    "CHORUS_DEMO_ACCESS_SECRET_ARN": _SECRET.format("demo-access"),
    "CHORUS_CURSOR_SIGNING_SECRET_ARN": _SECRET.format("cursor-signing"),
    "CHORUS_WORKER_FUNCTION_ARN": _FN.format("chorus-worker-demo"),
    "CHORUS_COMPILER_FUNCTION_ARN": _FN.format("chorus-compiler-demo"),
    "CHORUS_WATCHER_FUNCTION_ARN": _FN.format("chorus-commitment-watcher-demo") + ":live",
}
_WORKER = {
    **_BASE,
    "CHORUS_CORE_TABLE": "chorus-core-demo",
    "CHORUS_MONITOR_RUNTIME_ARN": _RUNTIME.format("monitor-a"),
    "CHORUS_INVESTIGATOR_RUNTIME_ARN": _RUNTIME.format("investigator-b"),
    "CHORUS_ACTION_RUNTIME_ARN": _RUNTIME.format("action-c"),
    "CHORUS_COMPILER_FUNCTION_ARN": _FN.format("chorus-compiler-demo"),
    "CHORUS_SENDER_FUNCTION_ARN": _FN.format("chorus-sender-demo"),
    "CHORUS_WATCHER_FUNCTION_ARN": _FN.format("chorus-commitment-watcher-demo") + ":live",
    "CHORUS_SCHEDULER_GROUP": "chorus-demo",
    "CHORUS_SCHEDULER_ENVIRONMENT": "demo",
    "CHORUS_SCHEDULER_ROLE_ARN": _ROLE.format("chorus-scheduler-demo"),
    "CHORUS_SES_CONFIGURATION_SET": "chorus-demo",
}
_COMPILER = {
    **_BASE,
    "CHORUS_CORE_TABLE": "chorus-core-demo",
    "CHORUS_PRIVATE_EVIDENCE_BUCKET": "chorus-private-evidence-demo",
    "CHORUS_EXPORT_EVIDENCE_BUCKET": "chorus-export-evidence-demo",
    "CHORUS_PRIVATE_EVIDENCE_KEY_ARN": _KMS.format("private"),
    "CHORUS_EXPORT_EVIDENCE_KEY_ARN": _KMS.format("export"),
}
_SENDER = {
    **_BASE,
    "CHORUS_CORE_TABLE": "chorus-core-demo",
    "CHORUS_SES_CONFIGURATION_SET": "chorus-demo",
    "CHORUS_DESTINATION_REGISTRY_SECRET_ARN": _SECRET.format("dest-registry"),
    "CHORUS_COMPILER_FUNCTION_ARN": _FN.format("chorus-compiler-demo"),
}
_WATCHER = dict(_BASE)


@pytest.fixture
def clean_env() -> Iterator[None]:
    import os

    saved = {k: v for k, v in os.environ.items() if k.startswith(("CHORUS_", "AWS_"))}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith(("CHORUS_", "AWS_"))]:
            del os.environ[key]
        os.environ.update(saved)


def _load(env: dict[str, str]) -> Settings:
    import os

    os.environ.update(env)
    return Settings.load()


# -- each mapper constructs from its own complete environment --------------------------


def test_api_settings_needs_no_agentcore_arns(clean_env: None) -> None:
    mapped = api_settings(_load(_API))
    assert mapped.compiler_function_arn.endswith("chorus-compiler-demo")
    assert mapped.watcher_function_arn.endswith(":live")


def test_worker_settings_requires_the_three_runtime_arns_and_no_model_profile(
    clean_env: None,
) -> None:
    mapped = worker_settings(_load(_WORKER))
    assert mapped.monitor_runtime_arn.startswith("arn:aws:bedrock-agentcore:")
    assert mapped.investigator_runtime_arn and mapped.action_runtime_arn
    # WorkerSettings has no model-profile field at all
    assert not hasattr(mapped, "monitor_model_profile_arn")


def test_compiler_sender_watcher_settings_construct_without_any_agentcore_arn(
    clean_env: None,
) -> None:
    assert compiler_settings(_load(_COMPILER)).private_evidence_key_arn
    assert sender_settings(_load(_SENDER)).compiler_function_arn
    assert watcher_settings(_load(_WATCHER)).shareable_table == "chorus-shareable-demo"


# -- negatives: a missing OWN field fails; a missing irrelevant AgentCore field does not --


def test_api_settings_fails_on_a_missing_own_required_field(clean_env: None) -> None:
    env = {k: v for k, v in _API.items() if k != "CHORUS_CURSOR_SIGNING_SECRET_ARN"}
    with pytest.raises(ValueError, match="cursor signing secret ARN"):
        api_settings(_load(env))


def test_api_settings_still_constructs_with_no_agentcore_runtime_arns(clean_env: None) -> None:
    # _API deliberately carries none; this passes only because the coupling is gone (P2-8)
    assert api_settings(_load(_API)) is not None


def test_worker_settings_fails_on_a_missing_runtime_target(clean_env: None) -> None:
    env = {k: v for k, v in _WORKER.items() if k != "CHORUS_INVESTIGATOR_RUNTIME_ARN"}
    with pytest.raises(ValueError, match="Investigator runtime ARN"):
        worker_settings(_load(env))


def test_worker_settings_does_not_require_a_model_profile_arn(clean_env: None) -> None:
    # _WORKER carries no model-profile var; this constructs, proving the profile is not required
    assert worker_settings(_load(_WORKER)) is not None


def test_compiler_settings_fails_without_its_evidence_key_arns(clean_env: None) -> None:
    from functions.compiler.composition import build_compile_view

    from chorus.domain.time import SystemClock

    env = {k: v for k, v in _COMPILER.items() if k != "CHORUS_PRIVATE_EVIDENCE_KEY_ARN"}
    settings = compiler_settings(_load(env))
    assert settings.private_evidence_key_arn is None
    # a stub driver so the check is reached without constructing a boto3 client
    with pytest.raises(ValueError, match="evidence KMS key ARNs"):
        build_compile_view(settings, clock=SystemClock(), driver=object())  # type: ignore[arg-type]


def test_sender_settings_fails_without_the_destination_registry_secret(clean_env: None) -> None:
    from functions.sender.handler import build_registry

    env = {k: v for k, v in _SENDER.items() if k != "CHORUS_DESTINATION_REGISTRY_SECRET_ARN"}
    _load(env)
    with pytest.raises(ValueError, match="destination registry secret ARN"):
        build_registry(Settings.load())
