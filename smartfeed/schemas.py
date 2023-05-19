from abc import ABC, abstractmethod
from random import shuffle
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, root_validator

FeedTypes = Annotated[
    Union[
        "MergerPositional",
        "MergerPercentage",
        "SubFeed",
    ],
    Field(discriminator="type"),
]


class SmartFeedResultNextPageInside(BaseModel):
    """
    Модель данных курсора пагинации конкретной позиции.
    """

    page: int = 1
    after: Any = None


class SmartFeedResultNextPage(BaseModel):
    """
    Модель курсора пагинации.
    """

    data: Dict[str, SmartFeedResultNextPageInside]


class SmartFeedResult(BaseModel):
    """
    Модель результата метода get_data() любой позиции / целого фида.
    """

    data: List
    next_page: SmartFeedResultNextPage
    has_next_page: bool


class BaseFeedConfigModel(ABC, BaseModel):
    """
    Абстрактный класс для мерджера / субфида конфигурации.
    """

    @abstractmethod
    def get_data(
        self,
        methods_dict: Dict[str, Callable],
        limit: int,
        next_page: SmartFeedResultNextPage,
        **params: Any,
    ) -> SmartFeedResult:
        """
        Метод для получения данных.

        :param methods_dict: словарь с используемыми методами.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: параметры для метода.
        :return: список данных.
        """


class MergerAppend(BaseFeedConfigModel):
    """
    Модель append мерджера.
    """

    merger_id: str
    type: Literal["merger_append"]
    items: List[FeedTypes]

    def get_data(
        self,
        methods_dict: Dict[str, Callable],
        limit: int,
        next_page: SmartFeedResultNextPage,
        **params: Any,
    ) -> SmartFeedResult:
        """
        Метод для получения данных методом append.

        :param methods_dict: словарь с используемыми методами.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных методом append.
        """

        # Формируем результат append мерджера.
        result = SmartFeedResult(data=[], next_page=SmartFeedResultNextPage(data={}), has_next_page=False)

        result_limit = limit
        for item in self.items:
            # Получаем данные из позиции мерджера.
            item_result = item.get_data(methods_dict=methods_dict, limit=result_limit, next_page=next_page, **params)

            # Добавляем данные позиции к общему результату процентного мерджера.
            result.data.extend(item_result.data)

            # Обновляем result_limit
            result_limit -= len(item_result.data)

            # Если has_next_page = False, то проверяем has_next_page у позиции и, если необходимо, обновляем.
            if not result.has_next_page and item_result.has_next_page:
                result.has_next_page = True

            # Обновляем next_page.
            result.next_page.data.update(item_result.next_page.data)

            # Если полученных данных хватает, то прерываем итерацию и возвращаем результат.
            if result_limit <= 0:
                break

        return result


