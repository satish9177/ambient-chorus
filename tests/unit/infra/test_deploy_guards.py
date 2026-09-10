"""The two explicit synthesis modes, and what each refuses (review P2-3, P2-4).

Deployment-capable mode -- the default for ``build_app()`` and ``python infra/cdk/app.py`` --
refuses a missing Lambda artifact and a synthetic / malformed / account-zero deployment
identity *before* synth. Offline-review mode is a deliberate choice (``build_app(offline=True)``
or ``-c offline_synth=true``) and uses the clearly-named placeholder fixtures.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from aws_cdk import aws_lambda as lambda_
from infra.cdk import lambda_support
from infra.cdk.app import build_app
from infra.cdk.config import (
    DeploymentIdentities,
    MissingDeploymentIdentityError,
    offline_synth_requested,
)
from infra.cdk.lambda_support import (
    OFFLINE_PLACEHOLDER_CODE_DIR,
    LambdaArtifactMissingError,
    lambda_asset_code,
)

_REAL_CONTEXT = {
    "environment": "demo",
    "namespace": "DEMO",
    "account": "111122223333",
    "demo_access_secret_arn": (
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:chorus-demo-access-AbCdEf"
    ),
    "cursor_signing_secret_arn": (
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:chorus-cursor-signing-GhIjKl"
    ),
    "destination_registry_secret_arn": (
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:chorus-dest-registry-MnOpQr"
    ),
    "monitor_runtime_arn": (
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_monitor-a/runtime-endpoint/live"
    ),
    "investigator_runtime_arn": (
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_investigator-b/runtime-endpoint/live"
    ),
    "action_runtime_arn": (
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_action-c/runtime-endpoint/live"
    ),
}


# -- P2-4: identity validation --------------------------------------------------------------


def _identities(**overrides: str) -> DeploymentIdentities:
    base = {k: v for k, v in _REAL_CONTEXT.items() if k.endswith("_arn")}
    base.update(overrides)
    return DeploymentIdentities.from_context("demo", base.get, offline=False)


def test_real_arns_pass_deployment_mode_validation() -> None:
    identities = _identities()
    assert identities.demo_access_secret_arn.startswith("arn:aws:secretsmanager:us-east-1:")
    assert not identities.offline


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("demo_access_secret_arn", "", "missing"),
        ("demo_access_secret_arn", "not-an-arn", "malformed"),
        (
            "demo_access_secret_arn",
            "arn:aws:s3:us-east-1:111122223333:secret:x",
            "wrong service",
        ),
        (
            "demo_access_secret_arn",
            "arn:aws:secretsmanager:eu-west-1:111122223333:secret:x",
            "wrong region",
        ),
        (
            "demo_access_secret_arn",
            "arn:aws:secretsmanager:us-east-1:000000000000:secret:x",
            "account zero",
        ),
        (
            "demo_access_secret_arn",
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:x-PLACEHOLDER",
            "placeholder marker",
        ),
        (
            "monitor_runtime_arn",
            "arn:aws:lambda:us-east-1:111122223333:function:x",
            "runtime wrong service",
        ),
        ("action_runtime_arn", "", "runtime missing"),
        # R1 -- the resource portion, not just the prefix
        (
            "demo_access_secret_arn",
            "arn:aws:secretsmanager:us-east-1:111122223333:not-a-secret",
            "secret: resource not a secret",
        ),
        (
            "cursor_signing_secret_arn",
            "arn:aws:secretsmanager:us-east-1:111122223333:parameter/foo",
            "secret: wrong resource type",
        ),
        (
            "destination_registry_secret_arn",
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:",
            "secret: empty resource id",
        ),
        (
            "monitor_runtime_arn",
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_monitor-a",
            "bare runtime with no endpoint",
        ),
        (
            "investigator_runtime_arn",
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:not-a-runtime",
            "not-a-runtime",
        ),
        (
            "action_runtime_arn",
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/x/wrong-child/foo",
            "wrong child resource",
        ),
        (
            "monitor_runtime_arn",
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime//runtime-endpoint/live",
            "missing runtime component",
        ),
        (
            "monitor_runtime_arn",
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/x/runtime-endpoint/",
            "missing endpoint component",
        ),
        (
            "monitor_model_profile_arn",
            "arn:aws:bedrock:us-east-1:111122223333:foundation-model/x",
            "model profile wrong resource shape",
        ),
    ],
)
def test_deployment_mode_rejects_a_bad_identity(field: str, value: str, why: str) -> None:
    with pytest.raises(MissingDeploymentIdentityError):
        _identities(**{field: value})


def test_deployment_mode_accepts_a_real_secrets_manager_suffix() -> None:
    """AWS appends a 6-char suffix to a secret name; the real ARN shape must pass."""

    identities = _identities(
        demo_access_secret_arn=(
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:prod/chorus/demo-access-a1B2c3"
        )
    )
    assert identities.demo_access_secret_arn.endswith("demo-access-a1B2c3")


def test_deployment_mode_accepts_a_real_runtime_endpoint_arn() -> None:
    identities = _identities(
        monitor_runtime_arn=(
            "arn:aws:bedrock-agentcore:us-east-1:111122223333:"
            "runtime/chorus_monitor-9Zx8yW7v6u/runtime-endpoint/live"
        )
    )
    assert "/runtime-endpoint/live" in identities.monitor_runtime_arn


def test_a_missing_model_profile_arn_is_allowed_but_a_malformed_one_is_not() -> None:
    """Model-profile ARNs are optional -- no batch-5 component consumes one -- but a supplied
    one is still shape-checked."""

    _identities()  # none supplied -> fine
    with pytest.raises(MissingDeploymentIdentityError):
        _identities(monitor_model_profile_arn="arn:aws:s3:us-east-1:111122223333:x")


def test_offline_mode_fills_clearly_typed_synthetic_fixtures() -> None:
    identities = DeploymentIdentities.from_context("demo", lambda _k: None, offline=True)
    assert identities.offline
    assert "PLACEHOLDER" in identities.demo_access_secret_arn
    assert "000000000000" in identities.monitor_runtime_arn
    # and deployment-mode validation would reject exactly those fixtures
    with pytest.raises(MissingDeploymentIdentityError):
        DeploymentIdentities.from_context("demo", lambda _k: None, offline=False)


# -- P2-3: placeholder Lambda code is not deployable --------------------------------------


def test_lambda_asset_code_refuses_a_missing_artifact_in_deployment_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lambda_support, "LAMBDA_BUILD_OUTPUT_ROOT", tmp_path)
    with pytest.raises(LambdaArtifactMissingError):
        lambda_asset_code("chorus-api", offline=False)


def test_the_reset_lambda_fails_closed_without_its_built_zip_in_deployment_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review completion B4: the Reset Lambda is Macro A compute and gets the same
    deployment-capable artifact guarantee as the other five -- a missing
    ``build/lambda/chorus-demo-reset.zip`` refuses synth, with no silent placeholder."""

    monkeypatch.setattr(lambda_support, "LAMBDA_BUILD_OUTPUT_ROOT", tmp_path)
    with pytest.raises(LambdaArtifactMissingError):
        lambda_asset_code("chorus-demo-reset", offline=False)
    # explicit offline mode is still allowed to use the placeholder
    assert isinstance(lambda_asset_code("chorus-demo-reset", offline=True), lambda_.Code)


