"""The one deterministic demo message classification, shared by two local callers.

Both :class:`~chorus.infrastructure.local.monitor_agent.LexicalFakeMonitorAgent` -- the keyword
stand-in the demo replays over ``POST /ingest/messages`` -- and
:class:`~chorus.composition.demo_reset.DemoResetService` -- which must pre-place the private
evidence object the resulting case will cite -- have to agree, message for message, on which
corpus messages are lift-equipment signals. Restating that judgement in two places is exactly
how a reordered or edited fixture comes to seed evidence against a case identity the Monitor
never independently derives.

So it is stated once, here, as pure text predicates over a message's own words. Neither caller
runs the other: reset does not invoke the fake agent, and the fake agent does not seed
anything. They share a *classification function*, and the identity reset predicts is derived
from the actual seeded message identifiers a live Monitor run would classify the same way --
never from a positional or hard-coded assumption about which channel ids those are.
"""

from __future__ import annotations

from typing import Final

SIGNAL_TERMS: Final = (
    "lift",
    "elevator",
    "cab",
    "stuck between",
    "stalled",
    "out of service",
)
INSTRUCTION_TERMS: Final = (
    "ignore all previous instructions",
    "ignore previous instructions",
)

# P2-8: the corpus already carries three private details in the same short exchange the
# elevator incident report is drawn from (feed-004/006/007 -- a family member, a health
# reaction, and a unit number), and the frozen mandate/compile contracts already have typed
# facts for exactly this shape (IDENTITY_ATTRIBUTE, HEALTH_DETAIL, UNIT_LOCATION). The lexical
# stand-in never produced them, so the hero demo could never show a real exclusion -- every run
# showed "Included: N, Excluded: 0" instead of the boundary the compile step exists to prove.
# These are literal fixture phrases, tuned to this exact corpus the same way `SIGNAL_TERMS` is,
# not a general classifier.
HEALTH_DETAIL_TERMS: Final = ("asthma",)
IDENTITY_DETAIL_TERMS: Final = ("my mother",)
UNIT_DETAIL_TERMS: Final = ("apartment",)

SensitiveDetailKind = str
"""One of ``"HEALTH_DETAIL"``, ``"IDENTITY_ATTRIBUTE"``, or ``"UNIT_LOCATION"``.

Spelled as the domain's own `FactType` values without importing `FactType` here: this module
is pure text predicates shared by two callers that must not both depend on the domain layer to
stay in agreement (see the module docstring).
"""


def is_policy_like_instruction(text: str) -> bool:
    """True when the stand-in routes ``text`` to ``POLICY_LIKE_INSTRUCTION``.

    Such a message is addressed to a system rather than to neighbours; it produces no report
    and no fact, so no fact ever cites its evidence.
    """

    lowered = text.lower()
    return any(term in lowered for term in INSTRUCTION_TERMS)


def is_signal_message(text: str) -> bool:
    """True when the deterministic stand-in classifies ``text`` as an equipment-failure signal.

    Instruction-like text is never a signal: the stand-in checks that first and routes it
    elsewhere, so this predicate mirrors that ordering exactly.
    """

    if is_policy_like_instruction(text):
        return False
    lowered = text.lower()
    return any(term in lowered for term in SIGNAL_TERMS)


def sensitive_detail_kind(text: str) -> SensitiveDetailKind | None:
    """Which private detail, if any, ``text`` carries -- checked in a fixed order.

    A message with more than one marker only ever happens to have none in this fixture, but
    the order is still fixed (health, then identity, then unit) so the answer cannot depend on
    dict/set iteration order if that ever changes.
    """

    if is_policy_like_instruction(text) or is_signal_message(text):
        return None
    lowered = text.lower()
    if any(term in lowered for term in HEALTH_DETAIL_TERMS):
        return "HEALTH_DETAIL"
    if any(term in lowered for term in IDENTITY_DETAIL_TERMS):
        return "IDENTITY_ATTRIBUTE"
    if any(term in lowered for term in UNIT_DETAIL_TERMS):
        return "UNIT_LOCATION"
    return None


def is_reportable_message(text: str) -> bool:
    """True for anything the stand-in turns into its own report: a signal or a private detail.

    :func:`~chorus.composition.demo_reset.predict_demo_case_id` must derive a report identity
    for exactly the same messages the live Monitor stand-in does, so it calls this rather than
    restating the union of the two predicates itself.
    """

    return is_signal_message(text) or sensitive_detail_kind(text) is not None


__all__ = [
    "HEALTH_DETAIL_TERMS",
    "IDENTITY_DETAIL_TERMS",
    "INSTRUCTION_TERMS",
    "SIGNAL_TERMS",
    "UNIT_DETAIL_TERMS",
    "is_policy_like_instruction",
    "is_reportable_message",
    "is_signal_message",
    "sensitive_detail_kind",
]
