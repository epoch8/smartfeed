from typing import Any, Dict

from .schemas import FeedConfig, SmartFeedResult, SmartFeedResultNextPage


class FeedManager:
    """
    Класс FeedManager.
    """

    def __init__(self, config: Dict, methods_dict: Dict):
        """
        Инициализация класса FeedManager.

        :param config: конфигурация.
        :param methods_dict: словарь с используемыми методами.
        :return: объект Feed.
        """

        self.feed_config = FeedConfig.parse_obj(config)
        self.methods_dict = methods_dict

    async def get_data(
        self,
        user_id: Any,
        limit: int,
        next_page: SmartFeedResultNextPage,
        **params: Any,
    ) -> SmartFeedResult:
        """
        Метод для получения данных согласно конфигурации.

        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: лимит на выдачу данных.
        :param next_page: курсор для пагинации в формате SmartFeedResultNextPage.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return:
        """

        result = await self.feed_config.feed.get_data(
            methods_dict=self.methods_dict,
            user_id=user_id,
            limit=limit,
            next_page=next_page,
            **params,
        )
        return result
