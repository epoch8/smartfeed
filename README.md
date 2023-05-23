# SmartFeed

Python-package для формирования ленты (Feed) из клиентских данных с заданной конфигурацией.

## Содержание:

- [Использование](#использование)
  - [Установка](#установка)
  - [Формирование конфигурации](#формирование-конфигурации)
  - [Требования к клиентскому методу](#требования-к-клиентскому-методу)
  - [Запуск](#запуск)

## Использование

### Установка

```
poetry add git+ssh://git@github.com:epoch8/looky-timeline.git
```

### Формирование конфигурации

Конфигурация каждого фида должна быть словарем следующего вида:
```
"version": "1",
"view_session": True or False,
"session_size": 800,  # по умолчанию 100
"session_live_time": 500,  # по умолчанию 300
"feed": {
    "merger_id": "merger_pos",
    "type": "merger_positional",
    "positions": [1, 3, 15],
    "start": 17,
    "end": 200,
    "step": 2,
    "positional": {
        "subfeed_id": "sf_positional",
        "type": "subfeed",
        "method_name": "ads",
    },
    "default": {
        "merger_id": "merger_percent",
        "type": "merger_percentage",
        "shuffle": False,
        "items": [
            {
                "percentage": 40,
                "data": {
                    "subfeed_id": "sf_1_default_merger_of_main",
                    "type": "subfeed",
                    "method_name": "followings",
                },
            },
            {
                "percentage": 60,
                "data": {
                    "subfeed_id": "sf_2_default_merger_of_main",
                    "type": "subfeed",
                    "method_name": "ads",
                },
            },
        ],
    },
},
```

### Требования к клиентскому методу

Клиентский метод для получения данных должен обязательно включать в себя следующие параметры:
- **user_id: Any** - ID объекта, на который ориентируемся при получении данных субфида.
- **subfeed_id: str** - ID использующего субфида.
- **limit: int** - Количество возвращаемых данных.
- **next_page: FeedResultNextPage** - Объект курсора пагинации, формируется на стороне клиента после обработки данных.

Возвращаемый тип данных: **FeedResult**.

### Запуск
Для получения ленты с помощью SmartFeed нужно выполнить следующий код:

```
from smartfeed.manager import FeedManager
from smartfeed.schemas import FeedResult, FeedResultNextPage, FeedResultNextPageInside

from client.services import ClientService

config = {} # получаем конфигурацию фида
methods_dict = {
    "method_1": ClientService().method_1,
    "method_2": ClientService().method_2,
    # и т.д.
}
# для конфигурации view_session = False,
# Redis передавать небязательно
redis_client = redis.Redis()

feed_manager = FeedManager(
    config=config,
    methods_dict=methods_dict,
    redis_client=redis_client,
)

user_id = "sjjdj?" # любой тип данных
limit = 100
next_page = FeedResultNextPage(
    data={
        "subfeed_id": FeedResultNextPageInside(page=1, after=None),
    }
)
data: FeedResult = await feed_manager.get_data(
    user_id=user_id,
    limit=limit,
    next_page=next_page,
)
```