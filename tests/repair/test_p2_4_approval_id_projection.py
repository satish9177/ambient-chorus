"""P2-4: the current-action read exposes the durable approval binding.

Before this repair, `ReadCurrentAction` (and therefore `GET /v1/cases/{case_id}`) surfaced the
execution's state and version but not its `approval_id`, so a browser that approved a proposal
and reloaded before executing had no read path back to the one value `POST .../executions`
needs alongside `execution_id` and `expected_execution_version`. This test pins the fix at the
query layer: `approval_id` is `None` before approval and the row's own real value afterward --
never invented, never a second pointer.
"""

from __future__ import annotations

import pytest

from chorus.domain.entities import ActionExecutionState
from chorus.ports.storage import StorageDriver
from tests.fixtures.send import SendHarness

pytestmark = pytest.mark.anyio


@pytest.fixture
def send(storage: StorageDriver) -> SendHarness:
    from tests.fixtures.action import ActionHarness

    return SendHarness(action=ActionHarness(driver=storage))


async def test_approval_id_is_none_before_approval(send: SendHarness) -> None:
    await send.prepare()

    projection = await send.action.read_current_action().execute(send.scope)

    assert projection is not None
    assert projection.execution_state is ActionExecutionState.DRAFT
    assert projection.approval_id is None


async def test_approval_id_reads_back_the_exact_durable_binding_after_approval(
    send: SendHarness,
) -> None:
    await send.prepare()
    approval_result = await send.approve()

    projection = await send.action.read_current_action().execute(send.scope)

    assert projection is not None
    assert projection.execution_state is ActionExecutionState.APPROVED
    assert projection.approval_id is not None
    assert projection.approval_id == approval_result.approval_id

    # And the row itself agrees -- the projection is a read of this field, not a second value.
    execution = await send.execution()
    assert projection.approval_id == execution.approval_id
