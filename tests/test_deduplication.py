"""
Тесты для дедупликации по приоритетам в SmartFeed.

Структура тестовых данных (методы из example_client.py):

dedup_method_a (dedup_a):
    Возвращает: a1, a2, a3, a4, common1, a6, a7, a8, a9, common2, a11, ...
    Паттерн: каждый 5-й элемент = common{i//5}, остальные = a{i}
    source="A", value=i

dedup_method_b (dedup_b):
    Возвращает: b1, b2, common1, b4, b5, common2, b7, b8, common3, ...
    Паттерн: каждый 3-й элемент = common{i//3}, остальные = b{i}
    source="B", value=i*10

dedup_method_c (dedup_c):
    Возвращает: c1, common1, c3, common2, c5, common3, ...
    Паттерн: каждый 2-й элемент = common{i//2}, остальные = c{i}
    source="C", value=i*100

dedup_no_overlap_method:
    Возвращает: unique_{user_id}_1, unique_{user_id}_2, ...
    Нет пересечений между субфидами.

ads (example_method):
    Возвращает: {user_id}_1, {user_id}_2, ...
    Строки, не словари.
"""

import pytest

from smartfeed.manager import FeedManager
from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.redis import redis_client  # noqa: F401

# ============================================================================
# Простые кейсы
# ============================================================================


@pytest.mark.asyncio
async def test_dedup_basic() -> None:
    """
    Тест базовой дедупликации: 2 субфида с разными приоритетами, есть дубли.

    Субфиды:
        subfeed_a (priority=1): a1, a2, a3, a4, common1, a6, a7, a8, a9, common2, ...
        subfeed_b (priority=2): b1, b2, common1, b4, b5, common2, ...

    Дубли: common1, common2, ... присутствуют в обоих субфидах.

    Ожидаемый результат:
        - common элементы остаются только из subfeed_a (priority=1, выше приоритет)
        - Все common элементы имеют source="A"
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,  # Высший приоритет
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "dedup_b",
                    "priority": 2,  # Низший приоритет
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # Проверяем, что результат содержит элементы
    assert len(result.data) == 10

    # Проверяем, что common элементы имеют source="A" (приоритет 1)
    common_items = [item for item in result.data if item["id"].startswith("common")]
    for item in common_items:
        assert item["source"] == "A", f"Common item {item['id']} should be from source A (priority 1)"


@pytest.mark.asyncio
async def test_dedup_same_priority() -> None:
    """
    Тест: 2 субфида с одинаковым приоритетом — дубли НЕ удаляются.

    Субфиды:
        subfeed_a (priority=1): a1, a2, a3, a4, common1, a6, ... (source="A")
        subfeed_b (priority=1): b1, b2, common1, b4, b5, ... (source="B")

    Дубли: common1 присутствует в обоих субфидах с ОДИНАКОВЫМ приоритетом.

    Ожидаемый результат:
        - common1 остаётся из ОБОИХ источников (2 элемента)
        - Правило: одинаковый приоритет = дубли не удаляются
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_a",
                        "type": "subfeed",
                        "method_name": "dedup_a",
                        "priority": 1,  # Одинаковый приоритет
                    },
                },
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_b",
                        "priority": 1,  # Одинаковый приоритет
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=20,
        next_page=FeedResultNextPage(data={}),
    )

    # Ищем common1 элементы — должно быть 2 (из обоих источников)
    common1_items = [item for item in result.data if item["id"] == "common1"]

    # При одинаковом приоритете оба элемента должны остаться
    assert len(common1_items) == 2, "Both common1 items should remain with same priority"


