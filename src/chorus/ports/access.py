"""The deployed demo access boundary: does this request carry the shared demo token?

This is **not** authentication. It is the frozen hackathon demo access model
([08-api-design.md](../../../docs/architecture/08-api-design.md) SS Transport and authorization
assumptions): one shared bearer token gates the deployed API, and the persona header selects a
seeded actor **after** the token is accepted. What it establishes is that somebody holding the
demo token asserted a persona -- which is what ``ApproverAssurance.DEMO_SHARED_TOKEN`` already
records, and it is stated as such rather than dressed up as identity.

The port answers a boolean and takes a string. It deliberately cannot return the token, the
secret, or anything derived from either: a verifier that could hand its caller the credential
back would be one bug away from a response body that contains it.
"""

from __future__ import annotations

from typing import Protocol


class AccessTokenUnavailableError(Exception):
    """The configured secret is missing, malformed, or unreachable, so the API refuses.

    Fails **closed**. The alternative -- treating an unreadable secret as "no token required"
    -- would turn a Secrets Manager outage into an open API, and a throttled read is not
    distinguishable from a deleted secret at the moment it has to be decided.
    """


class DemoAccessVerifierPort(Protocol):
    """Decide whether one presented bearer token is the deployment's demo token."""

    async def verify(self, presented: str) -> bool:
        """Return whether ``presented`` matches, or raise if the answer cannot be obtained.

        Implementations compare in constant time and never log, echo, or return the presented
        value or the stored one.
        """


__all__ = ["AccessTokenUnavailableError", "DemoAccessVerifierPort"]
