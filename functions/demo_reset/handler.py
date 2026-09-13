"""The dedicated demo reset function's Lambda entry point -- a thin transport boundary.

Review R2. An **operator action**, never a request-path route: no API Gateway integration, no
Function URL, no scheduler target, and no Lambda resource-policy statement admits any service
principal (deployment contract §§ 19, 25). All the reset logic lives in the shared
:class:`~chorus.composition.deployed_demo_reset.DeployedDemoReset`; this module reads the
envelope, gates the environment, calls it, and shapes a safe result.

The order of one invocation
----------------------------
1. **environment gate** -- refuse unless ``CHORUS_ENVIRONMENT=demo`` and ``CHORUS_NAMESPACE=DEMO``;
2. **read the envelope** -- ``demo-reset-request/v1``: ``{namespace, confirm, seed_version?,
   idempotency_key?}``. A wrong namespace or confirmation string is refused before anything is
   constructed;
3. **run** -- ``DeployedDemoReset.reset(...)``: durable replay, ``DEMO_RESET_LOCK``, the
   manifest-driven bounded purge, the generation-fenced clock reseed, the shared seed, and the
   frozen ``DemoResetResult`` receipt;
4. **return safe operational metadata only** (deployment contract § 26) -- the reset id, the
   counts, a ``replayed`` flag, and a duration. No evidence text, no address, no token, no row.

**Cold start touches no network.** The object graph is built lazily on the first invocation.
"""

from __future__ import annotations

import os
import time
from typing import Any, Final

import anyio

from chorus.composition.demo_reset import DemoResetInFlightSend, DemoResetRefused
from chorus.composition.deployed_demo_reset import DeployedDemoReset
from chorus.ports.demo_reset import DemoResetInfrastructureError
from chorus.ports.errors import IdempotencyConflictError
from chorus.settings import Settings
from functions.demo_reset.composition import (
    DEMO_SEED_VERSION,
    DemoResetComposition,
    DemoResetSettings,
    build_demo_reset,
)
from functions.envelope import EnvelopeError, InvocationFailedError, failure, read_envelope

RESET_OPERATION: Final = "demo-reset-request/v1"
ACCEPTED_OPERATIONS: Final = frozenset({RESET_OPERATION})

DEMO_ENVIRONMENT: Final = "demo"
DEMO_NAMESPACE: Final = "DEMO"
RESET_CONFIRMATION: Final = "RESET DEMO"

WRONG_ENVIRONMENT: Final = "RESET_ENVIRONMENT"
WRONG_NAMESPACE: Final = "RESET_NAMESPACE"
WRONG_CONFIRMATION: Final = "RESET_CONFIRMATION"
MALFORMED_EVENT: Final = "MALFORMED_EVENT"
IDEMPOTENCY_CONFLICT: Final = "RESET_IDEMPOTENCY_CONFLICT"
IN_FLIGHT_SEND: Final = "RESET_EXECUTION_IN_FLIGHT"
RESET_INFRASTRUCTURE: Final = "RESET_INFRASTRUCTURE"
"""Raised (not returned) -- a reset stage that could not be proved complete must not be
answered normally, so AWS's own retry machinery sees it."""

_composition: DemoResetComposition | None = None


def demo_reset_settings(settings: Settings) -> DemoResetSettings:
    """Map process configuration onto the reset composition's own settings, and nothing wider."""

    return DemoResetSettings(
        region=settings.aws_region,
        environment=settings.environment.value,
        namespace=settings.namespace,
        core_table=settings.core_table,
        shareable_table=settings.shareable_table,
        audit_table=settings.audit_table,
        private_evidence_bucket=settings.private_evidence_bucket,
        export_evidence_bucket=settings.export_evidence_bucket,
        private_evidence_key_arn=settings.private_evidence_key_arn or "",
        export_evidence_key_arn=settings.export_evidence_key_arn or "",
        scheduler_group=settings.scheduler_group,
        scheduler_environment=settings.scheduler_environment,
        destination_id=settings.destination_id,
        destination_display_label=settings.destination_display_label,
        destination_registry_version=settings.destination_registry_version,
        destination_routing_token=str(settings.destination_routing_token),
        dynamodb_endpoint=(
            str(settings.dynamodb_endpoint) if settings.dynamodb_endpoint is not None else None
        ),
    )


def composition() -> DeployedDemoReset:
    """Build the object graph once per execution environment, on first use."""

    global _composition
    if _composition is None:
        _composition = build_demo_reset(demo_reset_settings(Settings.load()))
    return _composition.reset


async def run(event: object, *, built: DeployedDemoReset | None = None) -> dict[str, Any]:
    """Run one reset request to its outcome. The async body the handler drives."""

    if os.environ.get("CHORUS_ENVIRONMENT") != DEMO_ENVIRONMENT:
        return failure(WRONG_ENVIRONMENT)
    if os.environ.get("CHORUS_NAMESPACE") != DEMO_NAMESPACE:
        return failure(WRONG_NAMESPACE)

    try:
        _, payload = read_envelope(event, accepted=ACCEPTED_OPERATIONS)
    except EnvelopeError:
        return failure(MALFORMED_EVENT)
    if payload.get("namespace") != DEMO_NAMESPACE:
        return failure(WRONG_NAMESPACE)
    if payload.get("confirm") != RESET_CONFIRMATION:
        return failure(WRONG_CONFIRMATION)
    seed_version = payload.get("seed_version") or DEMO_SEED_VERSION
    idempotency_key = payload.get("idempotency_key")
    if not isinstance(seed_version, str) or (
        idempotency_key is not None and not isinstance(idempotency_key, str)
    ):
        return failure(MALFORMED_EVENT)

    deployed = built or composition()
    started = time.monotonic()
    try:
        result = await deployed.reset(
            namespace=DEMO_NAMESPACE,
            confirm=RESET_CONFIRMATION,
            seed_version=seed_version,
            idempotency_key=idempotency_key,
        )
    except DemoResetRefused as error:
        # ``entity_ref`` is the closed reason code the refusal was raised with
        # (``RESET_NAMESPACE`` / ``RESET_SEED_VERSION`` / …); ``str(error)`` would prepend the
        # generic ``VALIDATION_ERROR:`` class tag.
        return failure(str(error.entity_ref))
    except DemoResetInFlightSend:
        return failure(IN_FLIGHT_SEND)
    except IdempotencyConflictError:
        return failure(IDEMPOTENCY_CONFLICT)
    except DemoResetInfrastructureError as error:
        # A stage that could not be proved complete: fail the invocation, do not answer.
        raise InvocationFailedError(RESET_INFRASTRUCTURE) from error

    return {
        "status": "COMPLETED",
        "namespace": DEMO_NAMESPACE,
        "reset_id": str(result.reset_id),
        "replayed": result.replayed,
        "counts": {
            "deleted": result.counts.deleted,
            "messages": result.counts.messages,
            "contributors": result.counts.contributors,
            "evidence": result.counts.evidence,
        },
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def handler(event: object, context: object = None) -> dict[str, Any]:
    """The Lambda entry point. One invocation, one event loop."""

    return anyio.run(run, event)


__all__ = [
    "ACCEPTED_OPERATIONS",
    "IDEMPOTENCY_CONFLICT",
    "IN_FLIGHT_SEND",
    "MALFORMED_EVENT",
    "RESET_CONFIRMATION",
    "RESET_INFRASTRUCTURE",
    "RESET_OPERATION",
    "WRONG_CONFIRMATION",
    "WRONG_ENVIRONMENT",
    "WRONG_NAMESPACE",
    "composition",
    "demo_reset_settings",
    "handler",
    "run",
]