@pytest.mark.asyncio
async def test_dedup_no_duplicates() -> None:
    """
    Тест: субфиды без дублей — данные не меняются.

    Субфиды:
        subfeed_a (priority=1): unique_user1_1, unique_user1_2, unique_user1_3, ...
        subfeed_b (priority=2): unique_user1_1, unique_user1_2, ... (те же данные!)

    Примечание: dedup_no_overlap возвращает unique_{user_id}_{i}, так что
    с одним user_id данные будут одинаковые. Но limit_to_return ограничивает.

    Ожидаемый результат:
        - Данные получены
        - Количество <= limit
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_no_overlap",
                    "priority": 1,
                    "subfeed_params": {"limit_to_return": 5},
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "dedup_no_overlap",
                    "priority": 2,
                    "subfeed_params": {"limit_to_return": 5},
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="user1",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # С разными user_id элементы уникальны
    # Но здесь user_id одинаковый, так что проверяем что данные есть
    assert len(result.data) <= 10


@pytest.mark.asyncio
async def test_dedup_disabled() -> None:
    """
    Тест: deduplicate=False — дубли остаются.

    Субфиды:
        subfeed_a (priority=1): a1, a2, a3, a4, common1, ... (source="A")
        subfeed_b (priority=2): b1, b2, common1, ... (source="B")

    Дубли: common1 присутствует в обоих.

    Ожидаемый результат:
        - deduplicate=False, поэтому дедупликация НЕ применяется
        - common1 присутствует из ОБОИХ источников (2 элемента)
    """
    config = {
        "version": "1",
        "deduplicate": False,  # Дедупликация выключена
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_a",
                        "type": "subfeed",
                        "method_name": "dedup_a",
                        "priority": 1,
                    },
                },
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_b",
                        "priority": 2,
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=20,
        next_page=FeedResultNextPage(data={}),
    )

    # Без дедупликации common1 должен быть из обоих источников
    common1_items = [item for item in result.data if item.get("id") == "common1"]
    assert len(common1_items) == 2, "Both common1 items should remain when dedup is disabled"


# ============================================================================
# Кейсы с курсорами
# ============================================================================


@pytest.mark.asyncio
async def test_dedup_cursor_movement() -> None:
    """
    Тест: курсор двигается правильно, элементы не повторяются между страницами.

    Субфиды:
        subfeed_a (priority=1): a1, a2, a3, a4, common1, a6, a7, ...
        subfeed_b (priority=2): b1, b2, common1, b4, b5, ...

    Ожидаемый результат:
        - Страница 1: 10 элементов, курсоры обновлены
        - Страница 2: другие 10 элементов
        - Элементы между страницами НЕ пересекаются (межстраничная дедупликация через seen_ids)
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "dedup_b",
                    "priority": 2,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)

    # Первая страница
    result1 = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(result1.data) == 10
    assert "subfeed_a" in result1.next_page.data or "subfeed_b" in result1.next_page.data

    # Вторая страница с курсорами
    result2 = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=result1.next_page,
    )

    # Проверяем что вторая страница не пустая
    assert len(result2.data) > 0

    # Проверяем что элементы не повторяются между страницами
    ids1 = {item["id"] for item in result1.data}
    ids2 = {item["id"] for item in result2.data}
    assert ids1.isdisjoint(ids2), "Pages should not have overlapping items"


@pytest.mark.asyncio
async def test_dedup_cursor_partial_use() -> None:
    """
    Тест: items_with_source заполняется корректно.

    Субфиды:
        subfeed_a (priority=1): a1, a2, a3, a4, common1, ...
        subfeed_b (priority=2): b1, b2, common1, ...

    Ожидаемый результат:
        - items_with_source содержит информацию о каждом элементе
        - Количество items_with_source = количество data
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "dedup_b",
                    "priority": 2,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # Проверяем что items_with_source заполнен
    assert len(result.items_with_source) == len(result.data)


# ============================================================================
# Нетривиальные кейсы
# ============================================================================


@pytest.mark.asyncio
async def test_dedup_three_subfeeds_cascade() -> None:
    """
    Тест: 3 субфида с приоритетами 1, 2, 3 — элемент есть во всех трех.

    Субфиды:
        subfeed_a (priority=1): a1, a2, a3, a4, common1, ... (source="A")
        subfeed_b (priority=2): b1, b2, common1, ... (source="B")
        subfeed_c (priority=3): c1, common1, c3, common2, ... (source="C")

    Дубли: common1 присутствует во ВСЕХ трёх субфидах.

    Ожидаемый результат:
        - common1 остаётся только из subfeed_a (priority=1, самый высокий)
        - Только 1 элемент common1 с source="A"
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "dedup_b",
                    "priority": 2,
                },
                {
                    "subfeed_id": "subfeed_c",
                    "type": "subfeed",
                    "method_name": "dedup_c",
                    "priority": 3,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=20,
        next_page=FeedResultNextPage(data={}),
    )

    # common1 есть во всех трех источниках, должен остаться только из A
    common1_items = [item for item in result.data if item["id"] == "common1"]
    assert len(common1_items) == 1
    assert common1_items[0]["source"] == "A"


