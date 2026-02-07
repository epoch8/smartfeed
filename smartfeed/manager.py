from typing import Any, Dict, Optional, Union

import redis
from redis.asyncio import Redis as AsyncRedis

from .execution.context import ExecutionContext
from .execution.executor import Executor
from .schemas import FeedConfig, FeedResult, FeedResultNextPage
from tests.utils import parse_model


class FeedManager:
    """
    Класс FeedManager.
    """

    def __init__(self, config: Dict, methods_dict: Dict, redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None):
        """
        Инициализация класса FeedManager.

        :param config: конфигурация.
        :param methods_dict: словарь с используемыми методами.
        :param redis_client: объект клиента Redis (для конфигурации с view_session = True).
        """

        validate = getattr(FeedConfig, "model_validate", None)
        if validate is not None:
            self.feed_config = validate(config)
        else:
            self.feed_config = parse_model(FeedConfig, config)  # type: ignore
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

        ctx = ExecutionContext(methods_dict=self.methods_dict, user_id=user_id, redis_client=self.redis_client)
        ctx.executor = Executor()
        result = await ctx.executor.run(self.feed_config.feed, ctx, limit, next_page, **params)
        return result
