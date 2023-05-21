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


class FeedResultNextPageInside(BaseModel):
    """
    Модель данных курсора пагинации конкретной позиции.

    Attributes:
        page        порядковый номер страницы.
        after       данные для пагинации клиентского метода.
    """

    page: int = 1
    after: Any = None


class FeedResultNextPage(BaseModel):
    """
    Модель курсора пагинации.

    Attributes:
        data        словарь вида "ключ: данные по пагинации", где ключ - subfeed_id или merger_id.
    """

    data: Dict[str, FeedResultNextPageInside]


class FeedResult(BaseModel):
    """
    Модель результата метода get_data() любой позиции / целого фида.

    Attributes:
        data                список данных, возвращенных мерджером / субфидом.
        next_page           курсор пагинации.
        has_next_page       флаг наличия следующей страницы данных.
    """

    data: List
    next_page: FeedResultNextPage
    has_next_page: bool


class BaseFeedConfigModel(ABC, BaseModel):
    """
    Абстрактный класс для мерджера / субфида конфигурации.
    """

    @abstractmethod
    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: параметры для метода.
        :return: список данных.
        """


class MergerAppend(BaseFeedConfigModel):
    """
    Модель append мерджера.

    Attributes:
        merger_id     уникальный ID мерджера.
        type          тип объекта - всегда "merger_append".
        items         позиции мерджера.
    """

    merger_id: str
    type: Literal["merger_append"]
    items: List[FeedTypes]

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных методом append.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных методом append.
        """

        # Формируем результат append мерджера.
        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        result_limit = limit
        for item in self.items:
            # Получаем данные из позиции мерджера.
            item_result = await item.get_data(
                methods_dict=methods_dict, user_id=user_id, limit=result_limit, next_page=next_page, **params
            )

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

    Attributes:
        merger_id       уникальный ID мерджера.
        type            тип объекта - всегда "merger_positional".
        positions       позиции для вставки из мерджера / субфида "positional" [обязателен, если нет start, end, step].
        start           начальная позиция [обязателен, если нет positions].
        end             завершающая позиция [обязателен, если нет positions].
        step            шаг позиций между "start" и "end" [обязателен, если нет positions].
        positional      мерджер / субфид из которого берутся позиционные данные.
        default         мерджер / субфид из которого берутся остальные данные.
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

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных в позиционном соотношении из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Получаем данные "default".
        default_res = await self.default.get_data(
            methods_dict=methods_dict, user_id=user_id, limit=limit, next_page=next_page, **params
        )

        # Формируем результат позиционного мерджера.
        result = FeedResult(
            data=default_res.data,
            next_page=FeedResultNextPage(
                data={
                    self.merger_id: FeedResultNextPageInside(
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
        pos_res = await self.positional.get_data(
            methods_dict=methods_dict, user_id=user_id, limit=len(page_positions), next_page=next_page, **params
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

    Attributes:
        percentage      процент позиции в мерджере.
        data            мерджер / субфид.
    """

    percentage: int
    data: FeedTypes


class MergerPercentage(BaseFeedConfigModel):
    """
    Модель процентного мерджера.

    Attributes:
        merger_id     уникальный ID мерджера.
        type          тип объекта - всегда "merger_percentage".
        shuffle       флаг для перемешивания полученных данных мерджера.
        items         позиции мерджера.
    """

    merger_id: str
    type: Literal["merger_percentage"]
    shuffle: bool
    items: List[MergerPercentageItem]

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных в процентном соотношении из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Формируем результат процентного мерджера.
        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        for item in self.items:
            # Получаем данные из позиций процентного мерджера.
            item_result = await item.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit * item.percentage // 100,
                next_page=next_page,
                **params,
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

    Attributes:
        merger_id       уникальный ID мерджера.
        type            тип объекта - всегда "merger_percentage_gradient".
        item_from       мерджер / субфид из которого начинается "перетекание" градиента.
        item_to         мерджер / субфид в который "перетекает" градиент.
        step            изменение в % соотношения из item_from в item_to.
        size_to_step    шаг для применения изменений % соотношения (например, через каждые 30 позиций).
        shuffle         флаг для перемешивания полученных данных мерджера.
    """

    merger_id: str
    type: Literal["merger_percentage_gradient"]
    item_from: MergerPercentageItem
    item_to: MergerPercentageItem
    step: int
    size_to_step: int
    shuffle: bool

    @root_validator
    def validate_merger_percentage_gradient(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values["step"] < 1 or values["step"] > 100:
            raise ValueError('"step" must be in range from 1 to 100')
        if values["size_to_step"] < 1:
            raise ValueError('"size_to_step" must be bigger than 1')
        return values

    async def _calculate_limits_and_percents(self, page: int, limit: int) -> List[Dict[str, int]]:
        """
        Метод для получения списка лимитов данных с процентным соотношением позиций item_from & item_to,
        учитывая градиентное изменение соотношений.

        :param page: порядковый номер страницы.
        :param limit: общий лимит данных для страницы.
        :return: список лимитов данных с процентным соотношением позиций item_from & item_to.
        """

        result: List = []

        percentage_from = self.item_from.percentage
        percentage_to = self.item_to.percentage
        start_position = limit * (page - 1)
        first_iter = True

        for i in range(self.size_to_step, limit * page + self.size_to_step, self.size_to_step):
            # При первой итерации и percentage_to >= 100 не меняем соотношение % между позициями.
            if not first_iter and percentage_to < 100:
                # Меняем процентное соотношение позиций на "шаг", указанный в конфигурации.
                percentage_from -= self.step
                percentage_to += self.step

                # Если процентное соотношение вышло за 100+, то устанавливаем предельные значения.
                if percentage_to > 100 or percentage_from < 0:
                    percentage_from = 0
                    percentage_to = 100

            # Если индекс итерации по величине больше стартовой позиции согласно переданной странице,
            # то начинаем обработку.
            if i > start_position:
                # Рассчитываем лимит получения данных для конкретной итерации.
                iter_limit = (limit * page - start_position) if i > limit * page else (i - start_position)
                start_position = i

                # Формируем результат для каждой итерации и добавляем в возвращаемый список, но если процентное
                # соотношение у последней итерации 0 - 100, то добавляем лимит к ней.
                if result and result[-1]["to"] >= 100:
                    result[-1]["limit"] += iter_limit
                else:
                    iter_result = {"limit": iter_limit, "from": percentage_from, "to": percentage_to}
                    result.append(iter_result)

            # Если первая итерация цикла
            if first_iter:
                first_iter = False

        return result

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных в процентном соотношении с градиентом из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Формируем результат процентного мерджера с градиентом.
        result = FeedResult(
            data=[],
            next_page=FeedResultNextPage(
                data={
                    self.merger_id: FeedResultNextPageInside(
                        page=next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1,
                        after=next_page.data[self.merger_id].after if self.merger_id in next_page.data else None,
                    )
                },
            ),
            has_next_page=False,
        )

        # Получаем список лимитов данных и соотношений согласно странице и градиенту.
        limits_and_percent = await self._calculate_limits_and_percents(
            page=result.next_page.data[self.merger_id].page,
            limit=limit,
        )

        for index, lp_data in enumerate(limits_and_percent):
            # Высчитываем лимиты для каждой позиции исходя из процентного соотношения.
            limit_from = lp_data["limit"] * lp_data["from"] // 100
            limit_to = lp_data["limit"] * lp_data["to"] // 100

            # При первой итерации используем next_page из запроса, при последующих из текущего мерджера.
            lp_next_page = next_page if index == 0 else result.next_page

            # Получаем данные из позиций в процентном соотношений.
            item_from = await self.item_from.data.get_data(
                methods_dict=methods_dict, user_id=user_id, limit=limit_from, next_page=lp_next_page, **params
            )
            item_to = await self.item_to.data.get_data(
                methods_dict=methods_dict, user_id=user_id, limit=limit_to, next_page=lp_next_page, **params
            )

            # Добавляем данные позиции к общему результату процентного мерджера с градиентом.
            result.data.extend(item_from.data)
            result.data.extend(item_to.data)

            # Обновляем next_page.
            result.next_page.data.update(item_from.next_page.data)
            result.next_page.data.update(item_to.next_page.data)

            # При последней итерации обновляем параметр has_next_page.
            if index == len(limits_and_percent) - 1:
                # Если has_next_page = False, то проверяем has_next_page у позиций и, если необходимо, обновляем.
                if any([item_from.has_next_page, item_to.has_next_page]):
                    result.has_next_page = True

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(result.data)

        # Обновляем страницу для курсора пагинации мерджера.
        result.next_page.data[self.merger_id].page += 1

        return result


class SubFeed(BaseFeedConfigModel):
    """
    Модель субфида.

    Attributes:
        subfeed_id      уникальный ID субфида.
        type            тип объекта - всегда "subfeed".
        method_name     название клиентского метода для получения данных субфида.
    """

    subfeed_id: str
    type: Literal["subfeed"]
    method_name: str

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных из метода субфида.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param params: параметры для метода.
        :return: список данных.
        """

        # Формируем next_page конкретного субфида.
        subfeed_next_page = FeedResultNextPage(
            data={
                self.subfeed_id: FeedResultNextPageInside(
                    page=next_page.data[self.subfeed_id].page if self.subfeed_id in next_page.data else 1,
                    after=next_page.data[self.subfeed_id].after if self.subfeed_id in next_page.data else None,
                )
            }
        )

        # Получаем результат функции клиента в формате SubFeedResult.
        result = await methods_dict[self.method_name](
            subfeed_id=self.subfeed_id, user_id=user_id, limit=limit, next_page=subfeed_next_page, **params
        )
        if not isinstance(result, FeedResult):
            raise TypeError(
                'SubFeed function must return "SubFeedResult" instance (from smartfeed.schemas import SubFeedResult).'
            )

        return result


class FeedConfig(BaseModel):
    """
    Модель конфигурации фида.

    Attributes:
        version             версия конфигурации.
        view_session        флаг использования механизма расчета всего фида сразу и сохранения в кэш.
        session_size        размер кэшируемого фида (limit получения данных для сохранения в кэш).
        session_live_time   срок хранения в кэше для кэшируемого фида (в секундах).
        feed                мерджер или субфид.
    """

    version: str
    view_session: bool
    session_size: int
    session_live_time: int
    feed: FeedTypes


# Update Forward Refs
MergerPositional.update_forward_refs()
MergerPercentage.update_forward_refs()
SubFeed.update_forward_refs()
MergerPercentageItem.update_forward_refs()
MergerAppend.update_forward_refs()
