"""Wire the sender Lambda's adapters to the one use case it exists to run.

A composition root and nothing else: it constructs, it does not decide. Every ordering rule is
inside ``chorus.application.commands.send_action``, every case-side authorization question is
inside the compiler's fence operation, and every classification is inside
``chorus.infrastructure.ses.classification``. If this module ever grows a branch on a state, an
outcome, or a destination, that branch is a second implementation of something already frozen.

**No deployed resource is created here.** Phase 8 owns this artifact, its role, its log group,
and the SES configuration set definition, and deploys none of them; the deployed function, the
live send, the verified identity, the sandbox exit, and the post-deploy IAM and SES canaries
belong to Phase 11. That is the same static-now split the compiler and the three agent runtimes
already use.

What this artifact must never import
-------------------------------------
Asserted by test, and the list is the sender's boundary written as code: no Strands, no Bedrock
client, no agent contract, no scheduler, and **no private domain type**. The sender renders an
already-approved message from already-safe artifacts; a private import would be the first step
toward it being able to read one.

Two compositions, and only one of them may touch Core
-----------------------------------------------------
The sender's IAM carries a **total Core deny** (ADR-024 SS 3), so a deployed sender cannot
construct a ``CoreRepository`` that answers anything. This root used to build one anyway, for
both compositions, and hand it to an in-process ``SendAuthorization`` -- which meant the
synthesized component could not perform its own send-time authorization at all, and nothing
caught it because no test runs without Core.

So the two compositions differ, and the difference is the boundary:

* **local** (``test`` and ``development``, where ``outbox_directory`` is set) builds the
  in-process authority over local storage, because there is no Lambda boundary to cross and the
  same objects can talk to the same tables;
* **deployed** builds :class:`chorus.infrastructure.compiler.send_authorization.
  CompilerSendAuthorization` over the one Core-adjacent grant the role actually holds --
  ``lambda:InvokeFunction`` on the compiler function ARN -- and constructs **no Core repository
  at all**.

``SendAction`` itself has no ``core`` field in either composition, which is what makes "the
sender cannot read Core" a property of the type rather than a convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from botocore.config import Config

from chorus.application.commands.send_action import SendAction
from chorus.application.services.action_authorization import SEND_FENCE_LIFETIME
from chorus.application.services.send_authorization import SendAuthorization
from chorus.domain.entities import Purpose
from chorus.domain.ids import IdGenerator, Uuid4Generator
from chorus.infrastructure.compiler.invoker import LambdaCompilerInvoker, create_lambda_client
from chorus.infrastructure.compiler.send_authorization import (
    CompilerInvokerPort,
    CompilerSendAuthorization,
)
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.local.sender import FilesystemOutboxSender
from chorus.infrastructure.ses.sender import SESV2_SERVICE_NAME, SesV2EmailSender
from chorus.ports.clock import Clock
from chorus.ports.records import StoredSafeDestination
from chorus.ports.repositories import CoreRepositoryPort, ShareableRepositoryPort
from chorus.ports.retention import AuditRetention
from chorus.ports.send_authorization import SendAuthorizationPort
from chorus.ports.sender import DestinationRegistryPort, EmailSenderPort
from chorus.ports.storage import TableName
from chorus.privacy.compiler import POLICY_BUILD_HASH
from chorus.privacy.policy import COMPILER_VERSION, POLICY_VERSION

SES_CONNECT_TIMEOUT_SECONDS = 10
"""Short, because a connection that never established is a **definite** non-send.

A connect timeout classifies as ``FAILED / SES_UNREACHABLE`` -- proof at the transport layer
that no request was transmitted (ADR-025 SS 8). Failing fast on that side costs nothing and
resolves the case definitively, so this is the one timeout worth tightening.
"""

SES_READ_TIMEOUT_SECONDS = int(SEND_FENCE_LIFETIME.total_seconds())
"""Pinned to the fence's maximum life, and deliberately no shorter.

