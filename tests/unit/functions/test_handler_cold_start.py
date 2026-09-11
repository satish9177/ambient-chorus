"""Every production handler imports with no credentials, no configuration, and no network.

A Lambda's module scope runs during **cold start**, before any request exists. A handler that
read the environment, constructed a client, or -- worst -- performed an operation at import time
would turn a misconfiguration into a failure with no invocation to attach it to, and would make
every handler test depend on an account.

So the property is asserted rather than intended: each module is imported with AWS credential
resolution disabled and every ``CHORUS_*`` variable removed, and importing must simply work.
The composition is built lazily on first use instead, which is where a missing ARN belongs.

``AWS_EC2_METADATA_DISABLED`` and the empty credential variables together mean botocore has
nowhere to look: if any of these modules constructed a client at import, the import would hang
on the instance-metadata endpoint or fail outright, which is exactly the signal wanted.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

HANDLERS = (
    "functions.api.handler",
    "functions.worker.handler",
    "functions.compiler.handler",
    "functions.sender.handler",
    "functions.commitment_watcher.handler",
)

COMPOSITIONS = (
    "functions.api.composition",
    "functions.worker.composition",
    "functions.compiler.composition",
    "functions.sender.composition",
    "functions.commitment_watcher.composition",
)

CREDENTIAL_FREE = {
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ACCESS_KEY_ID": "",
    "AWS_SECRET_ACCESS_KEY": "",
    "AWS_SESSION_TOKEN": "",
    "AWS_PROFILE": "",
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_CONFIG_FILE": os.devnull,
}


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No credentials, no metadata endpoint, and no CHORUS configuration at all."""

    for name, value in CREDENTIAL_FREE.items():
        monkeypatch.setenv(name, value)
    for name in list(os.environ):
        if name.startswith("CHORUS_"):
            monkeypatch.delenv(name, raising=False)
    for module in (*HANDLERS, *COMPOSITIONS):
        monkeypatch.delitem(sys.modules, module, raising=False)
    yield


@pytest.mark.parametrize("module", [*HANDLERS, *COMPOSITIONS])
def test_the_module_imports_with_no_aws_anything(module: str, isolated: None) -> None:
    imported = importlib.import_module(module)
    assert imported.__name__ == module


@pytest.mark.parametrize("module", HANDLERS)
def test_each_handler_exposes_one_entry_point(module: str, isolated: None) -> None:
    imported = importlib.import_module(module)
    assert callable(imported.handler)


@pytest.mark.parametrize("module", HANDLERS)
def test_no_handler_builds_its_object_graph_at_import(module: str, isolated: None) -> None:
    """The cold-start cache starts empty. Building it eagerly would need configuration."""

    imported = importlib.import_module(module)
    cache = "_adapter" if module.endswith("api.handler") else None
    for name in (cache, "_composition", "_send_action"):
        if name is not None and hasattr(imported, name):
            assert getattr(imported, name) is None


@pytest.mark.parametrize("module", HANDLERS)
def test_no_handler_reads_configuration_at_import(module: str, isolated: None) -> None:
    """``Settings.load`` refuses an unknown ``CHORUS_`` variable, so an import-time read would
    make an unrelated environment variable a cold-start failure. Proved by planting one."""

    os.environ["CHORUS_NOT_A_REAL_SETTING"] = "x"
    try:
        assert importlib.import_module(module) is not None
    finally:
        del os.environ["CHORUS_NOT_A_REAL_SETTING"]


def test_no_handler_source_contains_a_windows_path() -> None:
    """The deployed runtime is Linux; a backslash path in a handler is a local-only artifact."""

    root = Path(__file__).resolve().parents[3] / "functions"
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "C:\\" not in source
        assert "\\Users\\" not in source
