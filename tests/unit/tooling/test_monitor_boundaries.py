"""Package and artifact boundaries for the Phase 3 additions.

Import-linter enforces the same rules in CI. These tests exist alongside it because they check
things a contract cannot: what the *runtime artifact* would contain, whether a validated answer
can reach storage without passing the validator, and whether an error path could carry private
text into a log.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

import chorus.application.commands.run_monitor as run_monitor
import chorus.infrastructure.local.monitor_agent as local_agent
from chorus.ports.agents import MonitorAgentPort
from chorus.settings import AgentMode, Environment, Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = REPOSITORY_ROOT / "runtimes" / "monitor"

FORBIDDEN_RUNTIME_ROOTS = {
    "boto3",
    "botocore",
    "fastapi",
    "aws_cdk",
}

ALLOWED_RUNTIME_SDK_MODULES = {"botocore.config"}
"""The single AWS-SDK module the Monitor runtime may name, and why.

The runtime has to pin its Bedrock client to one attempt and one read timeout -- otherwise the
SDK retries a throttle up to six times underneath the single retry the application believes it
owns, spending five extra passes over private community text. Strands takes that setting only
as a ``botocore.config.Config``, so the type has to be importable here.

``botocore.config`` is a configuration value object: it constructs no client, holds no
credential, and reaches no service. Everything else under ``boto3`` and ``botocore`` stays
forbidden, which is what the assertion below actually checks -- the exception is one module,
not one package.
"""
FORBIDDEN_RUNTIME_MODULES = {
    "chorus.application",
    "chorus.infrastructure",
    "chorus.ports",
    "chorus.privacy",
    "chorus_api",
}


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _runtime_sources() -> list[Path]:
    return sorted(RUNTIME_ROOT.rglob("*.py"))


def test_the_runtime_package_has_source_files_to_scan() -> None:
    assert _runtime_sources()


@pytest.mark.parametrize("path", _runtime_sources(), ids=lambda path: path.name)
def test_the_monitor_runtime_imports_no_sdk_or_application_package(path: Path) -> None:
    """The artifact scan the deployment build runs, applied to the source it would ship."""

    modules = _imported_modules(path.read_text(encoding="utf-8")) - ALLOWED_RUNTIME_SDK_MODULES

    roots = {module.split(".", maxsplit=1)[0] for module in modules}
    assert not roots & FORBIDDEN_RUNTIME_ROOTS
    for forbidden in FORBIDDEN_RUNTIME_MODULES:
        assert not any(module.startswith(forbidden) for module in modules)


@pytest.mark.parametrize("path", _runtime_sources(), ids=lambda path: path.name)
def test_the_monitor_runtime_reaches_no_aws_service_client(path: Path) -> None:
    """The narrow SDK exception is a config type, not a door to the rest of the SDK."""

    modules = _imported_modules(path.read_text(encoding="utf-8"))

    sdk_modules = {
        module for module in modules if module.split(".", maxsplit=1)[0] in {"boto3", "botocore"}
    }
    assert sdk_modules <= ALLOWED_RUNTIME_SDK_MODULES


def test_the_monitor_runtime_imports_only_its_own_contract_from_chorus() -> None:
    modules: set[str] = set()
    for path in _runtime_sources():
        modules |= _imported_modules(path.read_text(encoding="utf-8"))

    chorus_modules = {module for module in modules if module.startswith("chorus")}
    assert chorus_modules <= {"chorus.contracts.common", "chorus.contracts.monitor"}


def test_the_contracts_package_re_exports_nothing() -> None:
    """A single import of ``chorus.contracts`` must not pull a private contract along.

    Submodule attributes appear once something else has imported them, which is unavoidable
    and harmless. What must never appear is a *model* re-exported from the package root: that
    is what would let the Action artifact reach a private contract through one import.
    """

    from pydantic import BaseModel

    import chorus.contracts as contracts

    source = (REPOSITORY_ROOT / "src" / "chorus" / "contracts" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert _imported_modules(source) == set()
    assert "__all__" not in source
    exported_models = {
        name
        for name, value in vars(contracts).items()
        if isinstance(value, type) and issubclass(value, BaseModel)
    }
    assert exported_models == set()


def test_the_monitor_use_case_depends_on_the_port_and_not_on_an_adapter() -> None:
    signature = inspect.get_annotations(run_monitor.RunMonitor.__init__, eval_str=False)

    assert signature["agent"] == "MonitorAgentPort"
    source = inspect.getsource(run_monitor)
    assert "AgentCoreMonitorAgent" not in source
    assert "boto3" not in source


def test_both_local_adapters_satisfy_the_monitor_port() -> None:
    scripted: MonitorAgentPort = local_agent.ScriptedMonitorAgent(
        responder=lambda invocation: local_agent.build_lexical_output(invocation)
    )
    lexical: MonitorAgentPort = local_agent.LexicalFakeMonitorAgent()

    assert callable(scripted.invoke_monitor)
    assert callable(lexical.invoke_monitor)


def test_the_agent_is_never_handed_a_repository_or_a_unit_of_work() -> None:
    """The port's only method takes an envelope, so there is nothing else to hand it."""

    signature = inspect.signature(MonitorAgentPort.invoke_monitor)

    assert list(signature.parameters) == ["self", "invocation"]


def test_the_lexical_fake_cannot_be_the_demo_agent() -> None:
    """``fake`` mode is developer-only; the deployed demo refuses to start with it."""

    with pytest.raises(ValueError, match="agentcore"):
        Settings(
            environment=Environment.DEMO,
            namespace="DEMO",
            agent_mode=AgentMode.FAKE,
        )


DYNAMODB_SCAN = re.compile(r"(?<![A-Za-z])Scan")
"""The DynamoDB scan operation, wherever it is spelled.

The lookbehind is the whole of the change from an earlier bare ``"Scan" not in text``. That
substring also matches ``MalwareScanStatus`` and ``malware_scan_status`` -- a domain enum about
whether storage accepted some bytes, which has nothing to do with a table read -- so the bare
check made a legitimate domain name look like a forbidden access pattern.

The lookbehind keeps every spelling that *is* one: a bare ``Scan``, ``ScanRequest``,
``dynamodb:Scan`` in a policy string, and ``"Scan"`` in an SDK call. It admits only an
identifier that ends in ``...Scan...`` preceded by another letter, which no scan API does.
"""


def test_no_application_module_performs_a_scan() -> None:
    """The frozen access patterns forbid a scan, and none of the new code has one."""

    sources = [
        REPOSITORY_ROOT / "src" / "chorus" / "application",
        REPOSITORY_ROOT / "apps" / "api" / "chorus_api",
        REPOSITORY_ROOT / "runtimes",
    ]
    for root in sources:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert ".scan(" not in text, path
            assert DYNAMODB_SCAN.search(text) is None, path


def test_the_validator_is_the_only_route_from_an_answer_to_the_planner() -> None:
    """``plan_monitor_application`` accepts a validated answer, never a raw one."""

    from chorus.application.services.monitor_apply import plan_monitor_application

    annotations = inspect.get_annotations(plan_monitor_application, eval_str=False)
    assert annotations["validated"] == "ValidatedMonitorOutput"

    # The planner module never imports the raw contract, so there is no type it could accept
    # an unvalidated answer as.
    from chorus.application.services import monitor_apply

    imported = _imported_modules(inspect.getsource(monitor_apply))
    assert "chorus.contracts.monitor" not in imported
