import base64
import json
from typing import Optional, Union

from pydantic import BaseModel, Field, validator

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

    class Config:
        validate_all = True

    @validator("next_page")
    def validate_next_page(cls, value: Union[str, FeedResultNextPage]) -> Union[str, FeedResultNextPage]:
        if isinstance(value, str):
            return FeedResultNextPage.parse_obj(json.loads(base64.urlsafe_b64decode(value)))
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

    @staticmethod
    async def dedup_method_a(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Метод A для тестирования дедупликации.
        Возвращает элементы с id: a1, a2, a3, common1, common2, a4, a5...

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: список элементов с дублями.
        """
        # Элементы: a1, a2, a3, common1, common2, a4, a5, a6, common3, a7...
        data = []
        for i in range(1, 100):
            if i % 5 == 0:
                data.append({"id": f"common{i//5}", "source": "A", "value": i})
            else:
                data.append({"id": f"a{i}", "source": "A", "value": i})

        from_index = 0
        if next_page.after:
            for idx, item in enumerate(data):
                if item == next_page.after:
                    from_index = idx + 1
                    break

        to_index = from_index + limit
        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=to_index < len(data))
        return result

    @staticmethod
    async def dedup_method_b(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Метод B для тестирования дедупликации.
        Возвращает элементы с id: b1, b2, common1, b3, b4, common2...

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: список элементов с дублями.
        """
        # Элементы: b1, b2, common1, b3, b4, common2...
        data = []
        for i in range(1, 100):
            if i % 3 == 0:
                data.append({"id": f"common{i//3}", "source": "B", "value": i * 10})
            else:
                data.append({"id": f"b{i}", "source": "B", "value": i * 10})

        from_index = 0
        if next_page.after:
            for idx, item in enumerate(data):
                if item == next_page.after:
                    from_index = idx + 1
                    break

        to_index = from_index + limit
        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=to_index < len(data))
        return result

    @staticmethod
    async def dedup_method_c(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Метод C для тестирования дедупликации (третий источник).
        Возвращает элементы с id: c1, common1, c2, common2, c3, common3...

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: список элементов с дублями.
        """
        data = []
        for i in range(1, 100):
            if i % 2 == 0:
                data.append({"id": f"common{i//2}", "source": "C", "value": i * 100})
            else:
                data.append({"id": f"c{i}", "source": "C", "value": i * 100})

        from_index = 0
        if next_page.after:
            for idx, item in enumerate(data):
                if item == next_page.after:
                    from_index = idx + 1
                    break

        to_index = from_index + limit
        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=to_index < len(data))
        return result

    @staticmethod
    async def dedup_no_overlap_method(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Метод без пересечений для тестирования.

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: список уникальных элементов.
        """
        data = [{"id": f"unique_{user_id}_{i}", "value": i} for i in range(1, 100)]

        from_index = 0
        if next_page.after:
            for idx, item in enumerate(data):
                if item == next_page.after:
                    from_index = idx + 1
                    break

        to_index = from_index + limit
        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=to_index < len(data))
        return result

    @staticmethod
    async def placeholder_tours(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Метод для получения placeholder туров (для позиционного мерджера).
        Возвращает туры с id вида "placeholder_X".

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: список placeholder туров.
        """
        # Генерируем placeholder туры
        data = [{"id": f"placeholder_{i}", "type": "placeholder", "value": i} for i in range(1, 21)]

        from_index = (next_page.page - 1) * limit
        to_index = from_index + limit
        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=to_index < len(data))
        return result

    @staticmethod
    async def regular_tours(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        """
        Метод для получения обычных туров (для view session).
        Возвращает туры с id вида "tour_X", некоторые из которых дублируются с placeholder.

        :param user_id: ID профиля.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: список обычных туров с дублями.
        """
        # Генерируем обычные туры, включая дубли с placeholder
        data = []
        for i in range(1, 101):
            if i <= 10:
                # Первые 10 элементов - дубли с placeholder
                data.append({"id": f"placeholder_{i}", "type": "regular", "value": i * 10})
            else:
                # Остальные - уникальные туры
                data.append({"id": f"tour_{i}", "type": "regular", "value": i * 10})

        from_index = (next_page.page - 1) * limit
        to_index = from_index + limit
        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1

        result = FeedResultClient(data=result_data, next_page=next_page, has_next_page=to_index < len(data))
        return result
