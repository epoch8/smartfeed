from typing import Any, Dict, Optional, Union

import aioredis
import redis

from .schemas import FeedConfig, FeedResult, FeedResultNextPage


class FeedManager:
    """
    Класс FeedManager.
    """

    def __init__(
        self, config: Dict, methods_dict: Dict, redis_client: Optional[Union[redis.Redis, aioredis.Redis]] = None
    ):
        """
        Инициализация класса FeedManager.

        :param config: конфигурация.
        :param methods_dict: словарь с используемыми методами.
        :param redis_client: объект клиента Redis (для конфигурации с view_session = True).
        """

        self.feed_config = FeedConfig.parse_obj(config)
        self.methods_dict = methods_dict
        self.redis_client = redis_client

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
            methods_dict=self.methods_dict,
            user_id=user_id,
            limit=limit,
            next_page=next_page,
            redis_client=self.redis_client,
            **params,
        )
        return result
