from typing import Callable, Dict, Optional

import pytest

from smartfeed.examples.example_client import LookyMixer, LookyMixerRequest
from smartfeed.manager import FeedManager
from smartfeed.schemas import (
    FeedConfig,
    FeedResult,
    FeedResultNextPage,
    FeedResultNextPageInside,
    MergerAppend,
    MergerPercentage,
    MergerPercentageGradient,
    MergerPercentageItem,
    MergerPositional,
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
    async def get_next_page(subfeed_data: Dict[str, FeedResultNextPage]) -> FeedResultNextPage:
        """
        Метод для получения модели курсора пагинации из данных субфидов.

        :param subfeed_data: данные субфидов
        :return: модель курсора пагинации.
        """

        subfeed_next_page_data = {}

        for subfeed_id, next_page in subfeed_data.items():
            subfeed_next_page_data[subfeed_id] = FeedResultNextPageInside(
                page=next_page.data[subfeed_id].page if subfeed_id in next_page.data else 1,
                after=next_page.data[subfeed_id].after if subfeed_id in next_page.data else None,
            )

        subfeed_next_page = FeedResultNextPage(data=subfeed_next_page_data)
        return subfeed_next_page

    async def get_example_client_method_result(
        self,
        subfeed_id: str,
        query_params: LookyMixerRequest,
        percentage: int = 0,
        limit_to_return: Optional[int] = None,
    ) -> FeedResult:
        """
        Метод для получения данных метода example_client.

        :param subfeed_id: ID субфида.
        :param query_params: входные параметры.
        :param percentage: процентное соотношение (если 0, то не учитываем)
        :param limit_to_return: ограничить количество результата.
        :return: SmartFeedResult.
        """

        next_page = await self.get_next_page(subfeed_data={subfeed_id: query_params.next_page})
        method_result = await LookyMixer().looky_method(
            subfeed_id=subfeed_id,
            limit=query_params.limit if percentage == 0 else query_params.limit * percentage // 100,
            user_id=query_params.profile_id,
            next_page=next_page,
            limit_to_return=limit_to_return,
        )
        return method_result

    @pytest.mark.asyncio
    async def test_parsing_sample_config(self) -> None:
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

    @pytest.mark.asyncio
    async def test_sub_feed_get_data(self) -> None:
        """
        Тест для проверки получения данных субфидов.
        """

        # Формируем "правильные ответы".
        sub_feed_ans = await self.get_example_client_method_result(
            subfeed_id=self.sub_feed.subfeed_id,
            query_params=self.query_params,
        )

        # Получаем данные из субфидов.
        sub_feed_data = await self.sub_feed.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )

        print(f"\n\nSubFeed Data: {sub_feed_data}")
        assert sub_feed_data.json() == sub_feed_ans.json()

    @pytest.mark.asyncio
    async def test_merger_percentage_get_data(self) -> None:
        """
        Тест для проверки получения данных процентного мерджера.
        """

        self.query_params.next_page = FeedResultNextPage(
            data={"ec_sub_feed_2": FeedResultNextPageInside(page=5, after="x_12")}
        )

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
        item_1_ans = await self.get_example_client_method_result(
            subfeed_id=item_1.data.subfeed_id,
            query_params=self.query_params,
            percentage=item_1.percentage,
        )
        item_2_ans = await self.get_example_client_method_result(
            subfeed_id=item_2.data.subfeed_id,
            query_params=self.query_params,
            percentage=item_2.percentage,
        )
        merger_percentage_ans = FeedResult(
            data=(item_1_ans.data + item_2_ans.data),
            next_page=await self.get_next_page(
                subfeed_data={
                    item_1.data.subfeed_id: item_1_ans.next_page,
                    item_2.data.subfeed_id: item_2_ans.next_page,
                }
            ),
            has_next_page=True if any([item_1_ans.has_next_page, item_2_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        merger_percentage_data = await merger_percentage.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )
        merger_percentage_shuffled_data = await merger_percentage_shuffled.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )

        print(f"\n\nPercentage for 1st': {item_1.percentage}%")
        print(f"\nPercentage for 2nd: {item_2.percentage}%")
        print(f"\nMerger Percentage Data: {merger_percentage_data}")
        print(f"\nMerger Percentage + Shuffle Data: {merger_percentage_shuffled_data}")

        assert merger_percentage_data == merger_percentage_ans
        assert set(merger_percentage_shuffled_data.data) == set(merger_percentage_ans.data)

    @pytest.mark.asyncio
    async def test_merger_positional_get_data(self) -> None:
        """
        Тест для проверки получения данных позиционного мерджера.
        """

        self.query_params.next_page = FeedResultNextPage(
            data={
                "ec_merger_positional_with_positions": FeedResultNextPageInside(page=2, after=None),
                "ec_sub_feed_2": FeedResultNextPageInside(page=3, after="x_20"),
            }
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
        default_ans = await self.get_example_client_method_result(
            subfeed_id=self.sub_feed_2.subfeed_id,
            query_params=self.query_params,
        )
        positional_ans = await self.get_example_client_method_result(
            subfeed_id=self.sub_feed.subfeed_id,
            query_params=self.query_params,
        )

        positional_ans.next_page.data[self.sub_feed.subfeed_id].after = "x_1"
        mp_with_positions_ans = FeedResult(
            data=["x_21", "x_1", "x_22", "x_23", "x_24", "x_25", "x_26", "x_27", "x_28", "x_29"],
            next_page=await self.get_next_page(
                subfeed_data={
                    self.sub_feed.subfeed_id: positional_ans.next_page,
                    self.sub_feed_2.subfeed_id: default_ans.next_page,
                    mp_with_positions.merger_id: FeedResultNextPage(
                        data={mp_with_positions.merger_id: FeedResultNextPageInside(page=3, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_ans.has_next_page, positional_ans.has_next_page]) else False,
        )

        positional_ans.next_page.data[self.sub_feed.subfeed_id].after = "x_3"
        mp_with_step_ans = FeedResult(
            data=["x_21", "x_1", "x_22", "x_23", "x_24", "x_2", "x_25", "x_26", "x_27", "x_3"],
            next_page=await self.get_next_page(
                subfeed_data={
                    self.sub_feed.subfeed_id: positional_ans.next_page,
                    self.sub_feed_2.subfeed_id: default_ans.next_page,
                    mp_with_step.merger_id: FeedResultNextPage(
                        data={mp_with_step.merger_id: FeedResultNextPageInside(page=2, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_ans.has_next_page, positional_ans.has_next_page]) else False,
        )

        positional_ans.next_page.data[self.sub_feed.subfeed_id].after = "x_5"
        mp_both_ans = FeedResult(
            data=["x_1", "x_21", "x_2", "x_3", "x_22", "x_4", "x_23", "x_5", "x_24", "x_25"],
            next_page=await self.get_next_page(
                subfeed_data={
                    self.sub_feed.subfeed_id: positional_ans.next_page,
                    self.sub_feed_2.subfeed_id: default_ans.next_page,
                    mp_both.merger_id: FeedResultNextPage(
                        data={mp_both.merger_id: FeedResultNextPageInside(page=2, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_ans.has_next_page, positional_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        mp_with_positions_data = await mp_with_positions.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )
        mp_with_step_data = await mp_with_step.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )
        mp_both_data = await mp_both.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )

        print(f"\n\nPositions: {mp_with_positions.positions}")
        print(f"\nMerger Positional with Positions Data: {mp_with_positions_data}")
        print(f"\nMerger Positional with Step Data: {mp_with_step_data}")
        print(f"\nMerger Positional with Positions & Step Data: {mp_both_data}")

        assert mp_with_positions_data == mp_with_positions_ans
        assert mp_with_step_data == mp_with_step_ans
        assert mp_both_data == mp_both_ans

    @pytest.mark.asyncio
    async def test_merger_append_get_data(self) -> None:
        """
        Тест для проверки получения данных append мерджера.
        """

        self.query_params.next_page = FeedResultNextPage(
            data={"ec_sub_feed_2": FeedResultNextPageInside(page=10, after="x_27")}
        )

        merger_append = MergerAppend(
            merger_id="ec_merger_append",
            type="merger_append",
            items=[self.sub_feed, self.sub_feed_2],
        )

        # Формируем "правильные ответы".
        item_1_ans = await self.get_example_client_method_result(
            subfeed_id=self.sub_feed.subfeed_id,
            query_params=self.query_params,
            limit_to_return=7,
        )

        item_2_ans = await self.get_example_client_method_result(
            subfeed_id=self.sub_feed_2.subfeed_id,
            query_params=self.query_params,
            percentage=30,
            limit_to_return=7,
        )
        merger_append_ans = FeedResult(
            data=(item_1_ans.data + item_2_ans.data),
            next_page=await self.get_next_page(
                subfeed_data={
                    self.sub_feed.subfeed_id: item_1_ans.next_page,
                    self.sub_feed_2.subfeed_id: item_2_ans.next_page,
                }
            ),
            has_next_page=True if any([item_1_ans.has_next_page, item_2_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        merger_append_data = await merger_append.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
            limit_to_return=7,
        )

        print(f"\nMerger Append Data: {merger_append_data}")

        assert merger_append_data == merger_append_ans

    @pytest.mark.asyncio
    async def test_merger_percentage_gradient_calculate_limits_and_percents(self) -> None:
        """
        Тест для проверки получения списка лимитов данных с процентным соотношением позиций item_from & item_to,
        учитывая градиентное изменение соотношений.
        """

        item_1 = MergerPercentageItem(percentage=70, data=self.sub_feed)
        item_2 = MergerPercentageItem(percentage=30, data=self.sub_feed_2)
        mp_gradient_1 = MergerPercentageGradient(
            merger_id="ec_merger_percentage_gradient",
            type="merger_percentage_gradient",
            item_from=item_1,
            item_to=item_2,
            step=8,
            size_to_step=30,
            shuffle=False,
        )
        mp_gradient_1_limits_and_percents_ans = [
            {"limit": 7, "from": 30, "to": 70},
            {"limit": 30, "from": 22, "to": 78},
            {"limit": 30, "from": 14, "to": 86},
            {"limit": 30, "from": 6, "to": 94},
            {"limit": 30, "from": 0, "to": 100},
            {"limit": 30, "from": 0, "to": 100},
            {"limit": 16, "from": 0, "to": 100},
        ]
        mp_gradient_1_limits_and_percents_data = await mp_gradient_1._calculate_limits_and_percents(page=2, limit=173)

        mp_gradient_2 = MergerPercentageGradient(
            merger_id="ec_merger_percentage_gradient",
            type="merger_percentage_gradient",
            item_from=item_1,
            item_to=item_2,
            step=1,
            size_to_step=43,
            shuffle=False,
        )
        mp_gradient_2_limits_and_percents_ans = [
            {"limit": 5, "from": 57, "to": 43},
            {"limit": 43, "from": 56, "to": 44},
            {"limit": 43, "from": 55, "to": 45},
            {"limit": 43, "from": 54, "to": 46},
            {"limit": 43, "from": 53, "to": 47},
            {"limit": 22, "from": 52, "to": 48},
        ]
        mp_gradient_2_limits_and_percents_data = await mp_gradient_2._calculate_limits_and_percents(page=4, limit=199)

        assert mp_gradient_1_limits_and_percents_data == mp_gradient_1_limits_and_percents_ans
        assert mp_gradient_2_limits_and_percents_data == mp_gradient_2_limits_and_percents_ans

    @pytest.mark.asyncio
    async def test_merger_percentage_gradient_get_data(self) -> None:
        """
        Тест для проверки получения данных процентного мерджера с градиентом.
        """

        self.query_params.limit = 30
        self.query_params.next_page = FeedResultNextPage(
            data={"ec_merger_percentage_gradient": FeedResultNextPageInside(page=3, after=None)}
        )

        item_1 = MergerPercentageItem(percentage=75, data=self.sub_feed)
        item_2 = MergerPercentageItem(percentage=25, data=self.sub_feed_2)
        mp_gradient = MergerPercentageGradient(
            merger_id="ec_merger_percentage_gradient",
            type="merger_percentage_gradient",
            item_from=item_1,
            item_to=item_2,
            step=25,
            size_to_step=30,
            shuffle=False,
        )
        mp_gradient_shuffled = MergerPercentageGradient(
            merger_id="ec_merger_percentage_gradient",
            type="merger_percentage_gradient",
            item_from=item_1,
            item_to=item_2,
            step=25,
            size_to_step=30,
            shuffle=True,
        )

        # Формируем "правильные ответы".
        item_1_ans = await self.get_example_client_method_result(
            subfeed_id=item_1.data.subfeed_id,
            query_params=self.query_params,
            percentage=item_1.percentage - 50,
        )
        item_2_ans = await self.get_example_client_method_result(
            subfeed_id=item_2.data.subfeed_id,
            query_params=self.query_params,
            percentage=item_2.percentage + 50,
        )
        mp_gradient_ans = FeedResult(
            data=(item_1_ans.data + item_2_ans.data),
            next_page=await self.get_next_page(
                subfeed_data={
                    mp_gradient.merger_id: FeedResultNextPage(
                        data={mp_gradient.merger_id: FeedResultNextPageInside(page=4, after=None)}
                    ),
                    item_1.data.subfeed_id: item_1_ans.next_page,
                    item_2.data.subfeed_id: item_2_ans.next_page,
                }
            ),
            has_next_page=True if any([item_1_ans.has_next_page, item_2_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        mp_gradient_data = await mp_gradient.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )
        mp_gradient_shuffled_data = await mp_gradient_shuffled.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )

        print(f"\n\nPercentage for 1st': {item_1.percentage}%")
        print(f"\nPercentage for 2nd: {item_2.percentage}%")
        print(f"\nMerger Percentage Gradient Data: {mp_gradient_data}")
        print(f"\nMerger Percentage Gradient + Shuffle Data: {mp_gradient_shuffled_data}")

        assert mp_gradient_data == mp_gradient_ans
        assert set(mp_gradient_shuffled_data.data) == set(mp_gradient_ans.data)

    @pytest.mark.asyncio
    async def test_feed_get_data(self) -> None:
        """
        Тест для проверки получения данных фида с помощью менеджера фидов.
        """

        self.query_params.next_page = FeedResultNextPage(
            data={
                "merger_pos": FeedResultNextPageInside(page=2, after=None),
                "sf_positional": FeedResultNextPageInside(page=9, after="x_24"),
                "sf_1_default_merger_of_main": FeedResultNextPageInside(page=1, after=None),
                "sf_2_default_merger_of_main": FeedResultNextPageInside(page=10, after="x_36"),
            }
        )

        feed_manager = FeedManager(config=EXAMPLE_CLIENT_FEED, methods_dict=self.methods_dict)
        feed = feed_manager.feed_config.feed

        # Формируем "правильные ответы".
        default_1_ans = await self.get_example_client_method_result(
            subfeed_id=feed.default.items[0].data.subfeed_id,
            query_params=self.query_params,
            percentage=feed.default.items[0].percentage,
        )
        default_2_ans = await self.get_example_client_method_result(
            subfeed_id=feed.default.items[1].data.subfeed_id,
            query_params=self.query_params,
            percentage=feed.default.items[1].percentage,
        )
        default_merger_ans = FeedResult(
            data=(default_1_ans.data + default_2_ans.data),
            next_page=await self.get_next_page(
                subfeed_data={
                    feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                }
            ),
            has_next_page=True if any([default_1_ans.has_next_page, default_2_ans.has_next_page]) else False,
        )
        pos_ans = await self.get_example_client_method_result(
            subfeed_id=feed.positional.subfeed_id, query_params=self.query_params, limit_to_return=3
        )

        feed_ans = FeedResult(
            data=["x_1", "x_2", "x_3", "x_4", "x_25", "x_37", "x_26", "x_38", "x_27", "x_39"],
            next_page=await self.get_next_page(
                subfeed_data={
                    feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                    feed.positional.subfeed_id: pos_ans.next_page,
                    feed.merger_id: FeedResultNextPage(
                        data={feed.merger_id: FeedResultNextPageInside(page=3, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_merger_ans.has_next_page, pos_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        feed_data = await feed_manager.get_data(
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            user_id=self.query_params.profile_id,
        )

        print(f"\nFeed Data: {feed_data}")
        assert feed_data == feed_ans
