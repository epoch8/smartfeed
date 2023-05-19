from typing import Callable, Dict

from smartfeed.examples.example_client import LookyMixer, LookyMixerRequest
from smartfeed.manager import FeedManager
from smartfeed.schemas import (
    FeedConfig,
    MergerPercentage,
    MergerPercentageItem,
    MergerPositional,
    SmartFeedResult,
    SmartFeedResultNextPage,
    SmartFeedResultNextPageInside,
    SubFeed,
)
from tests.fixtures.configs import EXAMPLE_CLIENT_FEED


class TestExampleClientConfig:
    """
    Класс тестирования конфигурации фида example_client.
    """

    def setup_method(self):
        self.sub_feed = SubFeed(subfeed_id="ec_sub_feed", type="subfeed", method_name="ads")
        self.sub_feed_2 = SubFeed(subfeed_id="ec_sub_feed_2", type="subfeed", method_name="ads")
        self.query_params = LookyMixerRequest(profile_id="x", limit=10)
        self.methods_dict: Dict[str, Callable] = {
            "ads": LookyMixer().looky_method,
            "followings": LookyMixer().looky_method,
        }

    @staticmethod
    def get_next_page(subfeed_data: Dict[str, SmartFeedResultNextPage]) -> SmartFeedResultNextPage:
        """
        Метод для получения модели курсора пагинации из данных субфидов.

        :param subfeed_data: данные субфидов
        :return: модель курсора пагинации.
        """

        subfeed_next_page_data = {}

        for subfeed_id, next_page in subfeed_data.items():
            subfeed_next_page_data[subfeed_id] = SmartFeedResultNextPageInside(
                page=next_page.data[subfeed_id].page if subfeed_id in next_page.data else 1,
                after=next_page.data[subfeed_id].after if subfeed_id in next_page.data else None,
            )

        subfeed_next_page = SmartFeedResultNextPage(data=subfeed_next_page_data)
        return subfeed_next_page

    def get_example_client_method_result(
        self,
        subfeed_id: str,
        query_params: LookyMixerRequest,
        percentage: int = 0,
    ) -> SmartFeedResult:
        """
        Метод для получения данных метода example_client.

        :param subfeed_id: ID субфида.
        :param query_params: входные параметры.
        :param percentage: процентное соотношение (если 0, то не учитываем)
        :return: SmartFeedResult.
        """

        next_page = self.get_next_page(subfeed_data={subfeed_id: query_params.next_page})
        method_result = LookyMixer().looky_method(
            subfeed_id=subfeed_id,
            limit=query_params.limit if percentage == 0 else query_params.limit * percentage // 100,
            profile_id=query_params.profile_id,
            next_page=next_page,
        )
        return method_result

    def test_parsing_sample_config(self) -> None:
        """
        Тест для проверки парсинга JSON-файла конфигурации.
        """

        feed_manager = FeedManager(config=EXAMPLE_CLIENT_FEED, methods_dict=self.methods_dict)

        assert isinstance(feed_manager.feed_config, FeedConfig)
        assert isinstance(feed_manager.feed_config.feed, MergerPositional)
        assert isinstance(feed_manager.feed_config.feed.positional, SubFeed)
        assert isinstance(feed_manager.feed_config.feed.default, MergerPercentage)
        assert isinstance(feed_manager.feed_config.feed.default.items[0].data, SubFeed)
        assert feed_manager.feed_config.feed.default.items[0].percentage == 40

    def test_sub_feed_get_data(self) -> None:
        """
        Тест для проверки получения данных субфидов.
        """

        # Формируем "правильные ответы".
        sub_feed_ans = self.get_example_client_method_result(
            subfeed_id=self.sub_feed.subfeed_id,
            query_params=self.query_params,
        )

        # Получаем данные из субфидов.
        sub_feed_data = self.sub_feed.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\n\nSubFeed Data: {sub_feed_data}")
        assert sub_feed_data.json() == sub_feed_ans.json()

    def test_merger_percentage_get_data(self) -> None:
        """
        Тест для проверки получения данных процентного мерджера.
        """

        item_1 = MergerPercentageItem(percentage=70, data=self.sub_feed)
        item_2 = MergerPercentageItem(percentage=30, data=self.sub_feed_2)
        merger_percentage = MergerPercentage(
            merger_id="ec_merger_percentage",
            type="merger_percentage",
            shuffle=False,
            items=[item_1, item_2],
        )
        merger_percentage_shuffled = MergerPercentage(
            merger_id="ec_merger_percentage",
            type="merger_percentage",
            shuffle=True,
            items=[item_1, item_2],
        )

        # Формируем "правильные ответы".
        item_1_ans = self.get_example_client_method_result(
            subfeed_id=item_1.data.subfeed_id,
            query_params=self.query_params,
            percentage=item_1.percentage,
        )
        item_2_ans = self.get_example_client_method_result(
            subfeed_id=item_2.data.subfeed_id,
            query_params=self.query_params,
            percentage=item_2.percentage,
        )
        merger_percentage_ans = SmartFeedResult(
            data=(item_1_ans.data + item_2_ans.data),
            next_page=self.get_next_page(
                subfeed_data={
                    item_1.data.subfeed_id: item_1_ans.next_page,
                    item_2.data.subfeed_id: item_2_ans.next_page,
                }
            ),
            has_next_page=True if any([item_1_ans.has_next_page, item_2_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        merger_percentage_data = merger_percentage.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )
        merger_percentage_shuffled_data = merger_percentage_shuffled.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\n\nPercentage for 1st': {item_1.percentage}%")
        print(f"\nPercentage for 2nd: {item_2.percentage}%")
        print(f"\nMerger Percentage Data: {merger_percentage_data}")
        print(f"\nMerger Percentage + Shuffle Data: {merger_percentage_shuffled_data}")

        assert merger_percentage_data == merger_percentage_ans
        assert set(merger_percentage_shuffled_data.data) == set(merger_percentage_ans.data)

    def test_merger_positional_get_data(self) -> None:
        """
        Тест для проверки получения данных позиционного мерджера.
        """

        self.query_params.next_page = SmartFeedResultNextPage(
            data={"ec_sub_feed_2": SmartFeedResultNextPageInside(page=2, after=None)}
        )

        mp_with_positions = MergerPositional(
            merger_id="ec_merger_positional_with_positions",
            type="merger_positional",
            positions=[1, 3, 12],
            positional=self.sub_feed,
            default=self.sub_feed_2,
        )
        mp_with_step = MergerPositional(
            merger_id="ec_merger_positional_with_step",
            type="merger_positional",
            start=2,
            end=25,
            step=4,
            positional=self.sub_feed,
            default=self.sub_feed_2,
        )
        mp_both = MergerPositional(
            merger_id="ec_merger_positional",
            type="merger_positional",
            positions=[1, 3],
            start=4,
            end=9,
            step=2,
            positional=self.sub_feed,
            default=self.sub_feed_2,
        )

        # Формируем "правильные ответы".
        default_ans = self.get_example_client_method_result(
            subfeed_id=self.sub_feed_2.subfeed_id,
            query_params=self.query_params,
        )
        positional_ans = self.get_example_client_method_result(
            subfeed_id=self.sub_feed.subfeed_id,
            query_params=self.query_params,
        )

        mp_with_positions_ans = SmartFeedResult(
            data=["x_0", "x_10", "x_1", "x_11", "x_12", "x_13", "x_14", "x_15", "x_16", "x_17"],
            next_page=self.get_next_page(
                subfeed_data={
                    self.sub_feed.subfeed_id: positional_ans.next_page,
                    self.sub_feed_2.subfeed_id: default_ans.next_page,
                    mp_with_positions.merger_id: SmartFeedResultNextPage(
                        data={mp_with_positions.merger_id: SmartFeedResultNextPageInside(page=2, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_ans.has_next_page, positional_ans.has_next_page]) else False,
        )
        mp_with_step_ans = SmartFeedResult(
            data=["x_10", "x_0", "x_11", "x_12", "x_13", "x_1", "x_14", "x_15", "x_16", "x_2"],
            next_page=self.get_next_page(
                subfeed_data={
                    self.sub_feed.subfeed_id: positional_ans.next_page,
                    self.sub_feed_2.subfeed_id: default_ans.next_page,
                    mp_with_step.merger_id: SmartFeedResultNextPage(
                        data={mp_with_step.merger_id: SmartFeedResultNextPageInside(page=2, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_ans.has_next_page, positional_ans.has_next_page]) else False,
        )
        mp_both_ans = SmartFeedResult(
            data=["x_0", "x_10", "x_1", "x_2", "x_11", "x_3", "x_12", "x_4", "x_13", "x_14"],
            next_page=self.get_next_page(
                subfeed_data={
                    self.sub_feed.subfeed_id: positional_ans.next_page,
                    self.sub_feed_2.subfeed_id: default_ans.next_page,
                    mp_both.merger_id: SmartFeedResultNextPage(
                        data={mp_both.merger_id: SmartFeedResultNextPageInside(page=2, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_ans.has_next_page, positional_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        mp_with_positions_data = mp_with_positions.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )
        mp_with_step_data = mp_with_step.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )
        mp_both_data = mp_both.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\n\nPositions: {mp_with_positions.positions}")
        print(f"\nMerger Positional with Positions Data: {mp_with_positions_data}")
        print(f"\nMerger Positional with Step Data: {mp_with_step_data}")
        print(f"\nMerger Positional with Positions & Step Data: {mp_both_data}")

        assert mp_with_positions_data == mp_with_positions_ans
        assert mp_with_step_data == mp_with_step_ans
        assert mp_both_data == mp_both_ans

    def test_feed_get_data(self) -> None:
        """
        Тест для проверки получения данных фида с помощью менеджера фидов.
        """

        self.query_params.next_page = SmartFeedResultNextPage(
            data={
                "merger_pos": SmartFeedResultNextPageInside(page=2, after=None),
                "sf_positional": {},
                "sf_1_default_merger_of_main": SmartFeedResultNextPageInside(page=2, after=None),
                "sf_2_default_merger_of_main": SmartFeedResultNextPageInside(page=3, after=None),
            }
        )

        feed_manager = FeedManager(config=EXAMPLE_CLIENT_FEED, methods_dict=self.methods_dict)
        feed = feed_manager.feed_config.feed

        # Формируем "правильные ответы".
        default_1_ans = self.get_example_client_method_result(
            subfeed_id=feed.default.items[0].data.subfeed_id,
            query_params=self.query_params,
            percentage=feed.default.items[0].percentage,
        )
        default_2_ans = self.get_example_client_method_result(
            subfeed_id=feed.default.items[1].data.subfeed_id,
            query_params=self.query_params,
            percentage=feed.default.items[1].percentage,
        )
        default_merger_ans = SmartFeedResult(
            data=(default_1_ans.data + default_2_ans.data),
            next_page=self.get_next_page(
                subfeed_data={
                    feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                }
            ),
            has_next_page=True if any([default_1_ans.has_next_page, default_2_ans.has_next_page]) else False,
        )
        pos_ans = self.get_example_client_method_result(
            subfeed_id=feed.positional.subfeed_id,
            query_params=self.query_params,
        )

        feed_ans = SmartFeedResult(
            data=["x_0", "x_4", "x_5", "x_6", "x_7", "x_12", "x_13", "x_14", "x_15", "x_16"],
            next_page=self.get_next_page(
                subfeed_data={
                    feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                    feed.positional.subfeed_id: pos_ans.next_page,
                    feed.merger_id: SmartFeedResultNextPage(
                        data={feed.merger_id: SmartFeedResultNextPageInside(page=3, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_merger_ans.has_next_page, pos_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        feed_data = feed_manager.get_data(
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\nFeed Data: {feed_data}")
        assert feed_data == feed_ans
