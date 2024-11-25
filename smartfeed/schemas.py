import heapq
import inspect
import json
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from random import shuffle
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Union, no_type_check

import redis
from pydantic import BaseModel, Field, root_validator
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import RedisCluster as AsyncRedisCluster

FeedTypes = Annotated[
    Union[
        "MergerAppend",
        "MergerAppendDistribute",
        "MergerPositional",
        "MergerPercentage",
        "MergerPercentageGradient",
        "MergerViewSession",
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


class FeedResultClient(BaseModel):
    """
    Модель результата клиентского метода субфида.

    Attributes:
        data                список данных, возвращенных мерджером / субфидом.
        next_page           курсор пагинации клиентского метода.
        has_next_page       флаг наличия следующей страницы данных.
    """

    data: List
    next_page: FeedResultNextPageInside
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
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
        :param params: параметры для метода.
        :return: список данных.
        """


class MergerViewSession(BaseFeedConfigModel):
    """
    Модель мерджера с кэшированием.

    Attributes:
        merger_id           уникальный ID мерджера.
        type                тип объекта - всегда "merger_view_session".
        view_session        флаг использования механизма расчета всего фида сразу и сохранения в кэш.
        session_size        размер кэшируемого фида (limit получения данных для сохранения в кэш).
        session_live_time   срок хранения в кэше для кэшируемого фида (в секундах).
        data                мерджер или субфид.
        deduplicate         флаг дедупликации (удаления дублей из сессии).
        dedup_key           название ключа или атрибута, по которому логика дедпликации найдет дубли.
        shuffle             флаг для перемешивания полученных данных мерджера.
    """

    merger_id: str
    type: Literal["merger_view_session"]
    session_size: int
    session_live_time: int
    data: FeedTypes
    deduplicate: bool = False
    dedup_key: str = None  # type: ignore
    shuffle: bool = False

    def _get_dedup_key_or_attr(self, item: Any) -> str:
        """
        Метод для получения ключа объекта кешируемой сессии.

        Если указанное в конфиге сессии название ключа имеет значение None,
        в качестве ключа вернется сам объект.
        Если название ключа не None, и для одного из объектов ни найден ни ключ, ни атрибут,
        метод выбросит AssertionError.

        :param item: объект, для которого нужен ключ.
        :return:  ключ объекта.
        """

        if not self.dedup_key:
            return item

        try:
            dedup_value = item.get(self.dedup_key)
        except AttributeError:
            dedup_value = getattr(item, self.dedup_key, None)

        assert dedup_value is not None, f"Deduplication failed: entity {item} has no key or attr {self.dedup_key}"
        return dedup_value

    def _dedup_data(self, data: List[Any]) -> List[Any]:
        """
        Метод для удаления дублей в списке data с сохранением последовательности.

        :param data: список, в котором нужно удалить дубли.
        :return: результат удаления дублей.
        """

        deduplicated_data = {self._get_dedup_key_or_attr(item): item for item in data}
        return list(deduplicated_data.values())

    async def _set_cache(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        redis_client: redis.Redis,
        cache_key: str,
        **params: Any,
    ) -> None:
        """
        Метод для кэширования данных Merger View Session.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param redis_client: объект клиента Redis.
        :param cache_key: ключ для кэширования.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: None.
        """

        result = await self.data.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=self.session_size,
            next_page=FeedResultNextPage(data={}),
            **params,
        )

        data = result.data
        if self.deduplicate:
            data = self._dedup_data(data)
        redis_client.set(name=cache_key, value=json.dumps(data), ex=self.session_live_time)

    async def _set_cache_async(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        redis_client: AsyncRedis,
        cache_key: str,
        **params: Any,
    ) -> None:
        """
        Метод для кэширования данных Merger View Session.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param redis_client: объект клиента Redis.
        :param cache_key: ключ для кэширования.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: None.
        """

        result = await self.data.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=self.session_size,
            next_page=FeedResultNextPage(data={}),
            **params,
        )

        data = result.data
        if self.deduplicate:
            data = self._dedup_data(data)
        await redis_client.set(cache_key, json.dumps(data))
        await redis_client.expire(cache_key, self.session_live_time)

    async def _get_cache(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: redis.Redis,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных Merger View Session из кэша Redis.
        При отсутствии данных в кэше - получить и сохранить.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: лимит на выдачу данных.
        :param next_page: курсор для пагинации в формате SmartFeedResultNextPage.
        :param redis_client: объект клиента Redis.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: результат получения данных согласно конфигурации фида.
        """

        # Формируем ключ для кэширования данных мерджера.
        if session_cache_key := params.get("custom_view_session_key", None):
            cache_key = f"{self.merger_id}_{user_id}_{session_cache_key}"
        else:
            cache_key = f"{self.merger_id}_{user_id}"

        # Если кэш не найден или передан пустой курсор пагинации на мерджер, обновляем данные и записываем в кэш.
        if not redis_client.exists(cache_key) or self.merger_id not in next_page.data:
            await self._set_cache(
                methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
            )

        # Получаем и возвращаем данные по мерджеру из кэша согласно пагинации.
        session_data = json.loads(redis_client.get(name=cache_key))  # type: ignore
        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
        result = FeedResult(
            data=session_data[(page - 1) * limit :][:limit],
            next_page=FeedResultNextPage(data={self.merger_id: FeedResultNextPageInside(page=page + 1, after=None)}),
            has_next_page=bool(len(session_data) > limit * page),
        )
        return result

    async def _get_cache_async(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: AsyncRedis,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных Merger View Session из кэша Redis.
        При отсутствии данных в кэше - получить и сохранить.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: лимит на выдачу данных.
        :param next_page: курсор для пагинации в формате SmartFeedResultNextPage.
        :param redis_client: объект клиента Redis.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: результат получения данных согласно конфигурации фида.
        """

        # Формируем ключ для кэширования данных мерджера.
        if session_cache_key := params.get("custom_view_session_key", None):
            cache_key = f"{self.merger_id}_{user_id}_{session_cache_key}"
        else:
            cache_key = f"{self.merger_id}_{user_id}"

        # Если кэш не найден или передан пустой курсор пагинации на мерджер, обновляем данные и записываем в кэш.
        if not await redis_client.exists(cache_key) or self.merger_id not in next_page.data:
            await self._set_cache_async(
                methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
            )

        # Получаем и возвращаем данные по мерджеру из кэша согласно пагинации.
        session_data = await redis_client.get(cache_key)
        session_data = json.loads(session_data)  # type: ignore[arg-type]
        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
        result = FeedResult(
            data=session_data[(page - 1) * limit :][:limit],
            next_page=FeedResultNextPage(data={self.merger_id: FeedResultNextPageInside(page=page + 1, after=None)}),
            has_next_page=bool(len(session_data) > limit * page),
        )
        return result

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных методом append.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
        :param params: для метода класса.
        :return: список данных методом append.
        """

        # Проверяем наличие клиента Redis в конфигурации фида.
        if not redis_client:
            raise ValueError("Redis client must be provided if using Merger View Session")

        # Формируем результат view session мерджера.
        if isinstance(redis_client, (AsyncRedis, AsyncRedisCluster)):
            result = await self._get_cache_async(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                **params,
            )
        else:
            result = await self._get_cache(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                **params,
            )

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(result.data)

        return result


class MergerAppend(BaseFeedConfigModel):
    """
    Модель append мерджера.

    Attributes:
        merger_id     уникальный ID мерджера.
        type          тип объекта - всегда "merger_append".
        items         позиции мерджера.
        shuffle       флаг для перемешивания полученных данных мерджера.
    """

    merger_id: str
    type: Literal["merger_append"]
    items: List[FeedTypes]
    shuffle: bool = False

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных методом append.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
        :param params: для метода класса.
        :return: список данных методом append.
        """

        # Формируем результат append мерджера.
        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        result_limit = limit
        for item in self.items:
            # Получаем данные из позиции мерджера.
            item_result = await item.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=result_limit,
                next_page=next_page,
                redis_client=redis_client,
                **params,
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

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(result.data)

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
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных в позиционном соотношении из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Получаем данные "default".
        default_res = await self.default.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=limit,
            next_page=next_page,
            redis_client=redis_client,
            **params,
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
            has_next_page=default_res.has_next_page,
        )

        # Получаем список позиций с учетом текущей страницы.
        positional_has_next_page = True
        page_positions = []
        available_positions = range(
            (result.next_page.data[self.merger_id].page - 1) * limit,
            (result.next_page.data[self.merger_id].page * limit) + 1,
        )
        for position in self.positions:
            if position in available_positions:
                page_positions.append(available_positions.index(position))

        # Если конечная позиция текущей страницы больше или равна MAX позиции в конфигурации, то has_next_page = False
        if max(available_positions) >= max(self.positions, default=0):
            positional_has_next_page = False

        if self.start is not None and self.end is not None and self.step is not None:
            # Если конечная позиция текущей страницы больше или равна конечной шаговой позиции, то has_next_page = False
            positional_has_next_page = not max(available_positions) >= self.end

            for position in range(self.start, self.end, self.step):
                if position in available_positions:
                    page_positions.append(available_positions.index(position))

        # Получаем данные "positional".
        pos_res = await self.positional.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=len(page_positions),
            next_page=next_page,
            redis_client=redis_client,
            **params,
        )

        # Если has_next_page = False, то проверяем has_next_page у позиции и, если необходимо, обновляем.
        if not result.has_next_page and all([positional_has_next_page, pos_res.has_next_page]):
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
    items: List[MergerPercentageItem]
    shuffle: bool = False

    @staticmethod
    async def _merge_items_data(items_data: List[List]) -> List:
        """
        Метод для получения максимально равномерно распределенных данных позиций процентного мерджера.

        :param items_data: список со списками данных из каждой позиции.
        :return: максимально равномерно распределенные данные позиций процентного мерджера.
        """

        # Формируем возвращаемый результат и список курсоров для списка каждой позиции.
        result: List = []
        cursor: List[Dict] = []

        # Получаем длину самого маленького списка и формируем курсор для каждого списка.
        min_length = min(len(item_data) for item_data in items_data) or 1
        for item_data in items_data:
            cursor.append(
                {
                    "items": item_data,
                    "current": 0,
                    "size": round(len(item_data) / min_length),
                }
            )

        # Получаем общий размер всех элементов всех списков и пока не получаем результат такого же размера
        # производим операции по распределению элементов.
        full_length = sum(len(item_data) for item_data in items_data)
        while len(result) < full_length:
            for item_cursor in cursor:
                items = item_cursor["items"]
                start = item_cursor["current"]
                end = start + item_cursor["size"] if start + item_cursor["size"] < len(items) else len(items)
                result.extend(items[start:end])
                item_cursor["current"] = end

        return result

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных в процентном соотношении из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
        :param params: для метода класса.
        :return: список данных в процентном соотношении.
        """

        # Формируем результат процентного мерджера.
        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        items_data: List = []
        for item in self.items:
            # Получаем данные из позиций процентного мерджера.
            item_result = await item.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit * item.percentage // 100,
                next_page=next_page,
                redis_client=redis_client,
                **params,
            )

            # Добавляем данные позиции в список данных позиций.
            items_data.append(item_result.data)

            # Если has_next_page = False, то проверяем has_next_page у позиции и, если необходимо, обновляем.
            if not result.has_next_page and item_result.has_next_page:
                result.has_next_page = True

            # Обновляем next_page.
            result.next_page.data.update(item_result.next_page.data)

        # Добавляем данные позиции к общему результату процентного мерджера.
        result.data = await self._merge_items_data(items_data=items_data)

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
    shuffle: bool = False

    @root_validator
    def validate_merger_percentage_gradient(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values["step"] < 1 or values["step"] > 100:
            raise ValueError('"step" must be in range from 1 to 100')
        if values["size_to_step"] < 1:
            raise ValueError('"size_to_step" must be bigger than 1')
        return values

    async def _calculate_limits_and_percents(self, page: int, limit: int) -> Dict:
        """
        Метод для получения списка лимитов данных с процентным соотношением позиций item_from & item_to,
        учитывая градиентное изменение соотношений.

        :param page: порядковый номер страницы.
        :param limit: общий лимит данных для страницы.
        :return: список лимитов данных с процентным соотношением позиций item_from & item_to.
        """

        result: Dict = {
            "limit_from": 0,
            "limit_to": 0,
            "percentages": [],
        }

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
                if result["percentages"] and result["percentages"][-1]["to"] >= 100:
                    result["limit_to"] += iter_limit
                    result["percentages"][-1]["limit"] += iter_limit
                else:
                    result["limit_from"] += iter_limit * percentage_from // 100
                    result["limit_to"] += iter_limit * percentage_to // 100
                    iter_result = {"limit": iter_limit, "from": percentage_from, "to": percentage_to}
                    result["percentages"].append(iter_result)

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
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных в процентном соотношении с градиентом из данных позиций.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
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
        limits_and_percents = await self._calculate_limits_and_percents(
            page=result.next_page.data[self.merger_id].page,
            limit=limit,
        )

        # Получаем данные из позиций в процентном соотношений.
        item_from = await self.item_from.data.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=limits_and_percents["limit_from"],
            next_page=next_page,
            redis_client=redis_client,
            **params,
        )
        item_to = await self.item_to.data.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=limits_and_percents["limit_to"],
            next_page=next_page,
            redis_client=redis_client,
            **params,
        )

        from_start_index = 0
        to_start_index = 0
        for lp_data in limits_and_percents["percentages"]:
            # Высчитываем лимиты для каждой позиции исходя из процентного соотношения.
            from_end_index = (lp_data["limit"] * lp_data["from"] // 100) + from_start_index
            to_end_index = (lp_data["limit"] * lp_data["to"] // 100) + to_start_index

            # Добавляем данные позиции к общему результату процентного мерджера с градиентом.
            result.data.extend(item_from.data[from_start_index:from_end_index])
            result.data.extend(item_to.data[to_start_index:to_end_index])

            # Обновляем стартовые индексы.
            from_start_index = from_end_index
            to_start_index = to_end_index

        # Обновляем next_page.
        result.next_page.data.update(item_from.next_page.data)
        result.next_page.data.update(item_to.next_page.data)

        # Если has_next_page = False, то проверяем has_next_page у позиций и, если необходимо, обновляем.
        if any([item_from.has_next_page, item_to.has_next_page]):
            result.has_next_page = True

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(result.data)

        # Обновляем страницу для курсора пагинации мерджера.
        result.next_page.data[self.merger_id].page += 1

        return result


class MergerAppendDistribute(BaseFeedConfigModel):
    """
    Модель мерджера, равномерно распределяющего данные по ключу.

    Attributes:
        merger_id           уникальный ID мерджера.
        type                тип объекта - всегда "merger_distribute".
        items               позиции мерджера.
        distribution_key    ключ для распределения данных мерджера.
        sorting_key         ключ сортировки.
        sorting_desc        флаг сортировки по убыванию.
    """

    merger_id: str
    type: Literal["merger_distribute"]
    items: List[FeedTypes]
    distribution_key: str
    sorting_key: Optional[str] = None
    sorting_desc: bool = False
    
    @no_type_check
    async def _uniform_distribute(self, data: list) -> list:
        # Сортируем записи глобально по `created_at` в порядке убывания
        if self.sorting_key:
            data = sorted(data, key=lambda x: x[self.sorting_key], reverse=self.sorting_desc)

        # Группируем записи по `distribution_key`
        grouped_entries = defaultdict(deque)
        for entry in data:
            grouped_entries[entry[self.distribution_key]].append(entry)
        result = []
        prev_profile_id = None
        while any(grouped_entries.values()):
            for profile_id in list(grouped_entries.keys()):
                if grouped_entries[profile_id]:
                    # Если текущий `distribution_key` отличается от предыдущего или он последний, берем его
                    if profile_id != prev_profile_id or len(grouped_entries) == 1:
                        result.append(grouped_entries[profile_id].popleft())
                        prev_profile_id = profile_id
                    if not grouped_entries[profile_id]:  # Если записи закончились, удаляем ключ из группы
                        del grouped_entries[profile_id]
                else:
                    del grouped_entries[profile_id]

        return result

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных методом append.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
        :param params: для метода класса.
        :return: список данных методом append.
        """

        # Формируем результат append мерджера.
        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        result_limit = limit
        for item in self.items:
            # Получаем данные из позиции мерджера.
            item_result = await item.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=result_limit,
                next_page=next_page,
                redis_client=redis_client,
                **params,
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

        # Распределяем данные равномерно по ключу.
        result.data = await self._uniform_distribute(result.data)
        return result


class SubFeed(BaseFeedConfigModel):
    """
    Модель субфида.

    Attributes:
        subfeed_id      уникальный ID субфида.
        type            тип объекта - всегда "subfeed".
        method_name     название клиентского метода для получения данных субфида.
        subfeed_params  статичные параметры для метода субфида.
        shuffle         флаг для перемешивания полученных данных мерджера.
    """

    subfeed_id: str
    type: Literal["subfeed"]
    method_name: str
    subfeed_params: Dict[str, Any] = {}
    raise_error: Optional[bool] = True
    shuffle: bool = False

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных из метода субфида.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param redis_client: объект клиента Redis (для конфигурации с view_session мерджером).
        :param params: параметры для метода.
        :return: список данных.
        """

        # Формируем next_page конкретного субфида.
        subfeed_next_page = FeedResultNextPageInside(
            page=next_page.data[self.subfeed_id].page if self.subfeed_id in next_page.data else 1,
            after=next_page.data[self.subfeed_id].after if self.subfeed_id in next_page.data else None,
        )

        # Формируем params для функции субфида.
        method_args = inspect.getfullargspec(methods_dict[self.method_name]).args
        method_params: Dict[str, Any] = {}
        for arg in method_args:
            if arg in params:
                method_params[arg] = params[arg]

        # Получаем результат функции клиента в формате SubFeedResult.
        try:
            method_result = await methods_dict[self.method_name](
                user_id=user_id,
                limit=limit,
                next_page=subfeed_next_page,
                **method_params,
                **self.subfeed_params,
            )
        except (Exception,) as _:
            if self.raise_error:
                raise

            method_result = FeedResultClient(
                data=[],
                next_page=subfeed_next_page,
                has_next_page=False,
            )

        if not isinstance(method_result, FeedResultClient):
            raise TypeError('SubFeed function must return "FeedResultClient" instance.')

        # Если в конфигурации указано "смешать" данные.
        if self.shuffle:
            shuffle(method_result.data)

        result = FeedResult(
            data=method_result.data,
            next_page=FeedResultNextPage(data={self.subfeed_id: method_result.next_page}),
            has_next_page=method_result.has_next_page,
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
    feed: FeedTypes


# Update Forward Refs
MergerPositional.update_forward_refs()
MergerPercentage.update_forward_refs()
SubFeed.update_forward_refs()
MergerPercentageItem.update_forward_refs()
MergerAppend.update_forward_refs()
MergerAppendDistribute.update_forward_refs()
MergerPercentageGradient.update_forward_refs()
MergerViewSession.update_forward_refs()
