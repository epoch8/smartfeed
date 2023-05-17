import base64
import json
from typing import Union

from pydantic import BaseModel, Field, validator

from smartfeed.schemas import SmartFeedResult, SmartFeedResultNextPage


class LookyMixerRequest(BaseModel):
    """
    Пример модели клиентского входящего запроса.
    """

    profile_id: str = Field(...)
    limit: int = Field(...)
    next_page: Union[str, SmartFeedResultNextPage] = Field(
        base64.urlsafe_b64encode(json.dumps({"data": {}}).encode()).decode()
    )

    class Config:
        validate_all = True

    @validator("next_page")
    def validate_next_page(cls, value: Union[str, SmartFeedResultNextPage]) -> Union[str, SmartFeedResultNextPage]:
        if isinstance(value, str):
            return SmartFeedResultNextPage.parse_obj(json.loads(base64.urlsafe_b64decode(value)))
        return value


class LookyMixer:
    """
    Пример клиентского класса LookyMixer.
    """

    @staticmethod
    def looky_method(
        subfeed_id: str,
        limit: int,
        next_page: SmartFeedResultNextPage,
        profile_id: str,
    ) -> SmartFeedResult:
        """
        Пример клиентского метода.

        :param subfeed_id: ID cубфида.
        :param limit: кол-во элементов.
        :param next_page: курсор пагинации.
        :param profile_id: ID профиля.
        :return: массив букв "profile_id" в количестве "limit" штук.
        """

        page = next_page.data[subfeed_id].page

        data = [f"{profile_id}_{i}" for i in range((page - 1) * limit, page * limit)]
        next_page.data[subfeed_id].page += 1

        result = SmartFeedResult(data=data, next_page=next_page, has_next_page=True)
        return result