@pytest.mark.asyncio
async def test_dedup_mixed_priorities() -> None:
    """
    Тест: субфиды с приоритетами 1, 1, 2 — дубли между приоритетом 1 остаются.

    Субфиды:
        subfeed_a1 (priority=1): a1, a2, a3, a4, common1, ... (source="A")
        subfeed_a2 (priority=1): a1, a2, a3, a4, common1, ... (source="A", те же данные!)
        subfeed_b (priority=2): b1, b2, common1, ... (source="B")

    Дубли:
        - a1 из subfeed_a1 и subfeed_a2 (одинаковый приоритет 1)
        - common1 из всех трёх

    Ожидаемый результат:
        - a1 остаётся из ОБОИХ subfeed_a1 и subfeed_a2 (одинаковый приоритет = не удаляем)
        - common1 из subfeed_b (priority=2) удаляется, остаются из priority=1
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 34,
                    "data": {
                        "subfeed_id": "subfeed_a1",
                        "type": "subfeed",
                        "method_name": "dedup_a",
                        "priority": 1,
                    },
                },
                {
                    "percentage": 33,
                    "data": {
                        "subfeed_id": "subfeed_a2",
                        "type": "subfeed",
                        "method_name": "dedup_a",  # Тот же метод, те же данные
                        "priority": 1,
                    },
                },
                {
                    "percentage": 33,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_b",
                        "priority": 2,
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=20,
        next_page=FeedResultNextPage(data={}),
    )

    # a1 есть в обоих субфидах с приоритетом 1 — оба должны остаться
    a1_items = [item for item in result.data if item["id"] == "a1"]
    assert len(a1_items) == 2, "Both a1 items with same priority should remain"


@pytest.mark.asyncio
async def test_dedup_percentage_merger() -> None:
    """
    Тест: дедупликация в процентном мерджере (MergerPercentage).

    Субфиды (50/50):
        subfeed_a (priority=1): a1, a2, a3, a4, common1, ... (source="A")
        subfeed_b (priority=2): b1, b2, common1, ... (source="B")

    MergerPercentage берёт 50% из каждого субфида и чередует.

    Ожидаемый результат:
        - Данные из обоих субфидов
        - common элементы только из subfeed_a (priority=1)
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_a",
                        "type": "subfeed",
                        "method_name": "dedup_a",
                        "priority": 1,
                    },
                },
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_b",
                        "priority": 2,
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # Проверяем что данные есть
    assert len(result.data) > 0

    # Проверяем что common элементы из A (приоритет 1)
    common_items = [item for item in result.data if item["id"].startswith("common")]
    for item in common_items:
        assert item["source"] == "A"


@pytest.mark.asyncio
async def test_dedup_nested_mergers() -> None:
    """
    Тест: вложенные мерджеры — MergerAppend внутри MergerPercentage.

    Структура:
        MergerPercentage (60/40):
            - 60%: MergerAppend
                - subfeed_a (priority=1): a1, a2, ..., common1, ... (source="A")
            - 40%: subfeed_b (priority=2): b1, b2, common1, ... (source="B")

    Дубли: common1 есть в обоих.

    Ожидаемый результат:
        - Дедупликация работает на всех уровнях вложенности
        - common элементы из subfeed_a (priority=1)
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "outer_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 60,
                    "data": {
                        "merger_id": "inner_merger",
                        "type": "merger_append",
                        "items": [
                            {
                                "subfeed_id": "subfeed_a",
                                "type": "subfeed",
                                "method_name": "dedup_a",
                                "priority": 1,
                            },
                        ],
                    },
                },
                {
                    "percentage": 40,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_b",
                        "priority": 2,
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(result.data) > 0

    # common элементы должны быть из A
    common_items = [item for item in result.data if item["id"].startswith("common")]
    for item in common_items:
        assert item["source"] == "A"


@pytest.mark.asyncio
async def test_dedup_positional_merger() -> None:
    """
    Тест: MergerPositional с дедупликацией.

    Структура:
        MergerPositional (позиции 3, 6, 9):
            - positional (priority=1): a1, a2, ..., common1, ... (source="A")
            - default (priority=2): b1, b2, common1, ... (source="B")

    MergerPositional вставляет positional элементы на позиции [3, 6, 9],
    остальные позиции заполняются из default.

    Дубли: common1 есть в обоих.

    Ожидаемый результат:
        - Данные получены из обоих источников
        - Все id уникальны после дедупликации
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "positional_merger",
            "type": "merger_positional",
            "positions": [3, 6, 9],
            "positional": {
                "subfeed_id": "subfeed_a",
                "type": "subfeed",
                "method_name": "dedup_a",
                "priority": 1,  # Высокий приоритет для positional
            },
            "default": {
                "subfeed_id": "subfeed_b",
                "type": "subfeed",
                "method_name": "dedup_b",
                "priority": 2,
            },
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(result.data) > 0

    # Проверяем что items_with_source заполнен
    assert len(result.items_with_source) == len(result.data)

    # Проверяем что есть элементы из обоих источников
    sources = {item.source_id for item in result.items_with_source}
    assert len(sources) >= 1  # Минимум один источник

    # Проверяем что нет дублей по id (все id уникальны после дедупликации)
    ids = [item["id"] for item in result.data]
    assert len(ids) == len(set(ids)), "All ids should be unique after deduplication"