def test_lambda_asset_code_uses_the_placeholder_only_in_offline_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lambda_support, "LAMBDA_BUILD_OUTPUT_ROOT", tmp_path)
    code = lambda_asset_code("chorus-api", offline=True)
    assert isinstance(code, lambda_.Code)
    # the placeholder directory is named so it cannot be mistaken for deploy-ready output
    assert OFFLINE_PLACEHOLDER_CODE_DIR.name == "_lambda_placeholder"
    assert (OFFLINE_PLACEHOLDER_CODE_DIR / "placeholder_handler.py").is_file()


def test_a_real_zip_and_the_placeholder_produce_different_cdk_asset_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aws_cdk import App, Stack
    from aws_cdk import aws_lambda as lam

    monkeypatch.setattr(lambda_support, "LAMBDA_BUILD_OUTPUT_ROOT", tmp_path)

    def _asset_hash(code: lam.Code) -> str:
        app = App()
        stack = Stack(app, "S")
        fn = lam.Function(
            stack,
            "F",
            runtime=lam.Runtime.PYTHON_3_12,
            handler="functions.api.handler.handler",
            code=code,
        )
        _ = fn
        template = app.synth().get_stack_by_name("S").template
        function = next(
            r for r in template["Resources"].values() if r["Type"] == "AWS::Lambda::Function"
        )
        return str(function["Properties"]["Code"]["S3Key"])

    placeholder_key = _asset_hash(lambda_asset_code("chorus-api", offline=True))
    (tmp_path / "chorus-api.zip").write_bytes(_minimal_zip())
    real_key = _asset_hash(lambda_asset_code("chorus-api", offline=False))
    assert placeholder_key != real_key


def _minimal_zip() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("functions/api/handler.py", "def handler(e, c=None): return {}\n")
    return buffer.getvalue()


# -- P2-3: the app path is deployment-capable by default --------------------------------


def test_the_default_app_path_refuses_without_a_build_or_real_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHORUS_CDK_OFFLINE_SYNTH", raising=False)
    with pytest.raises((LambdaArtifactMissingError, MissingDeploymentIdentityError)):
        build_app()


def test_offline_synth_is_selected_only_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHORUS_CDK_OFFLINE_SYNTH", raising=False)
    assert offline_synth_requested(lambda _k: None) is False
    assert offline_synth_requested({"offline_synth": "true"}.get) is True
    monkeypatch.setenv("CHORUS_CDK_OFFLINE_SYNTH", "1")
    assert offline_synth_requested(lambda _k: None) is True


def test_offline_build_app_synthesizes_and_pins_the_region() -> None:
    assembly = build_app(offline=True).synth()
    regions = {stack.environment.region for stack in assembly.stacks}
    assert regions == {"us-east-1"}
    assert not any("unknown-region" in stack.environment.region for stack in assembly.stacks)
    # every function still carries an environment (SS 44) and the placeholder code
    for stack in assembly.stacks:
        for resource in stack.template.get("Resources", {}).values():
            if resource["Type"] == "AWS::Lambda::Function":
                name = resource["Properties"]["FunctionName"]
                assert re.match(r"^chorus-[a-z-]+-development$", name)
