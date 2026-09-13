"""In-memory demo-reset purge adapters, so the deployed reset orchestration runs without AWS.

The manifest store, the lock, the receipt store, and the partition purge already work over
:class:`~chorus.infrastructure.local.memory.InMemoryStorageDriver` by construction -- they only
need a :class:`~chorus.ports.storage.StorageDriver`. What is left is the object-prefix purge and
the schedule purge, whose real adapters talk to S3 and EventBridge Scheduler; these back onto
the in-memory object store and deadline scheduler instead.
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from chorus.infrastructure.local.objects import InMemoryObjectStore
from chorus.infrastructure.local.scheduler import InMemoryDeadlineScheduler
from chorus.ports.demo_reset import require_demo_object_prefix


@dataclass(frozen=True, slots=True)
class InMemoryDemoObjectPrefixPurge:
    """List and delete ``ns/DEMO/`` objects in the in-memory object store."""

    objects: InMemoryObjectStore
    bucket_kind: str  # "private" or "export"

    def _store(self) -> MutableMapping[str, Any]:
        return self.objects.private if self.bucket_kind == "private" else self.objects.export

    async def list_prefix(self, *, bucket: str, prefix: str) -> tuple[str, ...]:
        require_demo_object_prefix(prefix)
        _ = bucket
        return tuple(key for key in self._store() if key.startswith(prefix))

    async def delete_objects(self, *, bucket: str, keys: Sequence[str]) -> int:
        _ = bucket
        store = self._store()
        deleted = 0
        for key in keys:
            require_demo_object_prefix(key)
            if store.pop(key, None) is not None:
                deleted += 1
        return deleted


@dataclass(frozen=True, slots=True)
class InMemoryDemoSchedulePurge:
    """List and delete the demo's one-time schedules in the in-memory deadline scheduler."""

    scheduler: InMemoryDeadlineScheduler
    schedule_name_prefix: str = ""

    async def list_demo_schedules(self, *, name_prefix: str) -> tuple[str, ...]:
        return tuple(name for name in self.scheduler.schedules if name.startswith(name_prefix))

    async def delete_schedules(self, *, names: Sequence[str]) -> int:
        deleted = 0
        for name in names:
            if not name.startswith(self.schedule_name_prefix):
                continue
            if self.scheduler.schedules.pop(name, None) is not None:
                deleted += 1
        return deleted


@dataclass(slots=True)
class InMemoryDemoManifestRegistrar:
    """Record every partition the demo registered, for tests that assert the inventory grows."""

    registered: list[str] = field(default_factory=list)

    async def register_partition(self, partition_key: str) -> None:
        if partition_key not in self.registered:
            self.registered.append(partition_key)


__all__ = [
    "InMemoryDemoManifestRegistrar",
    "InMemoryDemoObjectPrefixPurge",
    "InMemoryDemoSchedulePurge",
]
