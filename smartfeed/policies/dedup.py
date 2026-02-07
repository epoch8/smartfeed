from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

from .. import jsonlib as json
from .seen_store import SeenStore

MissingKeyPolicy = Literal["error", "keep", "drop"]


def normalize_key(value: Any) -> str:
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def extract_dedup_value(item: Any, dedup_key: Optional[str], missing_key_policy: MissingKeyPolicy) -> Any:
    if not dedup_key:
        return item

    try:
        value = item.get(dedup_key)
    except AttributeError:
        value = getattr(item, dedup_key, None)

    if value is None and missing_key_policy == "error":
        raise AssertionError(f"Deduplication failed: entity {item} has no key or attr {dedup_key}")
    return value


def entity_key(item: Any, dedup_key: Optional[str], missing_key_policy: MissingKeyPolicy) -> Optional[str]:
    raw_value = extract_dedup_value(item, dedup_key, missing_key_policy)
    if raw_value is None:
        if missing_key_policy == "drop":
            return None
        if missing_key_policy == "keep":
            raw_value = ("__missing__", id(item))
    return normalize_key(raw_value)


@dataclass
class DeduplicationPolicy:
    """Deduplication policy applied during execution.

    This keeps dedup logic out of merger implementations and plan interpreters.
    """

    dedup_key: Optional[str]
    missing_key_policy: MissingKeyPolicy

    # Store backend (cursor or redis)
    store: SeenStore

    # Keys encountered/accepted within this request. Prevents duplicates inside one response.
    seen_request_set: set[str]

    def key_for(self, item: Any) -> Optional[str]:
        return entity_key(item, self.dedup_key, self.missing_key_policy)

    async def prefetch_keys(self, keys: List[str]) -> None:
        if not keys:
            return

        filtered: List[str] = []
        seen: set[str] = set()
        for k in keys:
            if k in self.seen_request_set:
                continue
            if k in seen:
                continue
            seen.add(k)
            filtered.append(k)

        if not filtered:
            return

        await self.store.prefetch(filtered)

    def should_accept(self, key: str, priority: int) -> bool:
        if key in self.seen_request_set:
            return False

        existing_priority = self.store.get(key)
        if existing_priority is not None and priority <= existing_priority:
            return False
        return True

    def record(self, key: str, priority: int) -> None:
        self.seen_request_set.add(key)
        self.store.set_max(key, priority)

    async def arbitrate_owner_buffers(
        self,
        *,
        owners: List[Any],
        owner_buffers: Dict[int, List[Any]],
        owner_rank: Dict[int, int],
    ) -> Dict[int, List[Any]]:
        """Arbitrate winners across multiple owners.

        - Deterministic tie-break: (-priority, owner_rank, item_rank)
        - Records winners into the store
        - Returns per-owner buffers containing only accepted winners
        """

        keys_to_prefetch: List[str] = []
        keys_seen_local: set[str] = set()
        for owner in owners:
            owner_id = id(owner)
            for item in owner_buffers.get(owner_id, []):
                key = self.key_for(item)
                if key is None:
                    continue
                if key in keys_seen_local:
                    continue
                keys_seen_local.add(key)
                keys_to_prefetch.append(key)

        if keys_to_prefetch:
            await self.prefetch_keys(keys_to_prefetch)

        winners: Dict[str, int] = {}
        winner_prio: Dict[str, int] = {}
        winner_tie: Dict[str, Tuple[int, int, int]] = {}

        for owner in owners:
            owner_id = id(owner)
            prio = int(getattr(owner, "dedup_priority", 0))
            rank = int(owner_rank.get(owner_id, 0))
            for item_rank, item in enumerate(owner_buffers.get(owner_id, [])):
                key = self.key_for(item)
                if key is None:
                    continue
                tie = (-prio, rank, item_rank)
                existing = winner_tie.get(key)
                if existing is None or tie < existing:
                    winners[key] = owner_id
                    winner_prio[key] = prio
                    winner_tie[key] = tie

        for key, _tie in sorted(winner_tie.items(), key=lambda kv: kv[1]):
            winner_owner_id = winners.get(key)
            if winner_owner_id is None:
                continue
            prio = int(winner_prio.get(key, 0))
            if not self.should_accept(key, prio):
                continue
            self.record(key, prio)

        request_set = self.seen_request_set
        per_owner_accepted: Dict[int, List[Any]] = {id(o): [] for o in owners}
        for owner in owners:
            owner_id = id(owner)
            accepted: List[Any] = []
            for item in owner_buffers.get(owner_id, []):
                key = self.key_for(item)
                if key is None:
                    continue
                if winners.get(key) != owner_id:
                    continue
                if key not in request_set:
                    continue
                accepted.append(item)
            per_owner_accepted[owner_id] = accepted

        return per_owner_accepted

    async def accept_batch(
        self,
        *,
        items: List[Any],
        priority: int,
        limit: Optional[int] = None,
    ) -> Tuple[List[Any], int]:
        """Accept items from a single stream in order.

        Returns accepted items and the number of inspected items.
        """

        if not items:
            return [], 0

        keys_to_prefetch: List[str] = []
        keys_seen_local: set[str] = set()
        for item in items:
            key = self.key_for(item)
            if key is None:
                continue
            if key in self.seen_request_set:
                continue
            if key in keys_seen_local:
                continue
            keys_seen_local.add(key)
            keys_to_prefetch.append(key)

        if keys_to_prefetch:
            await self.prefetch_keys(keys_to_prefetch)

        accepted: List[Any] = []
        inspected_count = 0
        max_accept = int(limit) if limit is not None else len(items)

        for idx, item in enumerate(items, start=1):
            inspected_count = idx
            if len(accepted) >= max_accept:
                break
            key = self.key_for(item)
            if key is None:
                continue
            if not self.should_accept(key, priority):
                continue
            accepted.append(item)
            self.record(key, priority)
            if len(accepted) >= max_accept:
                break

        return accepted, inspected_count
