from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..feed_models import BaseFeedConfigModel, FeedResultNextPage


@dataclass
class CursorMap:
    next_page: FeedResultNextPage

    def merge_delta(self, *, base_next_page: FeedResultNextPage, owner_next_page: FeedResultNextPage) -> None:
        """Merge only the cursor keys that actually changed."""

        for key, value in owner_next_page.data.items():
            base_value = base_next_page.data.get(key)
            if base_value == value:
                continue
            self.next_page.data[key] = value

    def reset_keys(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.next_page.data.pop(key, None)

    @staticmethod
    def can_overfetch(*, node: BaseFeedConfigModel, base_next_page: FeedResultNextPage) -> bool:
        sub_id = getattr(node, "subfeed_id", None)
        if not isinstance(sub_id, str):
            return False
        entry = base_next_page.data.get(sub_id)
        if entry is None:
            return False
        return isinstance(entry.after, int)

    @staticmethod
    def rewind_overfetch(
        *,
        node: BaseFeedConfigModel,
        base_next_page: FeedResultNextPage,
        result_next_page: FeedResultNextPage,
        inspected_count: int,
        batch_size: int,
    ) -> None:
        sub_id = getattr(node, "subfeed_id", None)
        if not isinstance(sub_id, str):
            return
        if sub_id not in result_next_page.data:
            return

        entry = result_next_page.data[sub_id]
        end_after = entry.after
        if not isinstance(end_after, int):
            return

        base_entry = base_next_page.data.get(sub_id)
        prev_after = base_entry.after if base_entry is not None else None
        if not isinstance(prev_after, int):
            return

        expected_end = prev_after + batch_size
        if end_after == expected_end:
            entry.after = prev_after + inspected_count
