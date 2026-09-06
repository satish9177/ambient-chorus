"""The validator's own properties: the check order, and what it deliberately does not do.

The integration behaviour is covered against real storage in ``tests/contract/action``. What
lives here is the set of claims about the *module* that a running system cannot demonstrate:
that it consults no repository, loads no assessment, contains no similarity engine, and reads
prose only from the four model-authored fields.

Each of those is an absence, and an absence is only a guarantee while somebody is checking it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from chorus.application.services import action_grounding, action_validation
from chorus.ports.agents import ActionRejection

MODULES = (action_validation, action_grounding)


def _source(module: object) -> str:
    file = getattr(module, "__file__", None)
    assert file is not None
    return Path(file).read_text(encoding="utf-8")


def _code(module: object) -> str:
    """The module's source with every docstring and comment removed.

    Scanning the raw file would make these tests fail on their own explanations -- the modules
    say plainly that they contain no similarity engine, and the word appears in the sentence
    saying so. What is being asserted is a property of the *code*, so the prose is stripped
    first and the assertion means what it says.
    """

    import io
    import tokenize

    text = _source(module)
    tree = ast.parse(text)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Constant):
            continue
        if isinstance(node.value.value, str):
            docstrings.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT or token.start[0] in docstrings:
            continue
        kept.append(token.string)
    return " ".join(kept)


def _imports(module: object) -> set[str]:
    tree = ast.parse(_source(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


# ---------------------------------------------------------------------------------------
# What the validator is not
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", MODULES)
def test_neither_module_reaches_a_repository_or_a_clock(module: object) -> None:
    """Pure over strings and the bound view, and that is what makes it testable at all.

    A validator that could load something would be a validator whose answer depends on when it
    ran. Everything it needs is handed to it: the invocation, the result, and the exact stored
    view the caller strongly read.
    """

    for name in _imports(module):
        assert not name.startswith("chorus.infrastructure"), name
        assert "repositor" not in name.lower(), name
        assert not name.endswith(".clock"), name


@pytest.mark.parametrize("module", MODULES)
def test_neither_module_contains_a_similarity_engine(module: object) -> None:
    """No embeddings, no similarity, no stemming, no plural folding, no second model.

    Named individually rather than as a category, because each was a real option somebody could
    reach for the first time the grammar over-rejects -- and each would make the boundary
    probabilistic, which is the property the deterministic validator exists to remove.
    """

    code = _code(module).lower()
    for forbidden in (
        "embedding",
        "cosine",
        "similarity",
        "levenshtein",
        "difflib",
        "fuzzy",
        "threshold",
    ):
        assert forbidden not in code, forbidden


def test_the_validator_never_loads_an_investigation_assessment() -> None:
    """Contradiction *materiality* is deliberately absent from the Action zone (ADR-021 § 3).

    Phase 5 is the sole authority that ``MEDIUM`` and ``HIGH`` contradictions block readiness,
    so anything reaching a current view is ``LOW`` by construction and Phase 7 does not
    re-derive that judgement from a private record it should not be reading.
    """

    code = _code(action_validation)

    assert "InvestigationAssessment" not in code
    assert "ContradictionMateriality" not in code
    assert "materiality" not in code
    assert "assessment" not in code.lower()


def test_the_grounding_module_adds_no_third_party_dependency() -> None:
    """ADR-021 § 7 says the proper-name grammar is realized without a new regex engine.

    ``unicodedata.category`` expresses the frozen Unicode categories exactly, and
    ``pyproject.toml`` and ``uv.lock`` are untouched by this module.
    """

    imports = _imports(action_grounding)

    assert "unicodedata" in imports
    assert "regex" not in imports
    assert not any(name.startswith("regex") for name in imports)


def test_the_sensitive_term_rule_reuses_the_compiler_pattern() -> None:
    """One denylist, not two. A second one would be a second answer that can disagree."""

    from chorus.privacy.compiler import UNSAFE_VALUE_PATTERN

    # Read through the module namespace rather than by importing the name again, because what
    # is being asserted is that the grounding module *uses the compiler's object* -- not that
    # two modules happen to import something with the same name.
    assert action_grounding.UNSAFE_VALUE_PATTERN is UNSAFE_VALUE_PATTERN


# ---------------------------------------------------------------------------------------
# The refusal vocabulary
# ---------------------------------------------------------------------------------------


def test_every_grounding_code_maps_onto_a_closed_transport_code() -> None:
    """A grounding rejection has to be expressible as an ``ActionRejection`` to be reported."""

    mapping = action_validation._GROUNDING_TO_REJECTION

    assert set(mapping) == set(action_grounding.GroundingRejection)
    assert set(mapping.values()) <= set(ActionRejection)


def test_the_two_named_rules_keep_their_own_transport_codes() -> None:
    """``MAILTO_PATTERN`` and ``PHONE_PATTERN`` are named individually in the frozen set.

    They are the two rules whose absence would be hardest to notice from a generic "rejected
    construct", so they stay legible all the way to the operation record.
    """

    mapping = action_validation._GROUNDING_TO_REJECTION

    assert mapping[action_grounding.GroundingRejection.MAILTO_PATTERN] is (
        ActionRejection.MAILTO_PATTERN
    )
    assert mapping[action_grounding.GroundingRejection.PHONE_PATTERN] is (
        ActionRejection.PHONE_PATTERN
    )


def test_every_rejection_code_is_a_closed_uppercase_token() -> None:
    """So it can be logged, audited, and counted without a redaction rule of its own."""

    for member in ActionRejection:
        assert member.value.isupper()
        assert member.value.replace("_", "").isalnum()
        assert member.value == member.name


def test_the_frozen_minimum_rejection_vocabulary_is_present() -> None:
    """ADR-021's ``at minimum`` list, restated so a removal is a visible change."""

    required = {
        "SCHEMA_INVALID",
        "ENVELOPE_MISMATCH",
        "PROMPT_VERSION_MISMATCH",
        "VIEW_MISMATCH",
        "STALE_VIEW",
        "UNKNOWN_EXPORT_FACT_ID",
        "FOREIGN_IDENTIFIER",
        "EMPTY_CITATION_SET",
        "DUPLICATE_CLAIM_ID",
        "DUPLICATE_NORMALIZED_TEXT",
        "UNSUPPORTED_TOKEN",
        "REJECTED_CONSTRUCT",
        "PHONE_PATTERN",
        "MAILTO_PATTERN",
        "CONTRADICTED_FACT_NOT_CAVEATED",
        "OUTPUT_EXCEEDS_BOUNDS",
    }

    assert required <= {member.value for member in ActionRejection}