class MergerPositional(BaseFeedConfigModel):
    """
    Модель позиционного мерджера.
    """

    merger_id: str
    type: Literal["merger_positional"]
    positions: List[int] = []
    start: Optional[int] = None
    end: Optional[int] = None
    step: Optional[int] = None
    positional: FeedTypes
    default: FeedTypes

    @root_validator
    def validate_merger_positional(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if not values["positions"] and not all((values["start"], values["end"], values["step"])):
            raise ValueError('Either "positions" or "start", "end", and "step" must be provided')
        if values["start"] and values["positions"]:
            if isinstance(values["start"], int) and values["start"] <= max(values["positions"]):
                raise ValueError('"start" must be bigger than maximum value of "positions"')
        if isinstance(values["start"], int) and isinstance(values["end"], int):
            if values["end"] <= values["start"]:
                raise ValueError('"end" must be bigger than "start"')
        return values

    def get_data(
        self,
        methods_dict: Dict[str, Callable],
        limit: int,
        next_page: SmartFeedResultNextPage,
        **params: Any,
    ) -> SmartFeedResult:
        """
        Метод для получения данных в позиционном соотношении из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Получаем данные "default".
        default_res = self.default.get_data(methods_dict=methods_dict, limit=limit, next_page=next_page, **params)

        # Формируем результат позиционного мерджера.
        result = SmartFeedResult(
            data=default_res.data,
            next_page=SmartFeedResultNextPage(
                data={
                    self.merger_id: SmartFeedResultNextPageInside(
                        page=next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1,
                        after=next_page.data[self.merger_id].after if self.merger_id in next_page.data else None,
                    )
                },
            ),
            has_next_page=False,
        )

        # Получаем список позиций с учетом текущей страницы.
        page_positions = []
        available_positions = range(
            (result.next_page.data[self.merger_id].page - 1) * limit,
            (result.next_page.data[self.merger_id].page * limit) + 1,
        )
        for position in self.positions:
            if position in available_positions:
                page_positions.append(available_positions.index(position))

        if self.start is not None and self.end is not None and self.step is not None:
            for position in range(self.start, self.end, self.step):
                if position in available_positions:
                    page_positions.append(available_positions.index(position))

        # Получаем данные "positional".
        pos_res = self.positional.get_data(
            methods_dict=methods_dict, limit=len(page_positions), next_page=next_page, **params
        )

        # Если has_next_page = False, то проверяем has_next_page у позиции и, если необходимо, обновляем.
        if not result.has_next_page and any([default_res.has_next_page, pos_res.has_next_page]):
            result.has_next_page = True

        # Обновляем next_page.
        result.next_page.data.update(default_res.next_page.data)
        result.next_page.data.update(pos_res.next_page.data)

        # Формируем общие данные позиционного мерджера.
        for i, post in enumerate(pos_res.data):
            result.data = result.data[: page_positions[i] - 1] + [post] + result.data[page_positions[i] - 1 :]

        # Проверка на возврат данных в количестве не более limit.
        if len(result.data) > limit:
            result.data = result.data[:limit]

        # Обновляем страницу для курсора пагинации мерджера.
        result.next_page.data[self.merger_id].page += 1

        return result


class MergerPercentageItem(BaseModel):
    """
    Модель позиции процентного мерджера.
    """

    percentage: int
    data: FeedTypes


class MergerPercentage(BaseFeedConfigModel):
    """
    Модель процентного мерджера.
    """

    merger_id: str
    type: Literal["merger_percentage"]
    shuffle: bool
    items: List[MergerPercentageItem]

    def get_data(
        self,
        methods_dict: Dict[str, Callable],
        limit: int,
        next_page: SmartFeedResultNextPage,
        **params: Any,
    ) -> SmartFeedResult:
        """
        Метод для получения данных в процентном соотношении из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Формируем результат процентного мерджера.
        result = SmartFeedResult(data=[], next_page=SmartFeedResultNextPage(data={}), has_next_page=False)

        for item in self.items:
            # Получаем данные из позиций процентного мерджера.
            item_result = item.data.get_data(
                methods_dict=methods_dict, limit=limit * item.percentage // 100, next_page=next_page, **params
            )

            # Добавляем данные позиции к общему результату процентного мерджера.
            result.data.extend(item_result.data)

            # Если has_next_page = False, то проверяем has_next_page у позиции и, если необходимо, обновляем.
            if not result.has_next_page and item_result.has_next_page:
                result.has_next_page = True

            # Обновляем next_page.
            result.next_page.data.update(item_result.next_page.data)

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(result.data)

        return result


class MergerPercentageGradient(BaseFeedConfigModel):
    """
    Модель процентного мерджера с градиентном.
    """

    merger_id: str
    type: Literal["merger_percentage_gradient"]
    item_from: MergerPercentageItem
    item_to: MergerPercentageItem
    step: int
    shuffle: bool

    @root_validator
    def validate_merger_percentage_gradient(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values["step"] < 1 or values["step"] > 100:
            raise ValueError('"step" must be in range from 1 to 100')
        return values

    def get_data(
        self,
        methods_dict: Dict[str, Callable],
        limit: int,
        next_page: SmartFeedResultNextPage,
        **params: Any,
    ) -> SmartFeedResult:
        """
        Метод для получения данных в процентном соотношении с градиентом из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Формируем результат процентного мерджера с градиентом.
        result = SmartFeedResult(
            data=[],
            next_page=SmartFeedResultNextPage(
                data={
                    self.merger_id: SmartFeedResultNextPageInside(
                        page=next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1,
                        after=next_page.data[self.merger_id].after if self.merger_id in next_page.data else None,
                    )
                },
            ),
            has_next_page=False,
        )

        # Формируем limit для позиций from и to в соответствии с процентом, шагом и страницей.
        limit_from = (
            limit * (self.item_from.percentage - self.step * (result.next_page.data[self.merger_id].page - 1)) // 100
        )
        limit_to = (
            limit * (self.item_to.percentage + self.step * (result.next_page.data[self.merger_id].page - 1)) // 100
        )

        # Если limit_to превысил limit, то соотношение from - to будет 0% - 100%.
        if limit_to > limit:
            limit_from = 0
            limit_to = limit

        # Получаем данные позиций from & to.
        item_from = self.item_from.data.get_data(
            methods_dict=methods_dict, limit=limit_from, next_page=next_page, **params
        )
        item_to = self.item_to.data.get_data(methods_dict=methods_dict, limit=limit_to, next_page=next_page, **params)

        # Добавляем данные позиции к общему результату процентного мерджера с градиентом.
        result.data.extend(item_from.data)
        result.data.extend(item_to.data)

        # Если has_next_page = False, то проверяем has_next_page у позиции и, если необходимо, обновляем.
        if not result.has_next_page and any([item_from.has_next_page]):
            result.has_next_page = True

        # Обновляем next_page.
        result.next_page.data.update(item_from.next_page.data)
        result.next_page.data.update(item_to.next_page.data)

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(result.data)

        # Обновляем страницу для курсора пагинации мерджера.
        result.next_page.data[self.merger_id].page += 1

        return result


class SubFeed(BaseFeedConfigModel):
    """
    Модель субфида.
    """

    subfeed_id: str
    type: Literal["subfeed"]
    method_name: str

    def get_data(
        self,
        methods_dict: Dict[str, Callable],
        limit: int,
        next_page: SmartFeedResultNextPage,
        **params: Any,
    ) -> SmartFeedResult:
        """
        Метод для получения данных из метода субфида.

        :param methods_dict: словарь с используемыми методами.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: параметры для метода.
        :return: список данных.
        """

        # Формируем next_page конкретного субфида.
        subfeed_next_page = SmartFeedResultNextPage(
            data={
                self.subfeed_id: SmartFeedResultNextPageInside(
                    page=next_page.data[self.subfeed_id].page if self.subfeed_id in next_page.data else 1,
                    after=next_page.data[self.subfeed_id].after if self.subfeed_id in next_page.data else None,
                )
            }
        )

        # Получаем результат функции клиента в формате SubFeedResult.
        result = methods_dict[self.method_name](
            subfeed_id=self.subfeed_id, limit=limit, next_page=subfeed_next_page, **params
        )
        if not isinstance(result, SmartFeedResult):
            raise TypeError(
                'SubFeed function must return "SubFeedResult" instance (from smartfeed.schemas import SubFeedResult).'
            )

        return result


class FeedConfig(BaseModel):
    """
    Модель конфигурации фида.
    """

    version: str
    feed: FeedTypes


# Update Forward Refs
MergerPositional.update_forward_refs()
MergerPercentage.update_forward_refs()
SubFeed.update_forward_refs()
MergerPercentageItem.update_forward_refs()
MergerAppend.update_forward_refs()
