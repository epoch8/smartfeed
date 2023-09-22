import pytest

from smartfeed.manager import FeedManager
from smartfeed.schemas import (
    FeedConfig,
    MergerAppend,
    MergerPercentage,
    MergerPercentageGradient,
    MergerPercentageItem,
    MergerPositional,
    MergerViewSession,
    SubFeed,
)
from tests.fixtures.configs import METHODS_DICT, PARSING_CONFIG_FIXTURE


@pytest.mark.asyncio
async def test_parsing_config() -> None:
    """
    Тест для проверки парсинга JSON-файла конфигурации.
    """

    feed_manager = FeedManager(config=PARSING_CONFIG_FIXTURE, methods_dict=METHODS_DICT)

    # Feed Config.
    assert isinstance(feed_manager.feed_config, FeedConfig)
    # Merger Positional.
    assert isinstance(feed_manager.feed_config.feed, MergerPositional)
    # Merger Append.
    assert isinstance(feed_manager.feed_config.feed.positional, MergerAppend)
    # SubFeed with SubFeed Params.
    assert isinstance(feed_manager.feed_config.feed.positional.items[0], SubFeed)
    # Merger Percentage Gradient.
    assert isinstance(feed_manager.feed_config.feed.positional.items[1], MergerPercentageGradient)
    # Merger View Session.
    assert isinstance(feed_manager.feed_config.feed.positional.items[2], MergerViewSession)
    # Merger Percentage.
    assert isinstance(feed_manager.feed_config.feed.default, MergerPercentage)
    # Merger Percentage Item.
    assert isinstance(feed_manager.feed_config.feed.default.items[0], MergerPercentageItem)
    # SubFeed without SubFeed Params.
    assert isinstance(feed_manager.feed_config.feed.positional.items[1].item_from.data, SubFeed)
    assert isinstance(feed_manager.feed_config.feed.positional.items[1].item_to.data, SubFeed)
    assert isinstance(feed_manager.feed_config.feed.positional.items[2].data, SubFeed)
    # SubFeed with Raise Exception False.
    assert isinstance(feed_manager.feed_config.feed.default.items[0].data, SubFeed)
    assert feed_manager.feed_config.feed.default.items[0].data.raise_error is False
