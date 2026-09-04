"""Closed application-layer failures that are neither domain invariants nor storage outcomes.

Three failures live between the two existing taxonomies. A deterministic policy denial is not a
malformed request -- the request was well formed and the answer is no. A stale authorization
snapshot is not a storage conflict -- storage is fine and the caller's view of authority is
out of date. A live send fence is neither -- the command is valid, the state is valid, and it is
simply not this command's turn.

Each carries a safe code and bounded reason codes and nothing else. A denial says which
deterministic rule refused it, never which fact, which scope was asked for, or what the value
was. That is what makes it safe to return in an HTTP body, write to a log, and put in an audit
row without three different redaction rules.
"""

from __future__ import annotations

from enum import StrEnum


class ApplicationErrorCode(StrEnum):
    """Safe codes the API maps onto the frozen error table."""

    POLICY_DENIED = "POLICY_DENIED"
    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    SEND_AUTHORIZATION_IN_PROGRESS = "SEND_AUTHORIZATION_IN_PROGRESS"


class ApplicationError(Exception):
    """Base application failure carrying only a safe code and bounded reason codes.

    A plain exception rather than a frozen dataclass, for the same reason the domain and
    persistence errors are: parts of the Python exception protocol assign to ``__traceback__``,
    which a frozen dataclass refuses, and an error that cannot be re-raised is an error that
    gets replaced by an unrelated failure at the point it mattered most.
    """

    __slots__ = ("code", "reason_codes", "retryable")

    code: ApplicationErrorCode
    reason_codes: tuple[str, ...]
    retryable: bool

    def __init__(
        self,
        code: ApplicationErrorCode,
        reason_codes: tuple[str, ...] = (),
        retryable: bool = False,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.reason_codes = reason_codes
        self.retryable = retryable

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"reason_codes={self.reason_codes!r}, retryable={self.retryable!r})"
        )


class PolicyDeniedError(ApplicationError):
    """Deterministic policy refused these terms. Retryable only after the terms change."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        super().__init__(ApplicationErrorCode.POLICY_DENIED, reason_codes, False)


class StaleAuthorizationError(ApplicationError):
    """The caller decided against a version that is no longer current."""

    def __init__(self, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(ApplicationErrorCode.STALE_AUTHORIZATION, reason_codes, False)


class SendAuthorizationInProgressError(ApplicationError):
    """An unexpired send fence holds this case; the mutation is refused, briefly.

    Retryable, and that is the whole design. The frozen ordering says either the authorization
    change commits first and the send is denied stale, or the send commits first and the change
    waits out the fence -- at most sixty seconds. What must never happen is both believing they
    won, so the loser is told to come back rather than being merged in.
    """

    def __init__(self, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(ApplicationErrorCode.SEND_AUTHORIZATION_IN_PROGRESS, reason_codes, True)
