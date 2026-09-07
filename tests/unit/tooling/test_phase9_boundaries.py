"""What the Phase-9 artifacts are allowed to contain, checked by reading them.

Two independent guarantees, and both are asserted here rather than argued in a document.

**Composition-level.** The inbound entry point constructs no SES port, no Bedrock or AgentCore
client, no compiler client, and no scheduler client, and the AWS root passes
``authenticator=None`` -- so a deployed inbound path has nothing to authenticate with and nothing
to reach beyond persisting an artifact
([ADR-026](../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) § 1, § Consequences).

**Command-level.** No command in the codebase constructs a commitment cancellation, and none
constructs ``ACTIONED -> READY_FOR_ACTION``. Both edges stay legal -- removing a legal edge is a
bigger change than not calling it -- and neither is Phase 9's to take
([ADR-027](../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 5, § 6).

The scans read the import AST and the source text rather than matching substrings blindly: these
modules' docstrings are full of the words ``Bedrock``, ``scheduler``, and ``CANCELLED``,
explaining why each is denied, and a naive substring scan would fail on the explanation rather
than on the code.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOUND_ARTIFACT = REPO_ROOT / "functions" / "inbound_mail"
WATCHER_ARTIFACT = REPO_ROOT / "functions" / "commitment_watcher"
COMMANDS = REPO_ROOT / "src" / "chorus" / "application" / "commands"

FORBIDDEN_INBOUND_MODULES = frozenset(
    {
        "strands",
        "chorus.ports.sender",
        "chorus.infrastructure.ses",
        "chorus.infrastructure.ses.sender",
        "chorus.infrastructure.scheduler",
        "chorus.ports.scheduler",
        "chorus.infrastructure.compiler",
        "chorus.infrastructure.agentcore",
        "runtimes",
    }
)
"""No SES port, no scheduler port or adapter, no compiler client, no agent client.

IAM already denies the worker SES. This is the second, independent guarantee, and it is the one
that catches a composition change before a deployment does.
"""

FORBIDDEN_WATCHER_MODULES = frozenset(
    {
        "strands",
        "chorus.ports.sender",
        "chorus.ports.scheduler",
        "chorus.contracts",
        "chorus.infrastructure.ses",
        "chorus.infrastructure.scheduler",
        "chorus.infrastructure.compiler",
        "chorus.infrastructure.agentcore",
        "chorus.infrastructure.dynamodb.core",
        "chorus.infrastructure.s3",
        "runtimes",
    }
)
"""The watcher's whole boundary as a list of what it cannot reach.

``chorus.infrastructure.dynamodb.core`` is on it because the watcher is denied the Core table
outright: it takes no case edge, and the case row is in Core. ``chorus.ports.scheduler`` is on
it because the watcher is a schedule *target*, never a schedule client -- it cannot create the
schedule that invoked it.
"""

FORBIDDEN_CLIENT_SERVICES = frozenset(
    {"bedrock", "bedrock-runtime", "bedrock-agentcore", "sesv2", "ses", "scheduler", "events"}
)


def _source_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.add(node.module)
    return found


def _client_services(path: Path) -> set[str]:
    """Every service name a ``boto3.client(...)`` call in this file names."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    services: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name != "client":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                services.add(argument.value)
    return services


@pytest.mark.parametrize("path", _source_files(INBOUND_ARTIFACT), ids=lambda p: p.name)
def test_the_inbound_artifact_imports_no_send_model_or_scheduler_module(path: Path) -> None:
    assert not _imported_modules(path) & FORBIDDEN_INBOUND_MODULES


@pytest.mark.parametrize("path", _source_files(WATCHER_ARTIFACT), ids=lambda p: p.name)
def test_the_watcher_artifact_imports_nothing_beyond_its_one_partition(path: Path) -> None:
    assert not _imported_modules(path) & FORBIDDEN_WATCHER_MODULES


@pytest.mark.parametrize(
    "path",
    [*_source_files(INBOUND_ARTIFACT), *_source_files(WATCHER_ARTIFACT)],
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_neither_artifact_constructs_a_model_send_or_scheduler_client(path: Path) -> None:
    assert not _client_services(path) & FORBIDDEN_CLIENT_SERVICES


def test_the_aws_inbound_composition_defaults_its_authenticator_to_none() -> None:
    """Phase 11 owes the only implementation, and the deployed root must not invent one.

    Asserted on the signature rather than on behaviour, because the property is about what a
    composition root *does by default*: a deployed root that had to remember to pass ``None``
    is a root that will one day forget.
    """

    from functions.inbound_mail.composition import build_inbound_mail

    signature = inspect.signature(build_inbound_mail)
    assert signature.parameters["authenticator"].default is None


def test_the_inbound_composition_holds_no_send_agent_compiler_or_scheduler_field() -> None:
    """The absence is the design, and this reads the type to prove it."""

    from functions.inbound_mail.composition import InboundMailComposition

    assert set(InboundMailComposition.__dataclass_fields__) == {
        "attester",
        "verifier",
        "ingest",
        "record_rejection",
    }


def test_the_watcher_settings_name_no_core_table() -> None:
    """A setting naming a table the role is denied is a hint that somebody should try."""

    from functions.commitment_watcher.composition import WatcherSettings

    assert "core_table" not in WatcherSettings.__dataclass_fields__


def test_the_local_inbound_authenticator_refuses_outside_test_and_development() -> None:
    """It is not a stand-in that happens not to be wired: it is one a demo cannot build."""

    from chorus.infrastructure.local.inbound_mail import LocalInboundMailAuthenticator
    from chorus.settings import Environment

    with pytest.raises(ValueError, match="test or development"):
        LocalInboundMailAuthenticator(
            environment=Environment.DEMO, transport="aws:ses-receipt", source_arn="arn:aws:x"
        )


def test_no_command_constructs_a_commitment_cancellation() -> None:
    """``CANCELLED`` stays a legal edge and no code takes it. V1 has no cancellation route.

    A cancelled commitment would leave the case ``VERIFYING`` until a human closed it, and there
    is no endpoint for that -- so the honest state of affairs is a legal edge with no caller.
    """

    offenders = [
        path.name
        for path in _source_files(COMMANDS)
        if "CommitmentStatus.CANCELLED" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_command_constructs_actioned_to_ready_for_action() -> None:
    """ "The response requires another proportionate action" is not Phase 9's edge to take."""

    offenders: list[str] = []
    for path in _source_files(COMMANDS):
        source = path.read_text(encoding="utf-8")
        if "another_action_needed" in source:
            offenders.append(path.name)
    assert offenders == []


def test_no_command_creates_a_commitment_from_an_investigation_proposal() -> None:
    """``InvestigationAssessmentDraft.proposed_commitments`` is retired as an authority source.

    Phase 5 continues to validate its citation and discard it. There is exactly one producer of
    commitments and it is the extraction apply (ADR-027 § 1).
    """

    offenders = [
        path.name
        for path in _source_files(COMMANDS)
        if "proposed_commitments" in path.read_text(encoding="utf-8")
        and path.name != "run_investigation.py"
    ]
    assert offenders == []
