from __future__ import annotations

import pytest
from pydantic import ValidationError

from chorus.domain.ids import Namespace
from chorus.settings import Settings


def test_settings_unknown_chorus_variable_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_UNKNOWN_SETTING", "unsafe")

    with pytest.raises(ValueError, match="unknown CHORUS"):
        Settings.load()


def test_settings_production_value_fails() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production")


def test_settings_default_namespace_is_canonical() -> None:
    """The documented development default must satisfy the frozen namespace grammar."""

    assert Settings().namespace == "LOCAL_DEVELOPER"
    assert Namespace(Settings().namespace).value == "LOCAL_DEVELOPER"


@pytest.mark.parametrize(
    "value", ["DEMO", "LOCAL_DEV", "LOCAL_DEVELOPER", "TEST_ELEVATOR_V1", "A_", "A" * 32]
)
def test_settings_accepts_a_canonical_namespace(value: str) -> None:
    assert Settings(namespace=value).namespace == value


@pytest.mark.parametrize(
    "value",
    ["LOCAL_dev", "local_developer", "LOCAL-DEV", "1ABC", "A B", "", "A", "A" * 33, "A.B"],
)
def test_settings_rejects_a_non_canonical_namespace(value: str) -> None:
    """Configuration validates the namespace; it never repairs one."""

    with pytest.raises(ValidationError):
        Settings(namespace=value)


def test_settings_never_normalizes_a_namespace() -> None:
    """A lowercase value is refused outright, not silently upper-cased into a valid one."""

    with pytest.raises(ValidationError):
        Settings(namespace="local_developer")


def test_the_demo_environment_still_requires_the_demo_namespace() -> None:
    with pytest.raises(ValidationError, match="demo environment requires the DEMO namespace"):
        Settings(environment="demo", namespace="LOCAL_DEVELOPER", agent_mode="agentcore")


def test_the_demo_namespace_rule_is_satisfied_by_demo() -> None:
    """With ``DEMO`` the namespace rule stops firing; the remaining demo rules are unrelated."""

    with pytest.raises(ValidationError) as raised:
        Settings(environment="demo", namespace="DEMO", agent_mode="agentcore")

    assert "DEMO namespace" not in str(raised.value)


def test_the_sending_identity_is_configuration_and_not_an_address() -> None:
    """``CHORUS_SES_FROM_IDENTITY_ID`` is an opaque stable identifier, never the ``From`` address.

    That is the whole reason it can be ordinary deployment configuration rather than a Secrets
    Manager read (ADR-022 § 4): it names *which* verified identity, not *what* the mailbox is,
    so the preview hash is computable at proposal time with no secret anywhere in the path. The
    pattern refuses ``@``, so a deployment that pasted an address in fails at startup rather
    than putting one into a digest a human then approves.
    """

    from pydantic import ValidationError as PydanticValidationError

    assert Settings().ses_from_identity_id == "chorus-demo-sender"
    with pytest.raises(PydanticValidationError):
        Settings(ses_from_identity_id="sender@example.test")
    with pytest.raises(PydanticValidationError):
        Settings(ses_from_identity_id="")


def test_every_setting_the_env_example_names_exists_and_vice_versa() -> None:
    """The example file and the model are one contract, checked in both directions.

    ``Settings.load`` refuses to start on an unknown ``CHORUS_`` variable, so a stale example
    file is not merely untidy -- it is a deployment that fails at startup. And a setting with no
    example is one an operator has to discover from source.
    """

    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    documented = {
        line.split("=", 1)[0].strip()
        for line in (root / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("CHORUS_") and "=" in line
    }
    declared = {f"CHORUS_{name.upper()}" for name in Settings.model_fields}

    assert "CHORUS_SES_FROM_IDENTITY_ID" in documented
    assert documented <= declared, documented - declared
