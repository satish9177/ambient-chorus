"""The deployed demo reset's S3 object-prefix purge and EventBridge Scheduler purge.

Both are prefix / name-grammar bounded: they list only under ``ns/DEMO/`` (S3) or the frozen
``chorus-{env}-{namespace_hash8}`` schedule-name grammar (Scheduler), and every key or name is
re-checked before a delete is issued. Neither has an in-memory equivalent here -- tests drive
the reset orchestration with :mod:`chorus.infrastructure.local.demo_reset`; these are the real
adapters the Lambda composition wires.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import ClientError

from chorus.ports.demo_reset import require_demo_object_prefix

_S3_DELETE_BATCH = 1000
"""S3 ``DeleteObjects`` caps at 1000 keys per request."""


class S3PurgeClient(Protocol):
    """The narrow S3 surface the object-prefix purge needs, beyond ``S3ObjectStore``'s."""

    def list_objects_v2(self, **kwargs: object) -> Any: ...

    def delete_objects(self, **kwargs: object) -> Any: ...


class SchedulerPurgeClient(Protocol):
    """The narrow EventBridge Scheduler surface the schedule purge needs."""

    def list_schedules(self, **kwargs: object) -> Any: ...

    def delete_schedule(self, **kwargs: object) -> Any: ...


@dataclass(frozen=True, slots=True)
class S3DemoObjectPrefixPurge:
    """List and bounded-delete ``ns/DEMO/`` objects in one evidence bucket."""

    client: S3PurgeClient

    async def list_prefix(self, *, bucket: str, prefix: str) -> tuple[str, ...]:
        require_demo_object_prefix(prefix)
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token is not None:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for entry in response.get("Contents", ()) or ():
                key = str(entry["Key"])
                require_demo_object_prefix(key)
                keys.append(key)
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if token is None:
                break
        return tuple(keys)

    async def delete_objects(self, *, bucket: str, keys: Sequence[str]) -> int:
        checked = [require_demo_object_prefix(key) for key in keys]
        deleted = 0
        for start in range(0, len(checked), _S3_DELETE_BATCH):
            batch = checked[start : start + _S3_DELETE_BATCH]
            response = self.client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            errors = response.get("Errors", ()) or ()
            if errors:
                raise RuntimeError(f"S3 DeleteObjects reported {len(errors)} error(s)")
            deleted += len(batch)
        return deleted


@dataclass(frozen=True, slots=True)
class SchedulerDemoSchedulePurge:
    """List and delete the demo's one-time schedules in the ``chorus-{env}`` group.

    A name-grammar sweep, not a group wipe: only schedules whose name begins with the frozen
    ``chorus-{env}-{namespace_hash8}`` prefix are listed, and each name is re-checked before a
    ``DeleteSchedule``.
    """

    client: SchedulerPurgeClient
    group_name: str
    name_prefix: str

    async def list_demo_schedules(self, *, name_prefix: str) -> tuple[str, ...]:
        names: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {
                "GroupName": self.group_name,
                "NamePrefix": name_prefix,
                "MaxResults": 100,
            }
            if token is not None:
                kwargs["NextToken"] = token
            response = self.client.list_schedules(**kwargs)
            for entry in response.get("Schedules", ()) or ():
                name = str(entry["Name"])
                if name.startswith(name_prefix):
                    names.append(name)
            token = response.get("NextToken")
            if token is None:
                break
        return tuple(names)

    async def delete_schedules(self, *, names: Sequence[str]) -> int:
        deleted = 0
        for name in names:
            if not name.startswith(self.name_prefix):
                continue  # never delete a schedule outside the DEMO name grammar
            try:
                self.client.delete_schedule(Name=name, GroupName=self.group_name)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    continue
                raise
            deleted += 1
        return deleted


__all__ = [
    "S3DemoObjectPrefixPurge",
    "S3PurgeClient",
    "SchedulerDemoSchedulePurge",
    "SchedulerPurgeClient",
]
