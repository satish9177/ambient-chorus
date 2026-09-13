"""The deployed sender composition must be constructible where Core is denied.

The sender's role carries an explicit ``Deny dynamodb:*`` against the Core table (ADR-024 SS 3).
The composition root nevertheless built a ``CoreRepository`` and an in-process
``SendAuthorization`` for **both** compositions, which meant the synthesized component could not
perform its own send-time authorization at all -- and nothing caught it, because every test runs
with Core reachable.

These tests are what catches it. They assert the *shape of the object graph*: a deployed
composition reaches the compiler through the one grant its policy names and holds no Core handle
anywhere, and a local composition still holds the in-process authority a test needs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from functions.sender.composition import SenderSettings, build_send_action

from chorus.application.services.send_authorization import SendAuthorization
from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId
from chorus.infrastructure.compiler.send_authorization import CompilerSendAuthorization
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.local.sender import demo_registry
from chorus.ports.records import StoredSafeDestination
from chorus.ports.sender import SesEmailRequest, SesOutcome

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construction builds a DynamoDB client, and a client resolves credentials.

    Placeholder values so the composition is exercised without reaching a credential provider.
    Nothing here calls AWS: the assertions are entirely about which objects were constructed.
    """

    for name, value in (
        ("AWS_ACCESS_KEY_ID", "local"),
        ("AWS_SECRET_ACCESS_KEY", "local"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_EC2_METADATA_DISABLED", "true"),
        ("AWS_CONFIG_FILE", os.devnull),
        ("AWS_SHARED_CREDENTIALS_FILE", os.devnull),
    ):
        monkeypatch.setenv(name, value)


class FrozenClock:
    """The injected clock, and nothing else this composition could read time from."""

    def now(self) -> datetime:
        return datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class RefusingInvoker:
    """A compiler boundary that records what it was asked and answers nothing.

    Construction is the assertion in these tests, so the invoker only has to exist. A call that
    did happen would be a call this composition made before anybody asked it to send.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, *, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, payload))
        raise AssertionError("the composition must not invoke the compiler at construction")


def _destination() -> StoredSafeDestination:
    return StoredSafeDestination(
        destination_id=DestinationId("property_manager:demo"),
        kind=DestinationKind.PROPERTY_MANAGER,
        registry_version=1,
        routing_token=UUID("00000000-0000-0000-0000-000000000000"),
        display_label="Property Management",
    )


def _settings(*, outbox: Path | None, compiler_arn: str | None) -> SenderSettings:
    return SenderSettings(
        region="us-east-1",
        namespace="DEMO",
        core_table="chorus-core",
        shareable_table="chorus-shareable",
        audit_table="chorus-audit",
        destination=_destination(),
        from_identity_id="chorus-demo-sender",
        ses_configuration_set="chorus-test",
        cursor_secret=b"0" * 32,
        compiler_function_arn=compiler_arn,
        outbox_directory=outbox,
    )


def _holds_a_core_repository(root: object, seen: set[int] | None = None) -> bool:
    """Walk the constructed object graph looking for any Core handle at all.

    A structural search rather than a check on one field: the point is that **no** reachable
    object in a deployed sender can read Core, and a check on ``SendAction.authorization`` alone
    would pass for a graph that hid one two hops away.
    """

    seen = seen if seen is not None else set()
    if id(root) in seen:
        return False
    seen.add(id(root))
    if isinstance(root, CoreRepository):
        return True
    slots = getattr(type(root), "__slots__", None)
    names: list[str] = []
    if isinstance(slots, tuple | list):
        names.extend(str(name) for name in slots)
    if hasattr(root, "__dict__"):
        names.extend(vars(root))
    for name in names:
        try:
            value = getattr(root, name)
        except AttributeError:
            continue
        if isinstance(value, str | bytes | int | float | bool | type(None)):
            continue
        if _holds_a_core_repository(value, seen):
            return True
    return False


def test_the_deployed_composition_reaches_the_compiler_and_never_core(tmp_path: Path) -> None:
    """R13. Constructed with Core unreachable, and holding nothing that could reach it."""

    invoker = RefusingInvoker()
    send_action = build_send_action(
        _settings(outbox=None, compiler_arn="arn:aws:lambda:us-east-1:000000000000:function:c"),
        clock=FrozenClock(),
        registry=demo_registry(),
        sender=_NullSender(),
        invoker=invoker,
    )

    assert isinstance(send_action.authorization, CompilerSendAuthorization)
    assert not hasattr(send_action, "core")
    assert not _holds_a_core_repository(send_action)
    assert invoker.calls == []


def test_a_deployed_composition_without_a_compiler_is_refused_at_construction() -> None:
    """A sender that cannot acquire a fence must fail now, not at the first send."""

    with pytest.raises(ValueError, match="compiler function ARN"):
        build_send_action(
            _settings(outbox=None, compiler_arn=None),
            clock=FrozenClock(),
            registry=demo_registry(),
            sender=_NullSender(),
        )


def test_the_local_composition_still_uses_the_in_process_authority(tmp_path: Path) -> None:
    """``test`` and ``development`` have no Lambda boundary, so they keep the direct authority."""

    send_action = build_send_action(
        _settings(outbox=tmp_path / "outbox", compiler_arn=None),
        clock=FrozenClock(),
        registry=demo_registry(),
    )

    assert isinstance(send_action.authorization, SendAuthorization)
    assert _holds_a_core_repository(send_action)


class _NullSender:
    """An email sender that would raise if used. Construction is what is under test."""

    async def send(self, request: SesEmailRequest) -> SesOutcome:
        raise AssertionError("no send happens during composition")
