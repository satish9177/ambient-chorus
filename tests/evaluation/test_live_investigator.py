"""Gated live Investigator evaluation: does a real model reason skeptically about one case?

The scenario definitions are complete and the deterministic half of every assertion already
runs, against a scripted agent, in ``tests/contract/investigation``. What is missing is the
deployed runtime to send them to: the AgentCore resource, its VPC, and the server binding all
land in Phase 11, and ``runtimes/investigator/runtime.toml`` records that as
``live_evaluation = "NOT_RUN"``.

Two outcomes, and the difference between them is the whole point. Not asking for a live run is
a skip. *Asking* for one is a failure until the runtime exists, because an operator who set the
enable flag has said they want the real model exercised, and answering that with a green skip is
how "we never ran it" gets mistaken for "it passed".

What these will assert when they can run is the *shape* of the reasoning, never the wording:
that every identifier the answer carries was in its own input, that no fact ends ``VERIFIED``,
that a contradiction resolves its cited facts, and that a single-contributor case never reaches
``READY_FOR_ACTION``. Which phrases the model keyed on is its business.
"""

from __future__ import annotations

import os

import pytest

ENABLE_VARIABLE = "AMBIENT_CHORUS_LIVE_INVESTIGATOR_EVAL"
"""The gate, deliberately outside the ``CHORUS_`` configuration prefix.

``Settings.load`` refuses to start when it sees an unknown ``CHORUS_`` variable, which is what
keeps a typo in deployment configuration from being ignored. A test-only switch under that
prefix would break the very process this evaluation runs against.
"""

RUNTIME_ARN_VARIABLE = "CHORUS_INVESTIGATOR_RUNTIME_ARN"
REGION_VARIABLE = "CHORUS_AWS_REGION"

NOT_DEPLOYED = (
    f"{ENABLE_VARIABLE}=1 requests a live Investigator evaluation, but no Investigator "
    "AgentCore runtime exists yet: the resource, its isolated VPC, and the server binding "
    "land in Phase 11. See runtimes/investigator/runtime.toml, which records "
    'live_evaluation = "NOT_RUN". Nothing was run; this is a failure, not a skip.'
)

pytestmark = [pytest.mark.anyio, pytest.mark.live_agent]


def _requested() -> None:
    """Skip when nobody asked, and fail loudly when somebody did."""

    if os.environ.get(ENABLE_VARIABLE) != "1":
        pytest.skip(
            f"set {ENABLE_VARIABLE}=1 to request the live Investigator evaluation; it will "
            "fail until the Phase 11 runtime is deployed"
        )
    if not os.environ.get(RUNTIME_ARN_VARIABLE) or not os.environ.get(REGION_VARIABLE):
        pytest.fail(
            f"{ENABLE_VARIABLE}=1 requests a live Investigator evaluation, but "
            f"{RUNTIME_ARN_VARIABLE} and {REGION_VARIABLE} are not both set. "
            "Nothing was run; this is a failure, not a skip.",
            pytrace=False,
        )
    pytest.fail(NOT_DEPLOYED, pytrace=False)


async def test_the_live_runtime_answers_with_the_reviewed_prompt_version() -> None:
    """The application refuses any result naming a version other than the reviewed one."""

    _requested()


async def test_the_live_runtime_cites_only_identifiers_from_its_own_input() -> None:
    _requested()


async def test_the_live_runtime_never_produces_a_verified_fact() -> None:
    """The metric target is zero, and a non-zero count is a defect rather than a quality miss."""

    _requested()


async def test_a_contradiction_scenario_resolves_its_cited_facts_to_contradicted() -> None:
    """Evaluation scenario 5, end to end against a real model."""

    _requested()


async def test_a_single_contributor_case_never_reaches_ready_for_action() -> None:
    """Evaluation scenario 7, end to end against a real model."""

    _requested()
