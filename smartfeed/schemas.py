import base64
import inspect
import json
import logging
import zlib
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass
from random import shuffle
from typing import (
    Annotated,
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Union,
    cast,
    no_type_check,
)

import redis
from pydantic import BaseModel, Field, PrivateAttr, model_validator
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import RedisCluster as AsyncRedisCluster


def _pydantic_deep_copy(model: Any) -> Any:
    """Deep copy helper compatible with Pydantic v1 and v2."""

    if hasattr(model, "model_copy"):
        return model.model_copy(deep=True)
    return model.copy(deep=True)


class _DedupState(ABC):
    @abstractmethod
    def should_accept(self, key: str, priority: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    def record(self, key: str, priority: int) -> None:
        raise NotImplementedError

    async def prefetch(self, keys: List[str]) -> None:
        return


@dataclass
class _CursorDedupState(_DedupState):
    seen_priority_map: Dict[str, int]
    seen_updates_in_order: List[tuple[str, int]]
    seen_request_set: set[str]

    def should_accept(self, key: str, priority: int) -> bool:
        if key in self.seen_request_set:
            return False
        existing_priority = self.seen_priority_map.get(key)
        if existing_priority is not None and priority <= existing_priority:
            return False
        return True

    def record(self, key: str, priority: int) -> None:
        self.seen_priority_map[key] = priority
        self.seen_updates_in_order.append((key, priority))
        self.seen_request_set.add(key)


@dataclass
class _RedisDedupState(_DedupState):
    redis_client: Union[redis.Redis, AsyncRedis]
    redis_state_key: str
    redis_seen_cache: Dict[str, Optional[int]]
    redis_new_scores: Dict[str, int]
    seen_request_set: set[str]
    zmscore: Callable[
        [Union[redis.Redis, AsyncRedis], str, List[str]],
        Union[Awaitable[List[Optional[float]]], List[Optional[float]]],
    ]

    async def prefetch(self, keys: List[str]) -> None:
        if not keys:
            return
        unique: List[str] = []
        seen: set[str] = set()
        for k in keys:
            if k in self.seen_request_set:
                continue
            if k in self.redis_seen_cache:
                continue
            if k in seen:
                continue
            seen.add(k)
            unique.append(k)

        if not unique:
            return

        scores_result = self.zmscore(self.redis_client, self.redis_state_key, unique)
        if inspect.iscoroutine(scores_result):
            scores = await cast(Awaitable[List[Optional[float]]], scores_result)
        else:
            scores = cast(List[Optional[float]], scores_result)

        for k, s in zip(unique, scores):
            self.redis_seen_cache[k] = None if s is None else int(s)

    def should_accept(self, key: str, priority: int) -> bool:
        if key in self.seen_request_set:
            return False
        existing_priority = self.redis_seen_cache.get(key)
        if existing_priority is not None and priority <= existing_priority:
            return False
        return True

    def record(self, key: str, priority: int) -> None:
        self.seen_request_set.add(key)
        self.redis_seen_cache[key] = priority
        self.redis_new_scores[key] = max(self.redis_new_scores.get(key, 0), priority)


FeedTypes = Annotated[
    Union[
        "MergerDeduplication",
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

    # Higher value means the item should "win" deduplication when duplicates exist.
    # This is primarily used by MergerDeduplication and by mergers when a dedup wrapper is active.
    dedup_priority: int = 0

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
    ) -> List[Any]:
        """
        Метод для кэширования данных Merger View Session.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param redis_client: объект клиента Redis.
        :param cache_key: ключ для кэширования.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: обработанные данные, которые были записаны в кэш.
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
        return data

    async def _set_cache_async(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        redis_client: AsyncRedis,
        cache_key: str,
        **params: Any,
    ) -> List[Any]:
        """
        Метод для кэширования данных Merger View Session.

        :param methods_dict: словарь с используемыми методами.
        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param redis_client: объект клиента Redis.
        :param cache_key: ключ для кэширования.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: обработанные данные, которые были записаны в кэш.
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
        return data

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

        logging.info("MergerViewSession cache request for %s", cache_key)
        # Если кэш не найден или передан пустой курсор пагинации на мерджер, обновляем данные и записываем в кэш.
        if not redis_client.exists(cache_key) or self.merger_id not in next_page.data:
            logging.info("Cache miss or new session - generating fresh data for %s", cache_key)
            # Получаем свежие данные и используем их напрямую (избегаем чтение из кэша)
            session_data = await self._set_cache(
                methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
            )
        else:
            logging.info("Cache exists - attempting read from Redis for %s", cache_key)
            # Читаем из кэша только если он уже существовал
            cached_data = redis_client.get(name=cache_key)
            if cached_data is None:
                # Fallback: если кэш пропал, получаем свежие данные
                logging.info(
                    "Redis returned None for %s - falling back to fresh data (cluster replication issue)", cache_key
                )
                session_data = await self._set_cache(
                    methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
                )
            else:
                logging.info("Successfully read cached data for %s", cache_key)
                session_data = json.loads(cached_data)
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
            # Получаем свежие данные и используем их напрямую (избегаем чтение из кэша)
            session_data = await self._set_cache_async(
                methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
            )
        else:
            # Читаем из кэша только если он уже существовал
            cached_data = await redis_client.get(cache_key)
            if cached_data is None:
                # Fallback: если кэш пропал, получаем свежие данные
                logging.info(
                    "Redis returned None for %s - falling back to fresh data (cluster replication issue)", cache_key
                )
                session_data = await self._set_cache_async(
                    methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
                )
            else:
                logging.info("Successfully read cached data for %s", cache_key)
                session_data = json.loads(cached_data)
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

        # When a MergerDeduplication wrapper is active, we may need to respect dedup_priority
        # across children without changing the append output order. In that mode we fetch in
        # priority order, then concatenate in the configured order and trim to `limit`.
        dedup_active = bool(params.pop("_sf_dedup_active", False))

        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        if dedup_active:
            indexed_items = list(enumerate(self.items))
            fetch_order = sorted(indexed_items, key=lambda p: (getattr(p[1], "dedup_priority", 0), -p[0]), reverse=True)
            fetched: Dict[int, FeedResult] = {}

            for idx, item in fetch_order:
                fetched[idx] = await item.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=limit,
                    next_page=next_page,
                    redis_client=redis_client,
                    _sf_dedup_active=True,
                    **params,
                )

            for idx, _item in indexed_items:
                item_result = fetched[idx]
                result.data.extend(item_result.data)
                result.next_page.data.update(item_result.next_page.data)
                if item_result.has_next_page:
                    result.has_next_page = True

            if len(result.data) > limit:
                result.data = result.data[:limit]
        else:
            result_limit = limit
            for item in self.items:
                item_result = await item.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=result_limit,
                    next_page=next_page,
                    redis_client=redis_client,
                    **params,
                )

                result.data.extend(item_result.data)
                result_limit -= len(item_result.data)

                if not result.has_next_page and item_result.has_next_page:
                    result.has_next_page = True

                result.next_page.data.update(item_result.next_page.data)

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

    @model_validator(mode="after")
    def validate_merger_positional(self) -> "MergerPositional":
        if not self.positions and not all((self.start, self.end, self.step)):
            raise ValueError('Either "positions" or "start", "end", and "step" must be provided')
        if self.start and self.positions:
            if isinstance(self.start, int) and self.start <= max(self.positions):
                raise ValueError('"start" must be bigger than maximum value of "positions"')
        if isinstance(self.start, int) and isinstance(self.end, int):
            if self.end <= self.start:
                raise ValueError('"end" must be bigger than "start"')
        return self

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

        dedup_active = bool(params.pop("_sf_dedup_active", False))

        # Determine the merger page first (independent of children).
        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1

        positional_has_next_page = True
        page_positions: List[int] = []
        available_positions = range((page - 1) * limit, (page * limit) + 1)
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

        default_res: FeedResult
        pos_res: FeedResult

        if dedup_active and getattr(self.positional, "dedup_priority", 0) > getattr(self.default, "dedup_priority", 0):
            pos_res = await self.positional.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=len(page_positions),
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
            default_res = await self.default.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
        else:
            default_res = await self.default.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
                **params,
            )
            pos_res = await self.positional.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=len(page_positions),
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
                **params,
            )

        result = FeedResult(
            data=default_res.data,
            next_page=FeedResultNextPage(
                data={
                    self.merger_id: FeedResultNextPageInside(
                        page=page,
                        after=next_page.data[self.merger_id].after if self.merger_id in next_page.data else None,
                    )
                },
            ),
            has_next_page=default_res.has_next_page,
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

        dedup_active = bool(params.pop("_sf_dedup_active", False))

        items_data: List[List[Any]] = [[] for _ in self.items]
        results: List[Optional[FeedResult]] = [None for _ in self.items]

        indexed_items = list(enumerate(self.items))
        fetch_order = indexed_items
        if dedup_active:
            fetch_order = sorted(
                indexed_items,
                key=lambda p: (getattr(p[1].data, "dedup_priority", 0), -p[0]),
                reverse=True,
            )

        for idx, item in fetch_order:
            item_result = cast(
                FeedResult,
                await item.data.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=limit * item.percentage // 100,
                    next_page=next_page,
                    redis_client=redis_client,
                    _sf_dedup_active=dedup_active,
                    **params,
                ),
            )

            results[idx] = item_result

        for idx, result_item in enumerate(results):
            assert result_item is not None
            items_data[idx] = result_item.data

            if not result.has_next_page and result_item.has_next_page:
                result.has_next_page = True
            result.next_page.data.update(result_item.next_page.data)

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

    @model_validator(mode="after")
    def validate_merger_percentage_gradient(self) -> "MergerPercentageGradient":
        if self.step < 1 or self.step > 100:
            raise ValueError('"step" must be in range from 1 to 100')
        if self.size_to_step < 1:
            raise ValueError('"size_to_step" must be bigger than 1')
        return self

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

        dedup_active = bool(params.pop("_sf_dedup_active", False))

        from_priority = getattr(self.item_from.data, "dedup_priority", 0)
        to_priority = getattr(self.item_to.data, "dedup_priority", 0)

        if dedup_active and to_priority > from_priority:
            item_to = await self.item_to.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_to"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
            item_from = await self.item_from.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_from"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
        else:
            item_from = await self.item_from.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_from"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
                **params,
            )
            item_to = await self.item_to.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_to"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
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

        dedup_active = bool(params.pop("_sf_dedup_active", False))

        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        if dedup_active:
            indexed_items = list(enumerate(self.items))
            fetch_order = sorted(indexed_items, key=lambda p: (getattr(p[1], "dedup_priority", 0), -p[0]), reverse=True)
            fetched: Dict[int, FeedResult] = {}

            for idx, item in fetch_order:
                fetched[idx] = await item.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=limit,
                    next_page=next_page,
                    redis_client=redis_client,
                    _sf_dedup_active=True,
                    **params,
                )

            for idx, _item in indexed_items:
                item_result = fetched[idx]
                result.data.extend(item_result.data)
                result.next_page.data.update(item_result.next_page.data)
                if item_result.has_next_page:
                    result.has_next_page = True

            if len(result.data) > limit:
                result.data = result.data[:limit]
        else:
            result_limit = limit
            for item in self.items:
                item_result = await item.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=result_limit,
                    next_page=next_page,
                    redis_client=redis_client,
                    **params,
                )

                result.data.extend(item_result.data)
                result_limit -= len(item_result.data)

                if not result.has_next_page and item_result.has_next_page:
                    result.has_next_page = True

                result.next_page.data.update(item_result.next_page.data)

                if result_limit <= 0:
                    break

        # Распределяем данные равномерно по ключу.
        result.data = await self._uniform_distribute(result.data)
        return result


