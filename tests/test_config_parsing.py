import pytest
from smartfeed.models.base import BaseNode
from smartfeed.models import FeedConfig


class DummyNode(BaseNode):
    type: str = "dummy"
    node_id: str = "test"
    params: dict = {}


def test_config_hash_deterministic():
    a = DummyNode(params={"b": 2, "a": 1})
    b = DummyNode(params={"a": 1, "b": 2})
    assert a.config_hash() == b.config_hash()
    assert len(a.config_hash()) == 8


def test_config_hash_changes_with_params():
    a = DummyNode(params={"x": 1})
    b = DummyNode(params={"x": 2})
    assert a.config_hash() != b.config_hash()


def test_parse_simple_subfeed():
    cfg = FeedConfig.parse_obj({
        "version": "2",
        "feed": {"type": "subfeed", "subfeed_id": "x", "method_name": "items"},
    })
    assert cfg.feed.subfeed_id == "x"


def test_parse_wrapper_with_cache():
    cfg = FeedConfig.parse_obj({
        "version": "2",
        "feed": {
            "type": "wrapper", "node_id": "w",
            "cache": {"session_size": 100, "session_ttl": 300},
            "data": {"type": "subfeed", "subfeed_id": "x", "method_name": "items"},
        },
    })
    assert cfg.feed.cache.session_size == 100


def test_parse_nested_config():
    cfg = FeedConfig.parse_obj({
        "version": "2",
        "feed": {
            "type": "wrapper", "node_id": "outer",
            "dedup": {"dedup_key": "id"},
            "data": {
                "type": "merger_percentage", "node_id": "mix",
                "items": [
                    {"percentage": 50, "data": {"type": "subfeed", "subfeed_id": "a", "method_name": "items"}},
                    {"percentage": 50, "data": {"type": "subfeed", "subfeed_id": "b", "method_name": "items"}},
                ],
            },
        },
    })
    assert cfg.feed.dedup.dedup_key == "id"