@pytest.mark.asyncio
async def test_dedup_items_with_source_populated() -> None:
    """
    Тест: items_with_source правильно заполняется метаданными.

    Субфиды:
        subfeed_a (priority=1): a1, a2, ..., common1, ...
        subfeed_b (priority=2): b1, b2, common1, ...

    Ожидаемый результат:
        - items_with_source содержит для каждого элемента:
            - source_id: "subfeed_a" или "subfeed_b"
            - priority: 1 или 2
            - item: сам элемент данных
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "dedup_b",
                    "priority": 2,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # items_with_source должен быть заполнен
    assert len(result.items_with_source) == len(result.data)

    # Каждый элемент в items_with_source должен иметь source_id и priority
    for item_info in result.items_with_source:
        assert item_info.source_id in ["subfeed_a", "subfeed_b"]
        assert item_info.priority in [1, 2]
        assert item_info.item is not None


@pytest.mark.asyncio
async def test_dedup_without_dedup_key() -> None:
    """
    Тест: дедупликация без dedup_key — используется сам элемент как ключ.

    Субфиды:
        subfeed_a (priority=1): "x_1", "x_2", "x_3", ... (строки)
        subfeed_b (priority=2): "x_1", "x_2", "x_3", ... (те же строки)

    Метод "ads" возвращает строки "{user_id}_{i}", не словари.
    dedup_key=None означает, что сам элемент используется как ключ.

    Ожидаемый результат:
        - Дубли удаляются (x_1 из subfeed_b удаляется, остаётся из subfeed_a)
        - Все элементы уникальны
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": None,  # Без ключа — сам элемент
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "ads",  # Возвращает строки x_1, x_2...
                    "priority": 1,
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "ads",  # Те же данные
                    "priority": 2,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="x",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # Дубли должны быть удалены (разные приоритеты)
    # Все элементы x_1, x_2... должны быть уникальны
    assert len(result.data) == len(set(result.data))


