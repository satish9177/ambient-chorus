"""One storage-boundary interlock for normal DEMO mutations.

Only reset composition enters ``reset_authority``. Ordinary writes, including single-item
repository methods, acquire the absence condition in the same DynamoDB transaction. The
reference driver uses the identical participant builder, so interleaving tests exercise it.
"""

from contextvars import ContextVar
from hashlib import sha256

from chorus.domain.ids import Namespace
from chorus.ports.storage import CheckItem, ItemKey, KeyAbsent, PutItem, TableName, WriteOperation

reset_authority: ContextVar[str | None] = ContextVar("demo_reset_authority", default=None)
reset_transaction_identity: ContextVar[str | None] = ContextVar(
    "demo_reset_transaction_identity", default=None
)

LOCK_KEY = ItemKey(table=TableName.CORE, partition_key="NS#DEMO", sort_key="DEMO_RESET_LOCK")


def is_normal_demo_write(operation: WriteOperation) -> bool:
    key = operation.key
    return (
        not isinstance(operation, CheckItem)
        and (key.partition_key == "NS#DEMO" or key.partition_key.startswith("NS#DEMO#"))
        and not (key.partition_key == "NS#DEMO" and key.sort_key.startswith("DEMO_"))
    )


def fence_operations(operations: tuple[WriteOperation, ...]) -> tuple[WriteOperation, ...]:
    if reset_authority.get() is not None or not any(map(is_normal_demo_write, operations)):
        return operations
    result = list(operations)
    targets = {operation.key for operation in result}
    guard = CheckItem(key=LOCK_KEY, condition=KeyAbsent())
    if any(operation.key == LOCK_KEY and operation != guard for operation in operations):
        raise ValueError("normal DEMO mutations require the reset lock to be absent")
    if LOCK_KEY not in targets:
        result.append(guard)
    # Only the writer of a new private case registers its case-world roots. Compiler/sender
    # descendants remain discoverable through the existing atomic pointer/history records.
    from chorus.infrastructure.dynamodb.demo_reset_store import DynamoDbDemoManifestRegistrar

    for operation in operations:
        key = operation.key
        if (
            isinstance(operation, PutItem)
            and isinstance(operation.condition, KeyAbsent)
            and key.table == TableName.CORE
            and key.partition_key.startswith("NS#DEMO#CASE#")
            and key.sort_key == "CASE"
        ):
            case_id = key.partition_key.removeprefix("NS#DEMO#CASE#")
            for family in ("CASE", "FENCE", "VIEW_CURRENT", "ACTION_CURRENT"):
                registration = DynamoDbDemoManifestRegistrar.partition_registration(
                    Namespace("DEMO"), f"NS#DEMO#{family}#{case_id}"
                )
                if registration.key not in targets:
                    result.append(registration)
                    targets.add(registration.key)
    return tuple(result)


def transaction_token(token: str) -> str:
    identity = reset_transaction_identity.get()
    return token if identity is None else sha256(f"{identity}\x1f{token}".encode()).hexdigest()[:36]