An SES call may begin with as little as ``MIN_SEND_FENCE_WINDOW`` of fence remaining, so an
in-flight request can outlive the window that authorized it. Tightening this would **not** make
the safety property stronger -- the property is about the number of deliberate attempts, which a
timeout does not affect -- and it would make the system strictly worse: a read timeout is
classified ``SEND_UNKNOWN``, which is a quarantine no path may ever retry, so an aggressive value
converts slow-but-successful sends into permanent uncertainty.

What it does buy is a bound: a request cannot remain in flight for longer than the fence's own
maximum life, so the residual ADR-025 SS 5 already accepts -- an attempt that has started cannot
be recalled -- is bounded rather than open-ended. Derived from ``SEND_FENCE_LIFETIME`` rather
than written as ``60`` so the two cannot drift apart silently.
"""

SINGLE_ATTEMPT_CLIENT_CONFIG = Config(
    retries={"total_max_attempts": 1, "mode": "standard"},
    connect_timeout=SES_CONNECT_TIMEOUT_SECONDS,
    read_timeout=SES_READ_TIMEOUT_SECONDS,
)
"""One attempt, pinned at the SDK, with both transport deadlines stated rather than defaulted.

Without ``total_max_attempts`` botocore retries a throttle or a 5xx several times underneath the
single deliberate attempt the application believes it is making. The safety property is a
statement about the number of deliberate attempts, so an SDK retry is a second attempt nothing
records and nothing can see -- exactly the shape of duplication this phase exists to prevent.

The two timeouts happen to equal botocore's defaults on the read side and to be shorter on the
connect side. They are written down because each one is a decision about which side of the
``FAILED``/``SEND_UNKNOWN`` boundary a slow network lands on, and a decision that only exists as
somebody else's default is a decision nobody made.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class SenderSettings:
    """Everything the composition root needs, and nothing it could decide policy from."""

    region: str
    core_table: str
    shareable_table: str
    audit_table: str
    destination: StoredSafeDestination
    from_identity_id: str
    ses_configuration_set: str
    cursor_secret: bytes
    compiler_function_arn: str | None = None
    """The compiler function the deployed sender invokes for both halves of the fence.

    Required for a deployed composition and absent for a local one, which is the same split the
    outbox expresses on the SES side. It is an ARN rather than a bare name because it is the
    exact resource the sender's ``lambda:InvokeFunction`` grant names, and a name that resolved
    to a different account's function would be a grant the policy never made.
    """
    dynamodb_endpoint: str | None = None
    lambda_endpoint: str | None = None
    outbox_directory: Path | None = None
    """When set, the sender writes to a filesystem outbox and makes no network call.

    That is the required behaviour in ``test`` and ``development``. The one-attempt rule, the
    claim compare-and-swap, the classification table, and the fence all apply identically
    there, so every ambiguous path is exercised without SES ever being reachable.

    It is also what selects the **local** authorization composition, because the two travel
    together: an environment with no live SES is an environment with no Lambda boundary either.
    """

    @property
    def is_local(self) -> bool:
        """Whether this is the local composition. One predicate, read in two places."""

        return self.outbox_directory is not None


def build_email_sender(settings: SenderSettings) -> EmailSenderPort:
    """The outbox in local environments, and the pinned SESv2 client otherwise."""

    if settings.outbox_directory is not None:
        return FilesystemOutboxSender(directory=settings.outbox_directory)
    import boto3

    return SesV2EmailSender(
        client=boto3.client(
            SESV2_SERVICE_NAME,
            region_name=settings.region,
            config=SINGLE_ATTEMPT_CLIENT_CONFIG,
        )
    )


