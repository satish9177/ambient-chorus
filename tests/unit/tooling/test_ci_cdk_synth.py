"""CI's static CDK synthesis uses **explicit offline mode**; the default stays deploy-capable.

Review R3. Static synthesis in CI is not a deployment, so it must select offline mode
deliberately -- but ``npm run cdk:synth`` (the deployment-capable path) must remain visible and
must still fail closed without real deploy inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _package_scripts() -> dict[str, str]:
    scripts = json.loads((REPO / "package.json").read_text(encoding="utf-8"))["scripts"]
    return {str(k): str(v) for k, v in scripts.items()}


def test_package_json_has_both_a_deploy_capable_and_an_explicit_offline_synth_script() -> None:
    scripts = _package_scripts()
    assert "cdk:synth" in scripts
    assert "cdk:synth:offline" in scripts
    # the default carries no offline selector -- it represents deployment-capable synthesis
    assert "offline_synth" not in scripts["cdk:synth"]
    assert "CHORUS_CDK_OFFLINE_SYNTH" not in scripts["cdk:synth"]
    # the offline script selects offline mode through the supported CDK context key
    assert "offline_synth=true" in scripts["cdk:synth:offline"]
    assert "infra.cdk.app" in scripts["cdk:synth:offline"]


def test_ci_invokes_the_explicit_offline_synth_and_not_the_default() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "npm run cdk:synth:offline" in ci
    # the bare deployment-capable command must not be what CI runs
    assert "run: npm run cdk:synth\n" not in ci
    assert "run: npm run cdk:synth " not in ci


def test_the_offline_context_key_is_the_one_build_app_honours() -> None:
    from infra.cdk.config import offline_synth_requested

    assert offline_synth_requested({"offline_synth": "true"}.get) is True
    assert offline_synth_requested({}.get) is False


def test_the_default_build_app_still_refuses_without_real_deploy_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infra.cdk.app import build_app
    from infra.cdk.config import MissingDeploymentIdentityError
    from infra.cdk.lambda_support import LambdaArtifactMissingError

    monkeypatch.delenv("CHORUS_CDK_OFFLINE_SYNTH", raising=False)
    with pytest.raises((LambdaArtifactMissingError, MissingDeploymentIdentityError)):
        build_app()


def test_the_explicit_offline_context_makes_build_app_synthesize() -> None:
    from infra.cdk.app import build_app

    assembly = build_app(context={"offline_synth": "true"}).synth()
    assert {s.environment.region for s in assembly.stacks} == {"us-east-1"}
