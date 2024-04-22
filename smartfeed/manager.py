from typing import Any, Dict, Optional, Union

import redis
from redis.asyncio import Redis as AsyncRedis

from .schemas import FeedConfig, FeedResult, FeedResultNextPage


class FeedManager:
    """
    Класс FeedManager.
    """

    def __init__(self, config: Dict, api_endpoint: str, redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None):
        """
        Инициализация класса FeedManager.

        :param config: конфигурация.
        :param redis_client: объект клиента Redis (для конфигурации с view_session = True).
        """

        self.feed_config = FeedConfig.parse_obj(config)
        self.redis_client = redis_client
        self.api_endpoint = api_endpoint

    async def get_data(self, user_id: Any, limit: int, next_page: FeedResultNextPage, **params: Any) -> FeedResult:
        """
        Метод для получения данных согласно конфигурации.

        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: лимит на выдачу данных.
        :param next_page: курсор для пагинации в формате SmartFeedResultNextPage.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: результат получения данных согласно конфигурации фида.
        """
        result = await self.feed_config.feed.get_data(
            user_id=user_id,
            limit=limit,
            next_page=next_page,
            redis_client=self.redis_client,
            api_endpoint=self.api_endpoint,
            **params,
        )
        return result