def build_send_authorization(
    settings: SenderSettings,
    *,
    clock: Clock,
    core: CoreRepositoryPort | None = None,
    shareable: ShareableRepositoryPort | None = None,
    invoker: CompilerInvokerPort | None = None,
) -> SendAuthorizationPort:
    """The local authority over storage, or the deployed one over the compiler invocation.

    The deployed branch constructs **no Core repository**, which is the whole repair: a role
    holding ``Deny dynamodb:* `` on the Core table cannot answer a case-side question, so a
    composition that built the in-process authority there was building an object that could only
    fail -- and could only fail in an account, never in a test.
    """

    if settings.is_local:
        if core is None or shareable is None:  # pragma: no cover - guarded by the caller
            raise ValueError("a local send authorization needs both repositories")
        return SendAuthorization(
            core=core,
            shareable=shareable,
            clock=clock,
            policy_version=POLICY_VERSION,
            compiler_version=COMPILER_VERSION,
            policy_build_hash=POLICY_BUILD_HASH,
            purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        )
    if invoker is None:
        if not settings.compiler_function_arn:
            # Refused rather than defaulted. A deployed sender with no compiler to invoke has
            # no way to acquire a fence, and the failure has to happen at construction rather
            # than at the first send.
            raise ValueError("a deployed sender needs the compiler function ARN")
        invoker = LambdaCompilerInvoker(
            client=create_lambda_client(
                region_name=settings.region, endpoint_url=settings.lambda_endpoint
            ),
            function_name=settings.compiler_function_arn,
        )
    return CompilerSendAuthorization(invoker=invoker)


def build_send_action(
    settings: SenderSettings,
    *,
    clock: Clock,
    registry: DestinationRegistryPort,
    sender: EmailSenderPort | None = None,
    ids: IdGenerator | None = None,
    core: CoreRepositoryPort | None = None,
    shareable: ShareableRepositoryPort | None = None,
    invoker: CompilerInvokerPort | None = None,
) -> SendAction:
    """Construct the send use case over deployed adapters.

    ``registry`` is injected rather than constructed because resolving it requires a Secrets
    Manager read scoped to one secret ARN, and which secret that is belongs to deployment
    configuration rather than to this function. ``invoker`` is injected for the same kind of
    reason on the deployed path, and it is the seam a test uses to construct this composition
    with Core genuinely unreachable.
    """

    driver = DynamoDbStorageDriver(
        client=create_dynamodb_client(
            region_name=settings.region, endpoint_url=settings.dynamodb_endpoint
        ),
        table_names={
            TableName.CORE: settings.core_table,
            TableName.SHAREABLE: settings.shareable_table,
            TableName.AUDIT: settings.audit_table,
        },
    )
    cursors = SignedCursorCodec(secret=settings.cursor_secret)
    shareable_repository = shareable or ShareableRepository(driver=driver, cursors=cursors)
    # Constructed **only** on the local branch. On the deployed one there is deliberately no
    # Core handle anywhere in this object graph.
    core_repository = (
        (core or CoreRepository(driver=driver, cursors=cursors)) if settings.is_local else None
    )
    generator = ids or Uuid4Generator()
    return SendAction(
        shareable=shareable_repository,
        audit=AuditRepository(driver=driver, cursors=cursors, retention=AuditRetention.demo()),
        # The send-command records live in the EXECUTION partition of the Shareable table,
        # which is the only partition the sender's LeadingKeys grant permits it to write
        # (ADR-024). A record in the action partition would be a row the principal that must
        # write it is denied.
        idempotency=IdempotencyRepository(driver=driver, table=TableName.SHAREABLE),
        unit_of_work=StorageUnitOfWork(driver=driver),
        authorization=build_send_authorization(
            settings,
            clock=clock,
            core=core_repository,
            shareable=shareable_repository,
            invoker=invoker,
        ),
        sender=sender or build_email_sender(settings),
        registry=registry,
        clock=clock,
        ids=generator,
        destination=settings.destination,
        from_identity_id=settings.from_identity_id,
        configuration_set=settings.ses_configuration_set,
    )


__all__ = [
    "SINGLE_ATTEMPT_CLIENT_CONFIG",
    "SenderSettings",
    "build_email_sender",
    "build_send_action",
    "build_send_authorization",
]
