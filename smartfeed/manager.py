import json
from typing import Any, Dict, Optional

import redis

from .schemas import FeedConfig, FeedResult, FeedResultNextPage, FeedResultNextPageInside


class FeedManager:
    """
    Класс FeedManager.
    """

    def __init__(self, config: Dict, methods_dict: Dict, redis_client: Optional[redis.Redis] = None):
        """
        Инициализация класса FeedManager.

        :param config: конфигурация.
        :param methods_dict: словарь с используемыми методами.
        :param redis_client: объект клиента Redis (для конфигурации с view_session = True).
        """

        self.feed_config = FeedConfig.parse_obj(config)
        self.methods_dict = methods_dict
        self.redis_client = redis_client
        if self.feed_config.view_session and not self.redis_client:
            raise ValueError("Redis client must be provided if view_session = True")

    async def _set_cache(self, user_id: Any, redis_client: redis.Redis, **params: Any) -> None:
        """
        Метод для сохранения и возврата данных согласно конфигурации в кэш Redis.

        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param redis_client: объект клиента Redis.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: результат получения данных согласно конфигурации фида.
        """

        result = await self.feed_config.feed.get_data(
            methods_dict=self.methods_dict,
            user_id=user_id,
            limit=self.feed_config.session_size,
            next_page=FeedResultNextPage(data={}),
            **params,
        )
        redis_client.set(name=user_id, value=json.dumps(result.data), ex=self.feed_config.session_live_time)

    async def _get_cache(
        self,
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: redis.Redis,
        **params: Any,
    ) -> FeedResult:
        """
        Метод для получения данных согласно конфигурации из кэша Redis.
        При отсутствии данных в кэше - получить и сохранить.

        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: лимит на выдачу данных.
        :param next_page: курсор для пагинации в формате SmartFeedResultNextPage.
        :param redis_client: объект клиента Redis.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: результат получения данных согласно конфигурации фида.
        """

        if not redis_client.exists(user_id):
            await self._set_cache(user_id=user_id, redis_client=redis_client, **params)

        session_data = json.loads(redis_client.get(name=user_id))  # type: ignore
        page = next_page.data["session_feed_data"].page if "session_feed_data" in next_page.data else 1
        result = FeedResult(
            data=session_data[(page - 1) * limit :][:limit],
            next_page=FeedResultNextPage(
                data={"session_feed_data": FeedResultNextPageInside(page=page + 1, after=None)}
            ),
            has_next_page=bool(len(session_data) > limit * page),
        )
        return result

    async def get_data(self, user_id: Any, limit: int, next_page: FeedResultNextPage, **params: Any) -> FeedResult:
        """
        Метод для получения данных согласно конфигурации.

        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: лимит на выдачу данных.
        :param next_page: курсор для пагинации в формате SmartFeedResultNextPage.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: результат получения данных согласно конфигурации фида.
        """

        if self.feed_config.view_session:
            if not self.redis_client:
                raise ValueError("Redis client must be provided if view_session = True")

            result = await self._get_cache(
                user_id=user_id, limit=limit, next_page=next_page, redis_client=self.redis_client, **params
            )
        else:
            result = await self.feed_config.feed.get_data(
                methods_dict=self.methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                **params,
            )
        return result
