import base64
import json
from typing import Optional, Union

from pydantic import BaseModel, Field, validator

from smartfeed.schemas import FeedResult, FeedResultNextPage


class LookyMixerRequest(BaseModel):
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


class LookyMixer:
    """
    Пример клиентского класса LookyMixer.
    """

    @staticmethod
    async def looky_method(
        user_id: str,
        subfeed_id: str,
        limit: int,
        next_page: FeedResultNextPage,
        limit_to_return: Optional[int] = None,
    ) -> FeedResult:
        """
        Пример клиентского метода.

        :param user_id: ID профиля.
        :param subfeed_id: ID cубфида.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param limit_to_return: ограничить кол-во результата.
        :return: массив букв "profile_id" в количестве "limit" штук.
        """

        data = [f"{user_id}_{i}" for i in range(1, 1000)]

        from_index = (data.index(next_page.data[subfeed_id].after) + 1) if next_page.data[subfeed_id].after else 0
        to_index = from_index + limit

        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.data[subfeed_id].after = result_data[-1] if result_data else None
        next_page.data[subfeed_id].page += 1

        result = FeedResult(data=result_data, next_page=next_page, has_next_page=True)
        return result
