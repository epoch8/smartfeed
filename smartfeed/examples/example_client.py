import base64
from smartfeed import jsonlib as json
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from smartfeed.schemas import FeedResultClient, FeedResultNextPage, FeedResultNextPageInside


class TestClientRequest(BaseModel):
    """
    Пример модели клиентского входящего запроса.
    """

    profile_id: str = Field(...)
    limit: int = Field(...)
    next_page: Union[str, FeedResultNextPage] = Field(
        base64.urlsafe_b64encode(json.dumps({"data": {}}).encode()).decode()
    )

    model_config = ConfigDict(validate_default=True)

    @field_validator("next_page")
    @classmethod
    def validate_next_page(cls, value: Union[str, FeedResultNextPage]) -> Union[str, FeedResultNextPage]:
        if isinstance(value, str):
            payload = json.loads(base64.urlsafe_b64decode(value))
            validate = getattr(FeedResultNextPage, "model_validate", None)
            if validate is not None:
                return validate(payload)
            return FeedResultNextPage.parse_obj(payload)
        return value


class ClientMixerClass:
    """
    Пример клиентского класса ClientMixer.
    """

    @staticmethod
    async def example_method(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Пример клиентского метода.

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: массив букв "profile_id" в количестве "limit" штук.
        """

        data = [f"{user_id}_{i}" for i in range(1, 1000)]

        from_index = (data.index(next_page.after) + 1) if next_page.after else 0
        to_index = from_index + limit

        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=True)
        return result

    @staticmethod
    async def empty_method(
        user_id: str,  # pylint: disable=W0613
        limit: int,  # pylint: disable=W0613
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,  # pylint: disable=W0613
    ) -> FeedResultClient:
        """
        Пример клиентского метода, возвращающего пустые данные.

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: массив букв "profile_id" в количестве "limit" штук.
        """

        next_page.after = None
        next_page.page += 1

        result = FeedResultClient(data=[], next_page=next_page, has_next_page=False)
        return result

    @staticmethod
    async def error_method(
        user_id: str,  # pylint: disable=W0613
        limit: int,  # pylint: disable=W0613
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,  # pylint: disable=W0613
    ) -> FeedResultClient:
        """
        Пример клиентского метода, возвращающего пустые данные.

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: массив букв "profile_id" в количестве "limit" штук.
        """

        next_page.after = None
        next_page.page = int(10 / 0)

        result = FeedResultClient(data=[], next_page=next_page, has_next_page=False)
        return result

    @staticmethod
    async def doubles_method(
        user_id: str,  # pylint: disable=W0613
        limit: int,  # pylint: disable=W0613
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,  # pylint: disable=W0613
    ) -> FeedResultClient:
        """
        Пример клиентского метода, возвращающего данные с дублями.

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: массив целых чисел, равный [i for i in range(1, 11)] после удаления дублей.
        """

        data = [1, 2, 3, 4, 3, 2, 5, 6, 4, 4, 7, 8, 9, 10, 9, 9, 9]

        next_page.after = None
        next_page.page += 1

        result = FeedResultClient(data=data, next_page=next_page, has_next_page=False)
        return result

    @staticmethod
    async def keys_method(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Пример клиентского метода.

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: массив букв "profile_id" в количестве "limit" штук.
        """

        data = [{"user_id": f"{user_id}_{i%10}", "value": i} for i in range(1, 1000)]

        from_index = (data.index(next_page.after) + 1) if next_page.after else 0
        to_index = from_index + limit

        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=True)
        return result