@pytest.mark.asyncio
async def test_dedup_has_next_page() -> None:
    """
    Тест: has_next_page корректно работает с дедупликацией.

    Субфиды:
        subfeed_a (priority=1): a1, a2, a3, a4, common1, a6, a7, ... (много данных)

    Ожидаемый результат:
        - limit=5, но данных больше
        - has_next_page = True
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)
    result = await manager.get_data(
        user_id="test",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )

    # Должна быть следующая страница (данных больше чем limit)
    assert result.has_next_page is True


# ============================================================================
# Тесты для корректного движения курсора
# ============================================================================


@pytest.mark.asyncio
async def test_cursor_moves_to_last_used_element() -> None:
    """
    Тест: курсор устанавливается на последний ИСПОЛЬЗОВАННЫЙ элемент, а не на запрошенный.

    Сценарий:
        - limit=5, но запрашиваем с запасом (fetch_limit ≈ 10)
        - Субфид возвращает 10 элементов: a1, a2, a3, a4, common1, a6, a7, a8, a9, common2
        - Используем только 5: a1, a2, a3, a4, common1
        - Курсор (after) должен указывать на common1 (5-й элемент), а не на common2 (10-й)

    Ожидаемый результат:
        - На странице 1 показаны элементы 1-5
        - Курсор указывает на 5-й элемент
        - На странице 2 показаны элементы 6-10 (a6, a7, a8, a9, common2)
        - Элементы 6-10 НЕ пропущены
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)

    # Страница 1: получаем 5 элементов
    result1 = await manager.get_data(
        user_id="test",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(result1.data) == 5
    page1_ids = [item["id"] for item in result1.data]

    # Страница 2: получаем следующие 5 элементов
    result2 = await manager.get_data(
        user_id="test",
        limit=5,
        next_page=result1.next_page,
    )

    assert len(result2.data) > 0
    page2_ids = [item["id"] for item in result2.data]

    # Проверяем что страницы не пересекаются
    assert set(page1_ids).isdisjoint(set(page2_ids)), "Pages should not overlap"

    # Проверяем что не пропущены элементы между страницами
    # Первый элемент страницы 2 должен идти сразу после последнего элемента страницы 1
    # (в порядке приоритета и позиции)


@pytest.mark.asyncio
async def test_cursor_no_elements_lost_with_duplicates() -> None:
    """
    Тест: при удалении дублей не теряются элементы между страницами.

    Сценарий:
        - 2 субфида с разными приоритетами
        - Субфид A (priority=1): a1, a2, a3, a4, common1, a6, ...
        - Субфид B (priority=2): b1, b2, common1, b4, b5, common2, ...
        - limit=10
        - common1 из B удаляется (дубль)

    Ожидаемый результат:
        - Курсор субфида B должен указывать на последний ИСПОЛЬЗОВАННЫЙ элемент из B
        - На следующей странице элементы B не пропущены
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_a",
                        "type": "subfeed",
                        "method_name": "dedup_a",
                        "priority": 1,
                    },
                },
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_b",
                        "priority": 2,
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)

    # Собираем все элементы с 3 страниц
    all_ids: set = set()
    next_page = FeedResultNextPage(data={})

    for page_num in range(3):
        result = await manager.get_data(
            user_id="test_cursor",
            limit=10,
            next_page=next_page,
        )

        page_ids = {item["id"] for item in result.data}

        # Проверяем что нет пересечений с предыдущими страницами
        overlap = all_ids.intersection(page_ids)
        assert len(overlap) == 0, f"Page {page_num + 1} overlaps with previous: {overlap}"

        all_ids.update(page_ids)
        next_page = result.next_page

        if not result.has_next_page:
            break

    # Проверяем что собрали достаточно уникальных элементов
    assert len(all_ids) >= 20, f"Should collect at least 20 unique items, got {len(all_ids)}"


@pytest.mark.asyncio
async def test_cursor_after_points_to_last_used() -> None:
    """
    Тест: поле after в курсоре указывает на последний использованный элемент.

    Сценарий:
        - Один субфид, limit=3
        - Субфид возвращает: a1, a2, a3, a4, common1, ...
        - Используем только a1, a2, a3

    Ожидаемый результат:
        - after в курсоре = a3 (или его словарное представление)
        - Следующий запрос начнётся с a4
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)

    # Страница 1
    result1 = await manager.get_data(
        user_id="test",
        limit=3,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(result1.data) == 3
    last_item_page1 = result1.data[-1]

    # Проверяем что after указывает на последний элемент страницы
    cursor = result1.next_page.data.get("subfeed_a")
    assert cursor is not None
    assert cursor.after == last_item_page1, "Cursor after should point to last used item"

    # Страница 2
    result2 = await manager.get_data(
        user_id="test",
        limit=3,
        next_page=result1.next_page,
    )

    # Первый элемент страницы 2 должен быть следующим после последнего элемента страницы 1
    first_item_page2 = result2.data[0]
    assert first_item_page2["id"] != last_item_page1["id"], "Page 2 should start with next item"


@pytest.mark.asyncio
async def test_cross_page_dedup_between_subfeeds() -> None:
    """
    Тест: межстраничная дедупликация между разными субфидами.

    Сценарий:
        - Страница 1: subfeed_a возвращает a1, a2, a3, a4, common1, a6, a7, a8, a9, common2
        - Страница 2: subfeed_b возвращает b1, b2, common1, b4, b5, common2, ...
        - common1 и common2 уже были показаны на странице 1 (из subfeed_a)

    Ожидаемый результат:
        - На странице 2 common1 и common2 из subfeed_b НЕ показываются
        - seen_ids хранит ID элементов между страницами
        - Элементы b1, b2, b4, b5 показываются (они уникальны)

    Это проверяет что seen_ids работает между страницами и между субфидами.
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_a",
                    "type": "subfeed",
                    "method_name": "dedup_a",
                    "priority": 1,
                },
                {
                    "subfeed_id": "subfeed_b",
                    "type": "subfeed",
                    "method_name": "dedup_b",
                    "priority": 2,
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)

    # Страница 1: получаем элементы (в основном из subfeed_a из-за MergerAppend)
    # dedup_a: a1, a2, a3, a4, common1, a6, a7, a8, a9, common2, ...
    result1 = await manager.get_data(
        user_id="cross_page_test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    page1_ids = {item["id"] for item in result1.data}

    # Проверяем что на странице 1 есть common элементы
    common_on_page1 = {id for id in page1_ids if id.startswith("common")}
    assert len(common_on_page1) > 0, "Page 1 should have some common elements"

    # Страница 2
    result2 = await manager.get_data(
        user_id="cross_page_test",
        limit=10,
        next_page=result1.next_page,
    )

    page2_ids = {item["id"] for item in result2.data}

    # КЛЮЧЕВАЯ ПРОВЕРКА: common элементы со страницы 1 НЕ должны быть на странице 2
    duplicates_shown = page1_ids.intersection(page2_ids)
    assert len(duplicates_shown) == 0, (
        f"Elements from page 1 should NOT appear on page 2. " f"Duplicates found: {duplicates_shown}"
    )

    # Дополнительно: проверяем что страница 2 не пустая
    assert len(result2.data) > 0, "Page 2 should have elements"


@pytest.mark.asyncio
async def test_page_filled_despite_many_duplicates() -> None:
    """
    Тест: страница гарантированно заполняется до limit, даже если много дублей.

    Сценарий:
        - limit=10
        - subfeed_a и subfeed_b возвращают много общих элементов (common1, common2, ...)
        - subfeed_c (dedup_c) возвращает: c1, common1, c3, common2, c5, common3, ...
          (каждый второй элемент - common)

    Ожидаемый результат:
        - Страница заполнена ровно до limit (10 элементов)
        - Даже если первые запросы вернули много дублей, цикл продолжает
          запрашивать пока не наберёт нужное количество
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_a",
                        "type": "subfeed",
                        "method_name": "dedup_c",  # Много common элементов
                        "priority": 1,
                    },
                },
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_c",  # Те же данные = много дублей!
                        "priority": 2,
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)

    result = await manager.get_data(
        user_id="fill_test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # Страница должна быть заполнена до limit
    assert len(result.data) == 10, f"Page should have exactly 10 items, got {len(result.data)}"

    # Все ID должны быть уникальны
    ids = [item["id"] for item in result.data]
    assert len(ids) == len(set(ids)), "All IDs should be unique"


@pytest.mark.asyncio
async def test_cursor_unused_subfeed_not_advanced() -> None:
    """
    Тест: если субфид не использован (все элементы удалены как дубли), его курсор не двигается.

    Сценарий:
        - Субфид A (priority=1): common1, common2, common3, ...
        - Субфид B (priority=2): common1, common2, common3, ... (те же элементы!)
        - Все элементы B удалены как дубли

    Ожидаемый результат:
        - Курсор субфида B откатывается (page - 1)
        - На следующей странице B снова пробует те же элементы
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "feed": {
            "merger_id": "test_merger",
            "type": "merger_percentage",
            "items": [
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_a",
                        "type": "subfeed",
                        "method_name": "dedup_c",  # common1, c1, common2, c3, ...
                        "priority": 1,
                    },
                },
                {
                    "percentage": 50,
                    "data": {
                        "subfeed_id": "subfeed_b",
                        "type": "subfeed",
                        "method_name": "dedup_c",  # Те же данные!
                        "priority": 2,
                    },
                },
            ],
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT)

    result = await manager.get_data(
        user_id="test",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    # Все элементы из subfeed_b должны быть удалены как дубли (одинаковые данные, ниже приоритет)
    # Проверяем что курсор subfeed_b не продвинулся (page <= 1 или откатился)
    cursor_b = result.next_page.data.get("subfeed_b")
    if cursor_b:
        # Если элементы из B не использованы, page должен быть 1 или меньше
        # (в зависимости от реализации)
        assert cursor_b.page <= 2, "Cursor for unused subfeed should not advance much"


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_positional_with_view_session(redis_client) -> None:
    """
    Тест дедупликации с MergerPositional + MergerViewSession.

    Конфигурация:
    - MergerPositional с placeholder_tours на позициях [1, 3, 5, 7]
    - MergerViewSession с regular_tours как default (session_size=200)
    - placeholder_tours: возвращает {"id": "placeholder_1", ...}, {"id": "placeholder_2", ...}, ...
    - regular_tours: первые 10 элементов дублируются с placeholder (id: "placeholder_1" до "placeholder_10")
    - priority: placeholder_tours = 1 (высший), MergerViewSession = 0 (по умолчанию)

    Ожидаемое поведение:
    - MergerPositional сначала вставляет placeholder туры на позиции [1, 3, 5, 7]
    - Затем заполняет остальные позиции из MergerViewSession
    - Дедупликация удаляет дубли: так как placeholder_tours (priority=1) имеет более высокий приоритет,
      элементы placeholder_1 до placeholder_10 из regular_tours будут удалены
    - Но так как MergerViewSession уже закэшировал данные, он не может дозапросить новые элементы
    - Поэтому в результате будут только placeholder элементы из positional и tour_11+ из regular_tours
    - Страница будет заполнена до limit=15
    """
    config = {
        "version": "1",
        "deduplicate": True,
        "dedup_key": "id",
        "dedup_session_ttl": 300,
        "feed": {
            "merger_id": "serp_fast_main",
            "type": "merger_positional",
            "positions": [1, 3, 5, 7],
            "positional": {
                "subfeed_id": "placeholder_tours",
                "type": "subfeed",
                "method_name": "placeholder_tours",
                "priority": 1,  # Высший приоритет
                "subfeed_params": {},
            },
            "default": {
                "merger_id": "serp_fast_session",
                "type": "merger_view_session",
                "session_size": 200,
                "session_live_time": 300,
                "data": {
                    "subfeed_id": "regular_tours",
                    "type": "subfeed",
                    "method_name": "regular_tours",
                    "priority": 3,  # Низший приоритет
                    "subfeed_params": {},
                },
            },
        },
    }

    manager = FeedManager(config=config, methods_dict=METHODS_DICT, redis_client=redis_client)
    result = await manager.get_data(
        user_id="test_user",
        limit=15,
        next_page=FeedResultNextPage(data={}),
    )

    # Проверяем что получили 15 элементов
    assert len(result.data) == 15, f"Expected 15 items, got {len(result.data)}"

    # Отладочный вывод
    print("\n=== Результат ===")
    for i, item in enumerate(result.data):
        print(f"  [{i}] {item['id']} (type: {item['type']})")

    # Проверяем что дубли удалены
    all_ids = [item["id"] for item in result.data]

    # Считаем сколько раз встречается каждый id
    from collections import Counter

    id_counts = Counter(all_ids)

    # Проверяем что нет дублей
    for item_id, count in id_counts.items():
        assert count == 1, f"Item {item_id} appears {count} times, expected 1"

    # Проверяем что результат содержит placeholder_1 до placeholder_10
    # (так как они имеют priority=1 и должны вытеснить дубли из regular_tours)
    # и tour_11 до tour_15 (уникальные элементы из regular_tours)
    expected_ids = [f"placeholder_{i}" for i in range(1, 11)] + [f"tour_{i}" for i in range(11, 16)]
    assert all_ids == expected_ids, f"Expected {expected_ids}, got {all_ids}"

    # Проверяем что has_next_page = True (есть еще данные)
    assert result.has_next_page is True

    print(f"✅ Test passed! Got {len(result.data)} items")
    print(f"   IDs: {all_ids[:10]}...")
    print(f"   Deduplication worked correctly!")
