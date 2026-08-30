from __future__ import annotations

import ast

FORBIDDEN_DOMAIN_ROOTS = {"aws_cdk", "boto3", "fastapi", "pydantic", "strands"}


def _forbidden_imports(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots & FORBIDDEN_DOMAIN_ROOTS


def test_forbidden_domain_fixture_import_is_detected() -> None:
    assert _forbidden_imports("from boto3 import client") == {"boto3"}
