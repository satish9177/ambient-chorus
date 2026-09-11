"""Synthesis-only stand-in code for a Lambda whose real artifact has not been built yet.

``lambda.Code.from_asset`` needs a path that exists at synth time, and the real Lambda zips are
produced offline by ``tools/build_lambda_artifacts.py`` into the gitignored ``build/lambda/``.
So an offline ``cdk synth`` and the template tests -- which assert the ``Function`` resource,
its runtime, architecture, handler, role, environment, timeout, and memory, none of which need
the real code -- point at this directory when the built zip is absent.

A real deployment builds the artifacts first; ``infra.cdk.lambda_support.lambda_asset_code``
prefers ``build/lambda/<name>.zip`` whenever it is present and only falls back here. This file
is never invoked: it exists so the directory is non-empty and the asset hash is stable.
"""


def handler(event: object, context: object = None) -> dict[str, object]:  # pragma: no cover
    raise RuntimeError(
        "placeholder Lambda code: run `uv run python -m tools.build_lambda_artifacts` "
        "and redeploy with the real artifact"
    )
