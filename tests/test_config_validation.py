"""Config-construction validation: duplicate
subfeed_id/node_id anywhere in the tree, and non-positive gradient step /
size_to_step, are rejected at parse time instead of corrupting state or
crashing at runtime.
"""

import pytest
from pydantic import ValidationError

from smartfeed.models import (
    FeedConfig,
    MergerAppendDistribute,
    MergerPercentage,
    MergerPercentageGradient,
    MergerPositional,
)


def _cfg(feed: dict) -> dict:
    return {"version": "2", "feed": feed}


def _sub(sid: str) -> dict:
    return {"type": "subfeed", "subfeed_id": sid, "method_name": "m"}


def test_duplicate_subfeed_ids_rejected():
    feed = {"type": "merger_append", "node_id": "mix", "items": [_sub("a"), _sub("a")]}
    with pytest.raises(ValidationError, match="duplicate id 'a'"):
        FeedConfig.model_validate(_cfg(feed))


def test_subfeed_id_colliding_with_node_id_rejected():
    """subfeed_id and node_id share one cursor namespace -- a collision across
    kinds is just as corrupting as within one kind."""
    feed = {"type": "wrapper", "node_id": "x", "data": _sub("x")}
    with pytest.raises(ValidationError, match="duplicate id 'x'"):
        FeedConfig.model_validate(_cfg(feed))


def test_duplicate_across_nested_branches_rejected():
    feed = {
        "type": "merger_percentage",
        "node_id": "mix",
        "items": [
            {"percentage": 50, "data": {"type": "wrapper", "node_id": "w1", "data": _sub("a")}},
            {"percentage": 50, "data": {"type": "wrapper", "node_id": "w2", "data": _sub("a")}},
        ],
    }
    with pytest.raises(ValidationError, match="duplicate id 'a'"):
        FeedConfig.model_validate(_cfg(feed))


def test_duplicate_wrapper_node_ids_rejected():
    feed = {
        "type": "merger_append",
        "node_id": "mix",
        "items": [
            {"type": "wrapper", "node_id": "w", "data": _sub("a")},
            {"type": "wrapper", "node_id": "w", "data": _sub("b")},
        ],
    }
    with pytest.raises(ValidationError, match="duplicate id 'w'"):
        FeedConfig.model_validate(_cfg(feed))


def test_unique_ids_parse_ok():
    feed = {
        "type": "merger_percentage",
        "node_id": "mix",
        "items": [
            {"percentage": 50, "data": {"type": "wrapper", "node_id": "w1", "data": _sub("a")}},
            {"percentage": 50, "data": {"type": "wrapper", "node_id": "w2", "data": _sub("b")}},
        ],
    }
    cfg = FeedConfig.model_validate(_cfg(feed))
    assert isinstance(cfg.feed, MergerPercentage)
    assert cfg.feed.node_id == "mix"


def _gradient_feed(step: int, size_to_step: int) -> dict:
    return {
        "type": "merger_percentage_gradient",
        "node_id": "g",
        "item_from": {"percentage": 80, "data": _sub("a")},
        "item_to": {"percentage": 20, "data": _sub("b")},
        "step": step,
        "size_to_step": size_to_step,
    }


def test_gradient_zero_size_to_step_rejected_at_parse():
    with pytest.raises(ValidationError, match="size_to_step"):
        FeedConfig.model_validate(_cfg(_gradient_feed(step=10, size_to_step=0)))


def test_gradient_non_positive_step_rejected_at_parse():
    for bad_step in (0, -5):
        with pytest.raises(ValidationError, match="step"):
            FeedConfig.model_validate(_cfg(_gradient_feed(step=bad_step, size_to_step=10)))


def test_gradient_positive_params_parse_ok():
    cfg = FeedConfig.model_validate(_cfg(_gradient_feed(step=10, size_to_step=30)))
    assert isinstance(cfg.feed, MergerPercentageGradient)
    assert cfg.feed.step == 10


def test_positional_round_trips_via_public_config():
    feed = {
        "type": "merger_positional",
        "node_id": "pos",
        "positions": [1, 3],
        "positional": _sub("p"),
        "default": _sub("d"),
    }
    cfg = FeedConfig.model_validate(_cfg(feed))
    assert isinstance(cfg.feed, MergerPositional)
    assert cfg.feed.positions == [1, 3]


def test_distribute_round_trips_via_public_config():
    feed = {
        "type": "merger_distribute",
        "node_id": "dist",
        "distribution_key": "op",
        "sorting_key": "score",
        "sorting_desc": True,
        "items": [_sub("x"), _sub("y")],
    }
    cfg = FeedConfig.model_validate(_cfg(feed))
    assert isinstance(cfg.feed, MergerAppendDistribute)
    assert cfg.feed.distribution_key == "op"


def test_missing_required_field_raises():
    with pytest.raises(ValidationError):
        FeedConfig.model_validate(_cfg({"type": "subfeed", "subfeed_id": "a"}))  # method_name missing
