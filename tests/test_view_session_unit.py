from dataclasses import dataclass
from typing import Any

import pytest

from smartfeed.feed_models import _redis_call
from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerViewSession
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_VIEW_SESSION_CONFIG
from tests.fixtures.redis import redis_client
from tests.utils import parse_model


@dataclass
class _ItemWithAttr:
    id: str


def test_get_dedup_key_supports_dict_and_attr_and_raises_on_missing() -> None:
    cfg = dict(MERGER_VIEW_SESSION_CONFIG)
    cfg.update({"deduplicate": True, "dedup_key": "id"})
    merger = parse_model(MergerViewSession, cfg)

    assert merger._get_dedup_key_or_attr({"id": "x"}) == "x"
    assert merger._get_dedup_key_or_attr(_ItemWithAttr(id="y")) == "y"

    with pytest.raises(AssertionError):
        merger._get_dedup_key_or_attr({"nope": 1})


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_view_session_shuffle_applies_to_result(redis_client, monkeypatch) -> None:
    import smartfeed.mergers.view_session as vs_mod

    cfg = dict(MERGER_VIEW_SESSION_CONFIG)
    cfg.update({"shuffle": True})
    merger = parse_model(MergerViewSession, cfg)
    cache_key = f"{merger.merger_id}_x"
    await _redis_call(redis_client, "delete", cache_key)

    # Make shuffle deterministic: reverse in-place
    monkeypatch.setattr(vs_mod, "shuffle", lambda data: data.reverse())

    res = await merger.get_data(
        methods_dict=METHODS_DICT,
        limit=5,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
    )

    assert res.data == ["x_5", "x_4", "x_3", "x_2", "x_1"]
    await _redis_call(redis_client, "delete", cache_key)
