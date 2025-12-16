# SmartFeed

Python-package для формирования ленты (Feed) из клиентских данных с заданной конфигурацией.

## Содержание:

- [Использование](#использование)
  - [Установка](#установка)
  - [Формирование конфигурации](#формирование-конфигурации)
    - [MergerDeduplication (дедупликация)](#mergerdeduplication-дедупликация)
    - [Параметры MergerDeduplication](#параметры-mergerdeduplication)
    - [Важные нюансы (сброс, cursor/redis, overfetch)](#важные-нюансы-сброс-cursorredis-overfetch)
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
        "subfeed_params": {
            "limit_to_return": 10,
        },
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
                    "raise_error": True or False (default = True),
                },
            },
        ],
    },
},
```

### MergerDeduplication (дедупликация)

MergerDeduplication — обёртка над одним дочерним узлом (merger или subfeed), которая удаляет дубли по ключу.

Ключевые свойства реализации:

- Дедупликация выполняется на уровне листьев (SubFeed), а не пост-обработкой результата мерджера.
    Это важно: вложенные мерджеры (positional/percentage/gradient/append/distribute) сохраняют свои правила смешивания.
    Если элемент удалён как дубль, MergerDeduplication «дозапросит» следующий элемент из того же источника.
- Состояние «уже видели» может храниться:
    - в курсоре (state_backend="cursor") — удобно без Redis, но курсор может расти;
    - в Redis (state_backend="redis") — удобно для большого состояния.

Пример: обернуть существующую конфигурацию фида дедупликацией:

```json
{
    "version": "1",
    "feed": {
        "merger_id": "dedup_main",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "missing_key_policy": "error",
        "state_backend": "cursor",
        "cursor_compress": true,
        "cursor_max_keys": 2000,
        "overfetch_factor": 2,
        "max_refill_loops": 20,
        "data": {
            "merger_id": "merger_percent",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 60,
                    "data": {
                        "subfeed_id": "sf_posts",
                        "type": "subfeed",
                        "method_name": "posts",
                        "dedup_priority": 10
                    }
                },
                {
                    "percentage": 40,
                    "data": {
                        "subfeed_id": "sf_ads",
                        "type": "subfeed",
                        "method_name": "ads",
                        "dedup_priority": 0
                    }
                }
            ]
        }
    }
}
```

В примере выше, если `posts` и `ads` отдают объекты с одинаковым `id`, то «побеждает» источник с большим `dedup_priority`.

### Параметры MergerDeduplication

Обязательные поля:

- `merger_id: str` — уникальный ID мерджера.
- `type: "merger_deduplication"`
- `data` — ровно один дочерний узел (subfeed или merger).

Поля дедупликации:

- `dedup_key: str | null` — имя ключа/атрибута для поиска дублей.
    - если `null`, ключом считается сам объект (подходит, когда объекты уже hashable/строковые).
- `missing_key_policy: "error" | "keep" | "drop"` (default: `"error"`)
    - `error`: выбросить ошибку, если у элемента нет `dedup_key`;
    - `keep`: сохранить элемент, даже если ключа нет;
    - `drop`: выкинуть элемент без ключа.

Состояние seen (межстраничная дедупликация):

- `state_backend: "cursor" | "redis"` (default: `"cursor"`)
- `state_ttl_seconds: int` (default: `3600`) — TTL для Redis состояния (только для backend=`redis`).
- `cursor_compress: bool` (default: `true`) — сжимать seen-состояние в cursor backend.
- `cursor_max_keys: int | null` — ограничить размер seen-состояния в cursor backend (полезно для контроля размера курсора).

Производительность/поведение:

- `overfetch_factor: int` (default: `1`) — «перезапрос» внутри листьев, чтобы быстрее добрать `limit` без множества рефиллов.
- `max_refill_loops: int` (default: `20`) — верхняя граница количества дозапросов на один лист.

### Важные нюансы (сброс, cursor/redis, overfetch)

- Сброс состояния при `page <= 0` или отсутствии курсора для `merger_id`.
    - MergerDeduplication воспринимает это как «fresh session» и очищает курсоры всех дочерних узлов.
    - Для backend=`redis` дополнительно удаляет ключ состояния в Redis.

- Если `state_backend="redis"`, нужно передать `redis_client` в `FeedManager`.
        - Ключ состояния в Redis строится как `dedup:{merger_id}:{user_id}`.
        - Можно добавить суффикс через параметр запроса `custom_deduplication_key` (или `custom_view_session_key`),
            чтобы разделять состояния для разных режимов выдачи.

- Приоритет (`dedup_priority`) — это приоритет победы при конфликте дублей, а не порядок вывода.
    - Больше `dedup_priority` → элемент «побеждает» и будет считаться seen с этим приоритетом.
    - Это поле доступно у всех узлов (merger/subfeed) и используется MergerDeduplication при дедупликации.

- overfetch работает безопасно только для «перематываемых» курсоров.
    - Сейчас overfetch включается только если `next_page.after` у листа — целочисленный offset.
    - Если `after` — строка/словарь/любой другой объект, он считается непрозрачным и overfetch не применяется.

- Главный реальный bottleneck в дедупликации — не обёртки/копии, а рефиллы.
    - Если дублей много и upstream-методы дорогие, стоит аккуратно подобрать `overfetch_factor` и `max_refill_loops`.

### Требования к клиентскому методу

Клиентский метод для получения данных должен обязательно включать в себя следующие параметры:
- **user_id: Any** - ID объекта, на который ориентируемся при получении данных субфида.
- **limit: int** - Количество возвращаемых данных.
- **next_page: FeedResultNextPageInside** - Объект курсора пагинации, формируется на стороне клиента после обработки данных.

Возвращаемый тип данных: **FeedResultClient**.

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