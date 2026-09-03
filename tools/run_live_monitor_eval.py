"""Run the live Monitor evaluation against a deployed runtime, or fail loudly.

This is the manual command that discharges the standing obligation recorded in
``runtimes/monitor/runtime.toml`` under ``live_evaluation``. It exists because a gated test
suite has one dangerous failure mode: an unconfigured environment skips, pytest prints green,
and "we never ran the live evaluation" becomes indistinguishable from "the live evaluation
passed".

So this command never skips. Every missing prerequisite is reported, together, with a non-zero
exit status. It invents nothing: no credential, no region default, no runtime ARN, and no
fallback to the local stand-in. If it cannot run the real thing against the real model, it says
so and fails.

    AMBIENT_CHORUS_LIVE_MONITOR_EVAL=1 \\
    CHORUS_MONITOR_RUNTIME_ARN=arn:aws:bedrock-agentcore:... \\
    CHORUS_AWS_REGION=us-east-1 \\
    uv run python tools/run_live_monitor_eval.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ENABLE_VARIABLE = "AMBIENT_CHORUS_LIVE_MONITOR_EVAL"
RUNTIME_ARN_VARIABLE = "CHORUS_MONITOR_RUNTIME_ARN"
REGION_VARIABLE = "CHORUS_AWS_REGION"

EVALUATION_PATH = "tests/evaluation/test_live_monitor.py"

CREDENTIAL_VARIABLES = ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "AWS_PROFILE")
"""Any one of these is enough to suggest a caller has credentials.

Deliberately a hint rather than a check: the SDK resolves credentials from instance metadata
and SSO caches too, and this command must not refuse a correctly configured operator because
it could not see how they authenticated. What it must not do is *invent* a credential, and it
does not.
"""


def missing_prerequisites() -> list[str]:
    """Every prerequisite that is absent, so an operator fixes them in one pass."""

    problems: list[str] = []
    if os.environ.get(ENABLE_VARIABLE) != "1":
        problems.append(
            f"{ENABLE_VARIABLE} is not set to 1. "
            "The live evaluation spends money and reads real model output, so it is opt-in."
        )
    if not os.environ.get(RUNTIME_ARN_VARIABLE):
        problems.append(f"{RUNTIME_ARN_VARIABLE} is not set to a deployed Monitor runtime ARN.")
    if not os.environ.get(REGION_VARIABLE):
        problems.append(f"{REGION_VARIABLE} is not set to the runtime's AWS region.")
    if not any(os.environ.get(name) for name in CREDENTIAL_VARIABLES):
        problems.append(
            "No AWS credential environment variable is visible ("
            + ", ".join(CREDENTIAL_VARIABLES)
            + "). If you authenticate another way, the invocation will still be attempted; "
            "this command will not invent one."
        )
    return problems


def main() -> int:
    """Run the four live evaluation tests, or explain exactly why they cannot run."""

    root = Path(__file__).resolve().parents[1]
    problems = missing_prerequisites()
    blocking = [
        problem
        for problem in problems
        if not problem.startswith("No AWS credential environment variable")
    ]
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
    if blocking:
        print(
            "\nLive Monitor evaluation NOT RUN. This is a failure, not a skip: "
            "the obligation recorded in runtimes/monitor/runtime.toml is undischarged.",
            file=sys.stderr,
        )
        return 1

    completed = subprocess.run(  # noqa: S603 - fixed argument vector, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            EVALUATION_PATH,
            "-q",
            "--no-header",
            # Without this, a runtime that is unreachable makes every test skip and the command
            # would exit zero having proved nothing at all.
            "-p",
            "no:randomly",
            "-W",
            "error::pytest.PytestUnraisableExceptionWarning",
        ],
        cwd=root,
        check=False,
    )
    if completed.returncode != 0:
        print("\nLive Monitor evaluation FAILED.", file=sys.stderr)
        return completed.returncode
    print("\nLive Monitor evaluation PASSED against the configured runtime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
