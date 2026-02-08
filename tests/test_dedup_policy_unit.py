from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from smartfeed.policies.dedup import DeduplicationPolicy
from smartfeed.policies.seen_store import CursorSeenStore


def _policy(*, dedup_key: Optional[str], missing_key_policy: str, after: Any = None) -> DeduplicationPolicy:
    store = CursorSeenStore.from_after(after=after, cursor_compress=False, cursor_max_keys=None)
    return DeduplicationPolicy(
        dedup_key=dedup_key,
        missing_key_policy=missing_key_policy,  # type: ignore[arg-type]
        store=store,
        seen_request_set=set(),
    )


@pytest.mark.asyncio
async def test_accept_batch_dedups_within_request_and_respects_existing_priority() -> None:
    # Existing seen key with high priority should block lower/equal priority
    existing_after = {"v": 2, "seen": [["1", 10]]}
    policy = _policy(dedup_key="id", missing_key_policy="keep", after=existing_after)

    items = [{"id": 1}, {"id": 1}, {"id": 2}]
    accepted, inspected = await policy.accept_batch(items=items, priority=5)

    assert inspected == 3
    assert accepted == [{"id": 2}]


@pytest.mark.asyncio
async def test_accept_batch_missing_key_policies() -> None:
    items = [{"id": 1}, {"nope": 2}]

    policy_drop = _policy(dedup_key="id", missing_key_policy="drop")
    accepted_drop, _ = await policy_drop.accept_batch(items=items, priority=0)
    assert accepted_drop == [{"id": 1}]

    policy_error = _policy(dedup_key="id", missing_key_policy="error")
    with pytest.raises(AssertionError):
        await policy_error.accept_batch(items=items, priority=0)


@dataclass
class _Owner:
    dedup_priority: int


@pytest.mark.asyncio
async def test_arbitrate_owner_buffers_prefers_higher_priority_owner() -> None:
    policy = _policy(dedup_key="id", missing_key_policy="keep")

    owner_low = _Owner(dedup_priority=1)
    owner_high = _Owner(dedup_priority=2)

    owners = [owner_low, owner_high]
    owner_rank: Dict[int, int] = {id(owner_low): 0, id(owner_high): 1}

    shared = {"id": "same"}
    owner_buffers: Dict[int, List[Any]] = {
        id(owner_low): [shared, {"id": "low_only"}],
        id(owner_high): [shared, {"id": "high_only"}],
    }

    per_owner = await policy.arbitrate_owner_buffers(
        owners=owners,
        owner_buffers=owner_buffers,
        owner_rank=owner_rank,
    )

    assert per_owner[id(owner_high)] == [shared, {"id": "high_only"}]
    assert per_owner[id(owner_low)] == [{"id": "low_only"}]
