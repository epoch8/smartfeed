import json
from typing import Callable, Dict

from smartfeed.examples.example_client import LookyMixer, LookyMixerRequest
from smartfeed.manager import FeedManager
from smartfeed.schemas import (
    FeedConfig,
    MergerPercentage,
    MergerPositional,
    SmartFeedResult,
    SmartFeedResultNextPage,
    SmartFeedResultNextPageInside,
    SubFeed,
)

CONFIG_PATH: str = "tests/fixtures/example_client_config.json"
with open(CONFIG_PATH, encoding="utf-8") as config_file:
    CONFIG = json.load(config_file)


class TestExampleClientConfig:
    """
    Класс тестирования конфигурации фида example_client.
    """

    def setup_method(self):
        self.feed_limit: int = 50
        self.methods_dict: Dict[str, Callable] = {
            "ads": LookyMixer().looky_method,
            "followings": LookyMixer().looky_method,
            "followings_2": LookyMixer().looky_method,
        }
        self.feed_manager = FeedManager(config=CONFIG, methods_dict=self.methods_dict)
        self.feed = self.feed_manager.feed_config.feed
        self.query_params = LookyMixerRequest(
            profile_id="x",
            limit=self.feed_limit,
            next_page={
                "data": {
                    "merger_pos": {"page": 2},
                    "sf_positional": {},
                    "sf_1_default_merger_of_main": {"page": 2, "after": []},
                    "sf_2_default_merger_of_main": {"page": 3, "after": []},
                }
            },
        )

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

        assert isinstance(self.feed_manager.feed_config, FeedConfig)
        assert isinstance(self.feed_manager.feed_config.feed, MergerPositional)
        assert isinstance(self.feed_manager.feed_config.feed.positional, SubFeed)
        assert isinstance(self.feed_manager.feed_config.feed.default, MergerPercentage)
        assert isinstance(self.feed_manager.feed_config.feed.default.items[0].data, SubFeed)
        assert self.feed_manager.feed_config.feed.default.items[0].percentage == 40

    def test_sub_feed_get_data(self) -> None:
        """
        Тест для проверки получения данных субфидов.
        """

        # Формируем "правильные ответы".
        positional_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.positional.subfeed_id,
            query_params=self.query_params,
        )
        default_subfeed_1_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.default.items[0].data.subfeed_id,
            query_params=self.query_params,
        )

        # Получаем данные из субфидов.
        positional_data = self.feed.positional.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )
        default_subfeed_1_data = self.feed.default.items[0].data.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\n\nPositional SubFeed Data: {positional_data}")
        assert positional_data.json() == positional_ans.json()
        print(f"\nDefault 1st SubFeed Data: {default_subfeed_1_data}")
        assert default_subfeed_1_data.json() == default_subfeed_1_ans.json()

    def test_merger_percentage_get_data(self) -> None:
        """
        Тест для проверки получения данных процентного мерджера.
        """

        # Формируем "правильные ответы".
        default_1_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.default.items[0].data.subfeed_id,
            query_params=self.query_params,
            percentage=self.feed.default.items[0].percentage,
        )
        default_2_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.default.items[1].data.subfeed_id,
            query_params=self.query_params,
            percentage=self.feed.default.items[1].percentage,
        )
        default_merger_ans = SmartFeedResult(
            data=(default_1_ans.data + default_2_ans.data),
            next_page=self.get_next_page(
                subfeed_data={
                    self.feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    self.feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                }
            ),
            has_next_page=True if any([default_1_ans.has_next_page, default_2_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        default_merger_data = self.feed.default.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\n\nPercentage for 1st': {self.feed.default.items[0].percentage}%")
        print(f"\nPercentage for 2nd: {self.feed.default.items[1].percentage}%")
        print(f"\nPercentage Merger Data: {default_merger_data}")

        if self.feed.default.shuffle:
            assert set(default_merger_data.data) == set(default_merger_ans.data)
        else:
            assert default_merger_data == default_merger_ans

    def test_merger_positional_get_data(self) -> None:
        """
        Тест для проверки получения данных позиционного мерджера.
        """

        # Формируем "правильные ответы".
        default_1_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.default.items[0].data.subfeed_id,
            query_params=self.query_params,
            percentage=self.feed.default.items[0].percentage,
        )
        default_2_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.default.items[1].data.subfeed_id,
            query_params=self.query_params,
            percentage=self.feed.default.items[1].percentage,
        )
        default_merger_ans = SmartFeedResult(
            data=(default_1_ans.data + default_2_ans.data),
            next_page=self.get_next_page(
                subfeed_data={
                    self.feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    self.feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                }
            ),
            has_next_page=True if any([default_1_ans.has_next_page, default_2_ans.has_next_page]) else False,
        )
        pos_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.positional.subfeed_id,
            query_params=self.query_params,
        )

        pos_merger_ans = SmartFeedResult(
            data=[
                "x_20",
                "x_21",
                "x_22",
                "x_23",
                "x_0",
                "x_1",
                "x_24",
                "x_2",
                "x_25",
                "x_3",
                "x_26",
                "x_4",
                "x_27",
                "x_5",
                "x_28",
                "x_6",
                "x_29",
                "x_7",
                "x_30",
                "x_8",
                "x_31",
                "x_9",
                "x_32",
                "x_10",
                "x_33",
                "x_11",
                "x_34",
                "x_12",
                "x_35",
                "x_13",
                "x_36",
                "x_14",
                "x_37",
                "x_15",
                "x_38",
                "x_16",
                "x_39",
                "x_17",
                "x_60",
                "x_18",
                "x_61",
                "x_19",
                "x_62",
                "x_20",
                "x_63",
                "x_21",
                "x_64",
                "x_22",
                "x_65",
                "x_66",
                "x_67",
                "x_68",
                "x_69",
                "x_70",
                "x_71",
                "x_72",
                "x_73",
                "x_74",
                "x_75",
                "x_76",
                "x_77",
                "x_78",
                "x_79",
                "x_80",
                "x_81",
                "x_82",
                "x_83",
                "x_84",
                "x_85",
                "x_86",
                "x_87",
                "x_88",
                "x_89",
            ],
            next_page=self.get_next_page(
                subfeed_data={
                    self.feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    self.feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                    self.feed.positional.subfeed_id: pos_ans.next_page,
                    self.feed.merger_id: SmartFeedResultNextPage(
                        data={self.feed.merger_id: SmartFeedResultNextPageInside(page=3, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_merger_ans.has_next_page, pos_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        pos_merger_data = self.feed.get_data(
            methods_dict=self.methods_dict,
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\n\nPositions for 'x': {self.feed.positions}")
        print(f"\nPositional Merger Data: {pos_merger_data}")
        assert pos_merger_data == pos_merger_ans

    def test_feed_get_data(self) -> None:
        """
        Тест для проверки получения данных фида с помощью менеджера фидов.
        """

        # Формируем "правильные ответы".
        default_1_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.default.items[0].data.subfeed_id,
            query_params=self.query_params,
            percentage=self.feed.default.items[0].percentage,
        )
        default_2_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.default.items[1].data.subfeed_id,
            query_params=self.query_params,
            percentage=self.feed.default.items[1].percentage,
        )
        default_merger_ans = SmartFeedResult(
            data=(default_1_ans.data + default_2_ans.data),
            next_page=self.get_next_page(
                subfeed_data={
                    self.feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    self.feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                }
            ),
            has_next_page=True if any([default_1_ans.has_next_page, default_2_ans.has_next_page]) else False,
        )
        pos_ans = self.get_example_client_method_result(
            subfeed_id=self.feed.positional.subfeed_id,
            query_params=self.query_params,
        )

        feed_ans = SmartFeedResult(
            data=[
                "x_20",
                "x_21",
                "x_22",
                "x_23",
                "x_0",
                "x_1",
                "x_24",
                "x_2",
                "x_25",
                "x_3",
                "x_26",
                "x_4",
                "x_27",
                "x_5",
                "x_28",
                "x_6",
                "x_29",
                "x_7",
                "x_30",
                "x_8",
                "x_31",
                "x_9",
                "x_32",
                "x_10",
                "x_33",
                "x_11",
                "x_34",
                "x_12",
                "x_35",
                "x_13",
                "x_36",
                "x_14",
                "x_37",
                "x_15",
                "x_38",
                "x_16",
                "x_39",
                "x_17",
                "x_60",
                "x_18",
                "x_61",
                "x_19",
                "x_62",
                "x_20",
                "x_63",
                "x_21",
                "x_64",
                "x_22",
                "x_65",
                "x_66",
                "x_67",
                "x_68",
                "x_69",
                "x_70",
                "x_71",
                "x_72",
                "x_73",
                "x_74",
                "x_75",
                "x_76",
                "x_77",
                "x_78",
                "x_79",
                "x_80",
                "x_81",
                "x_82",
                "x_83",
                "x_84",
                "x_85",
                "x_86",
                "x_87",
                "x_88",
                "x_89",
            ],
            next_page=self.get_next_page(
                subfeed_data={
                    self.feed.default.items[0].data.subfeed_id: default_1_ans.next_page,
                    self.feed.default.items[1].data.subfeed_id: default_2_ans.next_page,
                    self.feed.positional.subfeed_id: pos_ans.next_page,
                    self.feed.merger_id: SmartFeedResultNextPage(
                        data={self.feed.merger_id: SmartFeedResultNextPageInside(page=3, after=None)}
                    ),
                }
            ),
            has_next_page=True if any([default_merger_ans.has_next_page, pos_ans.has_next_page]) else False,
        )

        # Получаем данные из мерджера.
        feed_data = self.feed_manager.get_data(
            limit=self.query_params.limit,
            next_page=self.query_params.next_page,
            profile_id=self.query_params.profile_id,
        )

        print(f"\nFeed Data: {feed_data}")
        assert feed_data == feed_ans
