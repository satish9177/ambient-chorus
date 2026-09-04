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


def test_the_mandate_transport_scope_literal_matches_the_domain_enum() -> None:
    """The route restates the disclosure vocabulary; this is what stops the two drifting.

    Restating it is deliberate -- it makes an unknown scope a 422 from the transport schema
    rather than a value that reaches a use case -- but a hand-written copy of an enum is a
    copy that goes stale. A scope added to the domain and forgotten here would be silently
    unusable through the API; one removed from the domain and left here would be accepted at
    the boundary and then fail somewhere less obvious.
    """

    from typing import get_args

    from chorus_api.routes.mandates import ScopeLiteral

    from chorus.domain.entities import DisclosureScope

    assert set(get_args(ScopeLiteral)) == {scope.value for scope in DisclosureScope}


def test_the_mandate_decision_literal_matches_the_domain_decision_enum() -> None:
    """The same drift guard for the four decision words."""

    from typing import get_args, get_type_hints

    from chorus_api.routes.mandates import MandateDecisionRequest

    from chorus.domain.mandates import MandateDecision

    declared = get_type_hints(MandateDecisionRequest)["decision"]
    assert set(get_args(declared)) == {decision.value for decision in MandateDecision}
