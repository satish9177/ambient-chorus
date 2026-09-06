"""Gated live Action evaluation: does a real model draft a message it can actually justify?

The scenario definitions are complete and the deterministic half of every assertion already
runs, against a scripted agent, in ``tests/contract/action``. What is missing is the deployed
runtime to send them to: the AgentCore resource, its VPC, and the server binding all land in
Phase 11, and ``runtimes/action/runtime.toml`` records that as ``live_evaluation = "NOT_RUN"``.

Two outcomes, and the difference between them is the whole point. Not asking for a live run is
a skip. *Asking* for one is a failure until the runtime exists, because an operator who set the
enable flag has said they want the real model exercised, and answering that with a green skip is
how "we never ran it" gets mistaken for "it passed".

What these will assert when they can run is the *shape* of the answer, never the wording: that
every citation names a fact in its own input, that every field it authored carries one, that
every factual token it wrote appears in a cited safe fact, and that a contradicted fact it
leaned on is caveated. Which phrases the model chose is its business -- that is the whole
reason the grammar is lexical rather than semantic.

The support-precision target is 1.00, and it is met by *rejecting*, not by loosening. A live run
that produced a beautifully written proposal the validator refused would be a pass for this
suite and a prompt problem for the next one.
"""

from __future__ import annotations

import os

import pytest

ENABLE_VARIABLE = "AMBIENT_CHORUS_LIVE_ACTION_EVAL"
"""The gate, deliberately outside the ``CHORUS_`` configuration prefix.

``Settings.load`` refuses to start when it sees an unknown ``CHORUS_`` variable, which is what
keeps a typo in deployment configuration from being ignored. A test-only switch under that
prefix would break the very process this evaluation runs against.
"""

RUNTIME_ARN_VARIABLE = "CHORUS_ACTION_RUNTIME_ARN"
REGION_VARIABLE = "CHORUS_AWS_REGION"

NOT_DEPLOYED = (
    f"{ENABLE_VARIABLE}=1 requests a live Action evaluation, but no Action AgentCore runtime "
    "exists yet: the resource, its isolated VPC, and the server binding land in Phase 11. See "
    'runtimes/action/runtime.toml, which records live_evaluation = "NOT_RUN". Nothing was run; '
    "this is a failure, not a skip."
)

pytestmark = [pytest.mark.anyio, pytest.mark.live_agent]


def _requested() -> None:
    """Skip when nobody asked, and fail loudly when somebody did."""

    if os.environ.get(ENABLE_VARIABLE) != "1":
        pytest.skip(
            f"set {ENABLE_VARIABLE}=1 to request the live Action evaluation; it will fail "
            "until the Phase 11 runtime is deployed"
        )
    if not os.environ.get(RUNTIME_ARN_VARIABLE) or not os.environ.get(REGION_VARIABLE):
        pytest.fail(
            f"{ENABLE_VARIABLE}=1 requests a live Action evaluation, but "
            f"{RUNTIME_ARN_VARIABLE} and {REGION_VARIABLE} are not both set. "
            "Nothing was run; this is a failure, not a skip.",
            pytrace=False,
        )
    pytest.fail(NOT_DEPLOYED, pytrace=False)


async def test_the_live_runtime_answers_with_the_reviewed_prompt_version() -> None:
    """The application refuses any result naming a version other than ``action/v1``."""

    _requested()


async def test_the_live_answer_cites_only_export_facts_it_was_given() -> None:
    """An identifier that was not in the payload rejects the whole proposal."""

    _requested()


async def test_the_live_answer_leaves_no_citation_set_empty() -> None:
    """Every claim, the request, and every caveat -- one to ten citations and never zero."""

    _requested()


async def test_every_factual_token_the_live_model_writes_is_lexically_supported() -> None:
    """The support-precision target of 1.00, measured against a real model's wording.

    This is the scenario the frozen grammar exists for and the one no scripted agent can stand
    in for: a model writing naturally will reach for ``four`` where the fact said ``4``, or for
    ``14 January`` where the fact said ``2030-01-14``, and the prompt's job is to stop it.
    """

    _requested()


async def test_the_live_model_writes_no_quotation_url_address_or_identifier() -> None:
    _requested()


async def test_a_contradicted_fact_the_live_model_relies_on_is_caveated() -> None:
    """The ADR-015 § 7 obligation, discharged by a model that was told about it in the prompt."""

    _requested()


async def test_the_live_model_returns_no_rendered_body_and_no_recipient() -> None:
    """It has no field for either, so this is a check that the runtime enforces the schema."""

    _requested()


async def test_the_live_runtime_registers_no_tool() -> None:
    """Asserted statically today; asserted against the deployed runtime here."""

    _requested()


async def test_the_live_runtime_cannot_reach_any_data_store() -> None:
    """The post-deploy ``AccessDenied`` canary. The static IAM half already passes."""

    _requested()