# ---------------------------------------------------------------------------------------
# The relied-fact scope
# ---------------------------------------------------------------------------------------


def test_relied_fact_ids_is_claims_union_request_and_excludes_caveats() -> None:
    """The scope decision that makes the caveat obligation terminate.

    A caveat citing a contradicted fact does not itself create a further obligation, because
    the only fixed point of a recursive rule would be an infinite regress or an arbitrary depth
    limit.
    """

    from uuid import uuid4

    from chorus.application.services.action_validation import ValidatedProposal
    from chorus.domain.entities import ActionTone

    claim_fact, request_fact, caveat_fact = uuid4(), uuid4(), uuid4()
    validated = ValidatedProposal(
        subject="Repair request",
        claims=((uuid4(), "A claim.", (claim_fact,)),),
        requested_action="Please repair.",
        requested_deadline=None,
        request_fact_ids=(request_fact,),
        caveats=((uuid4(), "Disputed.", (caveat_fact,)),),
        tone=ActionTone.NEUTRAL,
    )

    assert validated.relied_fact_ids == frozenset({claim_fact, request_fact})
    assert caveat_fact not in validated.relied_fact_ids


def test_the_validator_signature_takes_the_view_rather_than_a_way_to_fetch_one() -> None:
    """The citation-membership check runs against what is *persisted*, not a re-projection."""

    signature = inspect.signature(action_validation.validate_action_result)

    assert set(signature.parameters) == {
        "invocation",
        "result",
        "view",
        "namespace",
        "destination_id",
        "purpose",
        "expected_view_hash",
    }
