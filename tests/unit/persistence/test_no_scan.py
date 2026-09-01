"""Static proof that no production code path can reach a DynamoDB scan.

This is not a style check. A scan crosses namespace and case boundaries in one call, so its
absence is part of the isolation argument. The test reads the shipped source, so a future
adapter cannot quietly acquire one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chorus.infrastructure.dynamodb.client import DynamoDbClient
from chorus.ports.storage import StorageDriver

SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "chorus"
FORBIDDEN_NAMES = frozenset({"scan", "Scan", "parallel_scan", "get_paginator"})


def python_sources() -> tuple[Path, ...]:
    return tuple(sorted(SOURCE_ROOT.rglob("*.py")))


def test_the_source_tree_was_actually_found() -> None:
    assert len(python_sources()) > 20


@pytest.mark.parametrize("path", python_sources(), ids=lambda path: path.name)
def test_no_module_names_a_scan_operation(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_NAMES, f"{path.name} reaches {node.attr}"
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, f"{path.name} names {node.id}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.lower() != "scan", f"{path.name} embeds a scan literal"


def test_the_client_protocol_exposes_only_the_approved_operations() -> None:
    members = {
        name
        for name in dir(DynamoDbClient)
        if not name.startswith("_") and callable(getattr(DynamoDbClient, name, None))
    }

    assert members == {
        "get_item",
        "batch_get_item",
        "query",
        "put_item",
        "delete_item",
        "transact_write_items",
    }


def test_the_storage_port_exposes_no_unbounded_read() -> None:
    members = {
        name
        for name in dir(StorageDriver)
        if not name.startswith("_") and callable(getattr(StorageDriver, name, None))
    }

    assert members == {
        "get_item",
        "batch_get_items",
        "query",
        "write_item",
        "transact_write",
    }
