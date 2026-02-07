import base64
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from smartfeed import jsonlib as json
from smartfeed.pydantic_compat import parse_model
from smartfeed.schemas import FeedResultClient, FeedResultNextPage, FeedResultNextPageInside


class TestClientRequest(BaseModel):
    """Example client request model."""

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
            return parse_model(FeedResultNextPage, payload)

        return value


class ClientMixerClass:
    """Example client methods for SmartFeed."""

    @staticmethod
    async def example_method(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        data = [f"{user_id}_{i}" for i in range(1, 1000)]

        from_index = (data.index(next_page.after) + 1) if next_page.after else 0
        to_index = from_index + limit

        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1
        return FeedResultClient(data=result_data, next_page=next_page, has_next_page=True)

    @staticmethod
    async def empty_method(
        user_id: str,  # pylint: disable=W0613
        limit: int,  # pylint: disable=W0613
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,  # pylint: disable=W0613
    ) -> FeedResultClient:
        next_page.after = None
        next_page.page += 1
        return FeedResultClient(data=[], next_page=next_page, has_next_page=False)

    @staticmethod
    async def error_method(
        user_id: str,  # pylint: disable=W0613
        limit: int,  # pylint: disable=W0613
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,  # pylint: disable=W0613
    ) -> FeedResultClient:
        next_page.after = None
        next_page.page = int(10 / 0)
        return FeedResultClient(data=[], next_page=next_page, has_next_page=False)

    @staticmethod
    async def doubles_method(
        user_id: str,  # pylint: disable=W0613
        limit: int,  # pylint: disable=W0613
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,  # pylint: disable=W0613
    ) -> FeedResultClient:
        data = [1, 2, 3, 4, 3, 2, 5, 6, 4, 4, 7, 8, 9, 10, 9, 9, 9]

        next_page.after = None
        next_page.page += 1
        return FeedResultClient(data=data, next_page=next_page, has_next_page=False)

    @staticmethod
    async def keys_method(
        user_id: str,
        limit: int,
        next_page: FeedResultNextPageInside,
        limit_to_return: Optional[int] = None,
    ) -> FeedResultClient:
        data = [{"user_id": f"{user_id}_{i%10}", "value": i} for i in range(1, 1000)]

        from_index = (data.index(next_page.after) + 1) if next_page.after else 0
        to_index = from_index + limit

        result_data = data[from_index:to_index]

        if isinstance(limit_to_return, int) and limit_to_return > 0:
            result_data = result_data[:limit_to_return]

        next_page.after = result_data[-1] if result_data else None
        next_page.page += 1
        return FeedResultClient(data=result_data, next_page=next_page, has_next_page=True)