class MergerDeduplication(BaseFeedConfigModel):
    """Merger that deduplicates while preserving child mixing/position semantics.

    This merger acts as a wrapper around exactly one child feed node.
    Deduplication is applied at the leaf SubFeed method level with a shared seen-set.
    This lets nested mergers (positional/percentage/gradient/etc.) keep their slot rules:
    duplicates are skipped by fetching additional items from the *same* leaf source.
    """

    merger_id: str
    type: Literal["merger_deduplication"]
    data: FeedTypes

    dedup_key: Optional[str] = None
    missing_key_policy: Literal["error", "keep", "drop"] = "error"

    state_backend: Literal["cursor", "redis"] = "cursor"
    state_ttl_seconds: int = 3600
    cursor_compress: bool = True
    cursor_max_keys: Optional[int] = None

    overfetch_factor: int = 1

    max_refill_loops: int = 20

    _descendant_cursor_keys_cache: Optional[set[str]] = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_merger_deduplication(self) -> "MergerDeduplication":
        if self.overfetch_factor < 1:
            raise ValueError('"overfetch_factor" must be >= 1')
        if self.max_refill_loops < 1:
            raise ValueError('"max_refill_loops" must be >= 1')
        return self

    def _collect_descendant_cursor_keys(self, feed: BaseFeedConfigModel) -> set[str]:
        keys: set[str] = set()

        subfeed_id = getattr(feed, "subfeed_id", None)
        if isinstance(subfeed_id, str) and subfeed_id:
            keys.add(subfeed_id)

        merger_id = getattr(feed, "merger_id", None)
        if isinstance(merger_id, str) and merger_id:
            keys.add(merger_id)

        # Recurse into known child containers across existing feed types.
        child: Any
        for attr_name in ("data", "positional", "default"):
            child = getattr(feed, attr_name, None)
            if isinstance(child, BaseFeedConfigModel):
                keys.update(self._collect_descendant_cursor_keys(child))

        for attr_name in ("item_from", "item_to"):
            child = getattr(feed, attr_name, None)
            inner = getattr(child, "data", None)
            if isinstance(inner, BaseFeedConfigModel):
                keys.update(self._collect_descendant_cursor_keys(inner))

        items = getattr(feed, "items", None)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, BaseFeedConfigModel):
                    keys.update(self._collect_descendant_cursor_keys(item))
                    continue

                inner = getattr(item, "data", None)
                if isinstance(inner, BaseFeedConfigModel):
                    keys.update(self._collect_descendant_cursor_keys(inner))

        return keys

    def _get_descendant_cursor_keys_cached(self) -> set[str]:
        cached = self._descendant_cursor_keys_cache
        if cached is None:
            cached = self._collect_descendant_cursor_keys(self.data)
            self._descendant_cursor_keys_cache = cached
        return cached

    def _reset_descendant_cursors(self, next_page: FeedResultNextPage) -> None:
        descendant_keys = self._get_descendant_cursor_keys_cached()
        for key in descendant_keys:
            next_page.data.pop(key, None)

    def _normalize_key(self, value: Any) -> str:
        if isinstance(value, (str, int)):
            return str(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True, default=str)
        return str(value)

    def _extract_dedup_value(self, item: Any) -> Any:
        if not self.dedup_key:
            return item

        try:
            value = item.get(self.dedup_key)
        except AttributeError:
            value = getattr(item, self.dedup_key, None)

        if value is None and self.missing_key_policy == "error":
            raise AssertionError(f"Deduplication failed: entity {item} has no key or attr {self.dedup_key}")
        return value

    def _get_entity_key(self, entity: Any) -> Optional[str]:
        """Return normalized dedup key for entity, or None if entity should be skipped."""

        raw_value = self._extract_dedup_value(entity)
        if raw_value is None:
            if self.missing_key_policy == "drop":
                return None
            if self.missing_key_policy == "keep":
                raw_value = ("__missing__", id(entity))
        return self._normalize_key(raw_value)

    def _compute_overfetch_params(self, *, remaining: int, next_after: Any) -> tuple[bool, int, Optional[int]]:
        """Compute safe overfetch params.

        Overfetch is only safe when `after` is an integer offset (so we can rewind).

        Returns: (can_overfetch, request_limit, start_after)
        """

        can_overfetch = isinstance(next_after, int)
        request_limit = max(1, remaining)
        if can_overfetch and self.overfetch_factor > 1:
            request_limit = max(1, remaining * self.overfetch_factor)
        start_after: Optional[int] = int(next_after) if can_overfetch else None
        return can_overfetch, request_limit, start_after

    def _iter_subfeeds(self, feed: BaseFeedConfigModel) -> Iterator["SubFeed"]:
        if isinstance(feed, SubFeed):
            yield feed
            return

        for attr_name in ("data", "positional", "default"):
            inner = getattr(feed, attr_name, None)
            if isinstance(inner, BaseFeedConfigModel):
                yield from self._iter_subfeeds(inner)

        for attr_name in ("item_from", "item_to"):
            wrapper = getattr(feed, attr_name, None)
            inner = getattr(wrapper, "data", None)
            if isinstance(inner, BaseFeedConfigModel):
                yield from self._iter_subfeeds(inner)

        items = getattr(feed, "items", None)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, BaseFeedConfigModel):
                    yield from self._iter_subfeeds(item)
                    continue
                inner = getattr(item, "data", None)
                if isinstance(inner, BaseFeedConfigModel):
                    yield from self._iter_subfeeds(inner)

    def _register_wrapped_subfeed_method(
        self,
        *,
        subfeed: "SubFeed",
        original_methods_dict: Dict[str, Callable],
        rewritten_methods_dict: Dict[str, Callable],
        dedup_state: "_DedupState",
    ) -> None:
        original_name = subfeed.method_name
        original_method = original_methods_dict[original_name]
        unique_name = f"__dedup__{self.merger_id}__{subfeed.subfeed_id}"

        # Idempotency: if the same subfeed id appears multiple times, don't re-wrap.
        if unique_name in rewritten_methods_dict:
            subfeed.method_name = unique_name
            return

        subfeed.method_name = unique_name
        leaf_priority = int(getattr(subfeed, "dedup_priority", 0))

        wrapped = self._make_wrapped_leaf_method(
            original_method=original_method,
            dedup_state=dedup_state,
            leaf_priority=leaf_priority,
        )
        setattr(wrapped, "_smartfeed_original", original_method)
        rewritten_methods_dict[unique_name] = wrapped

    def _make_wrapped_leaf_method(
        self,
        *,
        original_method: Callable,
        dedup_state: "_DedupState",
        leaf_priority: int,
    ) -> Callable:
        async def _wrapped_method(
            user_id: Any,
            limit: int,
            next_page: FeedResultNextPageInside,
            **kw: Any,
        ) -> FeedResultClient:
            collected: List[Any] = []
            upstream_has_next_page = False

            loops = 0
            while len(collected) < limit and loops < self.max_refill_loops:
                loops += 1
                before_len = len(collected)

                remaining = limit - len(collected)
                can_overfetch, request_limit, start_after = self._compute_overfetch_params(
                    remaining=remaining,
                    next_after=next_page.after,
                )

                method_result = await original_method(user_id=user_id, limit=request_limit, next_page=next_page, **kw)
                if not isinstance(method_result, FeedResultClient):
                    raise TypeError('SubFeed function must return "FeedResultClient" instance.')

                upstream_has_next_page = upstream_has_next_page or method_result.has_next_page

                inspected_count = 0

                # Backend-specific optimization: Redis batches zmscore.
                # For cursor backend, prefetch is a no-op and we avoid the extra pass entirely.
                keys_by_index: Optional[List[Optional[str]]] = None
                if isinstance(dedup_state, _RedisDedupState):
                    keys_by_index = []
                    batch_keys: List[str] = []
                    for entity in method_result.data:
                        key = self._get_entity_key(entity)
                        keys_by_index.append(key)
                        if key is not None:
                            batch_keys.append(key)
                    await dedup_state.prefetch(batch_keys)

                for idx, entity in enumerate(method_result.data, start=1):
                    inspected_count = idx

                    key = keys_by_index[idx - 1] if keys_by_index is not None else self._get_entity_key(entity)
                    if key is None:
                        continue

                    if not dedup_state.should_accept(key, leaf_priority):
                        continue

                    collected.append(entity)
                    dedup_state.record(key, leaf_priority)

                    if len(collected) >= limit:
                        break

                if len(collected) == before_len:
                    # No progress this loop. Stop if upstream is exhausted.
                    if not method_result.has_next_page:
                        break

                # If we oversampled with a simple integer cursor, rewind to the point we actually consumed.
                # This prevents skipping un-inspected items that were fetched but not needed.
                if can_overfetch and request_limit > remaining and start_after is not None:
                    end_after = next_page.after
                    if isinstance(end_after, int) and end_after == start_after + len(method_result.data):
                        next_page.after = start_after + inspected_count

            return FeedResultClient(data=collected, next_page=next_page, has_next_page=upstream_has_next_page)

        return _wrapped_method

    def _decode_seen_from_cursor(self, next_page: FeedResultNextPage) -> Dict[str, int]:
        entry = next_page.data.get(self.merger_id)
        if not entry or entry.after is None:
            return {}

        after = entry.after
        if isinstance(after, dict) and "z" in after:
            payload = base64.urlsafe_b64decode(after["z"].encode())
            raw = zlib.decompress(payload).decode()
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return {str(k): int(v) for k, v in decoded.items()}
            if isinstance(decoded, list):
                # v2: list of [key, priority] entries
                seen_map: Dict[str, int] = {}
                for entry_item in decoded:
                    if isinstance(entry_item, (list, tuple)) and len(entry_item) == 2:
                        seen_map[str(entry_item[0])] = int(entry_item[1])
                    else:
                        seen_map[str(entry_item)] = 0
                return seen_map
            return {}
        if isinstance(after, dict) and "seen" in after:
            return {str(k): 0 for k in list(after["seen"])}
        if isinstance(after, list):
            return {str(k): 0 for k in list(after)}
        if isinstance(after, dict):
            # v2 uncompressed map
            return {str(k): int(v) for k, v in after.items() if k not in {"v", "c", "n"}}
        return {}

    def _encode_seen_for_cursor(self, seen_updates_in_order: List[tuple[str, int]]) -> Any:
        if self.cursor_max_keys is not None:
            seen_updates_in_order = seen_updates_in_order[-self.cursor_max_keys :]

        if not self.cursor_compress:
            return {"v": 2, "seen": [[k, p] for k, p in seen_updates_in_order]}

        raw = json.dumps([[k, p] for k, p in seen_updates_in_order]).encode()
        compressed = zlib.compress(raw)
        return {
            "v": 2,
            "c": "zlib+base64",
            "n": len(seen_updates_in_order),
            "z": base64.urlsafe_b64encode(compressed).decode(),
        }

    async def _redis_zmscore(
        self,
        redis_client: Union[redis.Redis, AsyncRedis],
        key: str,
        members: List[str],
    ) -> List[Optional[float]]:
        """Batch zscore for multiple members.

        Falls back to pipelined zscore when zmscore isn't available.
        """

        if not members:
            return []

        zmscore_fn = getattr(redis_client, "zmscore", None)
        if zmscore_fn is not None:
            res = zmscore_fn(key, members)
            if inspect.iscoroutine(res):
                res = await res
            # redis-py returns list[Optional[float]]
            return [None if v is None else float(v) for v in list(res)]

        pipe = redis_client.pipeline()
        for m in members:
            pipe.zscore(key, m)
        res = pipe.execute()
        if inspect.iscoroutine(res):
            res = await res
        return [None if v is None else float(v) for v in list(res)]

    async def _redis_zadd_and_expire(
        self,
        redis_client: Union[redis.Redis, AsyncRedis],
        key: str,
        member_scores: Dict[str, int],
    ) -> None:
        if not member_scores:
            return
        res = redis_client.zadd(key, mapping={m: float(s) for m, s in member_scores.items()})
        if inspect.iscoroutine(res):
            await res

        expire_res = redis_client.expire(key, self.state_ttl_seconds)
        if inspect.iscoroutine(expire_res):
            await expire_res

    def _build_redis_state_key(self, user_id: Any, params: Dict[str, Any]) -> str:
        suffix = params.get("custom_deduplication_key") or params.get("custom_view_session_key")
        if suffix:
            return f"dedup:{self.merger_id}:{user_id}:{suffix}"
        return f"dedup:{self.merger_id}:{user_id}"

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        if limit <= 0:
            return FeedResult(data=[], next_page=next_page, has_next_page=False)

        # Treat an explicit "page 0" (or missing cursor for this merger) as a fresh session.
        # This allows clients to restart the feed (e.g., full reload) without carrying over seen state.
        entry = next_page.data.get(self.merger_id)
        requested_page = entry.page if entry is not None else None
        is_fresh_session = requested_page is None or (isinstance(requested_page, int) and requested_page <= 0)

        if self.state_backend == "redis" and not redis_client:
            raise ValueError("Redis client must be provided if using MergerDeduplication with state_backend=redis")

        working_next_page = _pydantic_deep_copy(next_page)

        if is_fresh_session:
            # Reset cursors for all descendants under this merger so upstream nodes also restart.
            self._reset_descendant_cursors(working_next_page)

        # Shared dedup state (cross-page)
        seen_priority_map: Dict[str, int] = {}
        seen_updates_in_order: List[tuple[str, int]] = []
        if self.state_backend == "cursor" and not is_fresh_session:
            seen_priority_map = self._decode_seen_from_cursor(next_page)

        # Always maintain a per-request seen set to prevent duplicates within a single get_data() call.
        seen_request_set: set[str] = set(seen_priority_map.keys())

        redis_state_key = ""
        redis_new_scores: Dict[str, int] = {}
        redis_seen_cache: Dict[str, Optional[int]] = {}
        if self.state_backend == "redis" and redis_client:
            redis_state_key = self._build_redis_state_key(user_id=user_id, params=params)
            if is_fresh_session:
                # Drop state for a full restart.
                deleted = redis_client.delete(redis_state_key)
                if inspect.iscoroutine(deleted):
                    await deleted

        # Create a single state helper shared across all leaf wrappers.
        if self.state_backend == "cursor":
            dedup_state: _DedupState = _CursorDedupState(
                seen_priority_map=seen_priority_map,
                seen_updates_in_order=seen_updates_in_order,
                seen_request_set=seen_request_set,
            )
        else:
            assert redis_client is not None
            dedup_state = _RedisDedupState(
                redis_client=redis_client,
                redis_state_key=redis_state_key,
                redis_seen_cache=redis_seen_cache,
                redis_new_scores=redis_new_scores,
                seen_request_set=seen_request_set,
                zmscore=self._redis_zmscore,
            )

        # Preserve inner merger ordering/mixing semantics by deduplicating at the leaf method level
        # with a shared seen-set.
        original_methods_dict = methods_dict

        # Create a deep copy of the child tree and rewrite each SubFeed to call a unique wrapper
        # so we can associate a dedup_priority with each leaf.
        child = self.data
        child = _pydantic_deep_copy(child)

        rewritten_methods_dict = dict(original_methods_dict)

        for sf in self._iter_subfeeds(child):
            self._register_wrapped_subfeed_method(
                subfeed=sf,
                original_methods_dict=original_methods_dict,
                rewritten_methods_dict=rewritten_methods_dict,
                dedup_state=dedup_state,
            )

        child_result = await child.get_data(
            methods_dict=rewritten_methods_dict,
            user_id=user_id,
            limit=limit,
            next_page=working_next_page,
            redis_client=redis_client,
            _sf_dedup_active=True,
            **params,
        )

        if self.state_backend == "redis" and redis_client:
            await self._redis_zadd_and_expire(redis_client, redis_state_key, redis_new_scores)

        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
        merger_after: Any = None
        if self.state_backend == "cursor":
            merger_after = self._encode_seen_for_cursor(seen_updates_in_order)

        result_next_page = _pydantic_deep_copy(child_result.next_page)
        result_next_page.data[self.merger_id] = FeedResultNextPageInside(page=page + 1, after=merger_after)

        return FeedResult(data=child_result.data, next_page=result_next_page, has_next_page=child_result.has_next_page)


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
        method = methods_dict[self.method_name]
        method_spec = getattr(method, "_smartfeed_original", method)
        method_args = inspect.getfullargspec(method_spec).args
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
def _rebuild_model(model: Any) -> None:
    if hasattr(model, "model_rebuild"):
        model.model_rebuild()
    else:
        model.update_forward_refs()


_rebuild_model(MergerPositional)
_rebuild_model(MergerPercentage)
_rebuild_model(SubFeed)
_rebuild_model(MergerPercentageItem)
_rebuild_model(MergerAppend)
_rebuild_model(MergerAppendDistribute)
_rebuild_model(MergerPercentageGradient)
_rebuild_model(MergerViewSession)
_rebuild_model(MergerDeduplication)
