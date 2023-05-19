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
    Модель данных курсора пагинации конкретного субфида.
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
    Модель результата метода get_data() любой конфигурации.
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
        Метод для получения данных в позиционном соотношении из данных субфидов.

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
        Метод для получения данных в процентном соотношении из данных субфидов.

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
            data = item.data.get_data(
                methods_dict=methods_dict, limit=limit * item.percentage // 100, next_page=next_page, **params
            )
            # Добавляем данные позиции к общему результату процентного мерджера.
            result.data.extend(data.data)
            # Если has_next_page = False, то проверяем has_next_page у позиции и, если необходимо, обновляем.
            if not result.has_next_page and data.has_next_page:
                result.has_next_page = True
            # Обновляем next_page.
            result.next_page.data.update(data.next_page.data)

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(result.data)

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
        Метод для получения данных.

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
