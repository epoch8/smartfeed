import json
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Union

import redis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import RedisCluster as AsyncRedisCluster

from .schemas import FeedConfig, FeedResult, FeedResultItem, FeedResultNextPage, FeedResultNextPageInside


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

        self.feed_config = FeedConfig.parse_obj(config)
        self.methods_dict = methods_dict
        self.redis_client = redis_client

    def _get_dedup_key_value(self, item: Any) -> Any:
        """
        Получение значения ключа дедупликации из элемента.

        :param item: элемент данных.
        :return: значение ключа дедупликации.
        """
        dedup_key = self.feed_config.dedup_key

        if not dedup_key:
            return item

        try:
            dedup_value = item.get(dedup_key)
        except AttributeError:
            dedup_value = getattr(item, dedup_key, None)

        if dedup_value is None:
            raise ValueError(f"Deduplication failed: entity {item} has no key or attr {dedup_key}")

        return dedup_value

    def _deduplicate_by_priority(
        self,
        items_with_source: List[FeedResultItem],
        seen_ids: Set[Any],
    ) -> tuple[List[FeedResultItem], Dict[str, int]]:
        """
        Дедупликация элементов по приоритету.

        Правила:
        - Если два элемента имеют одинаковый dedup_key, оставляем тот, у которого приоритет выше (меньше число).
        - Если приоритеты одинаковые — оставляем оба.
        - Элементы из seen_ids пропускаются (межстраничная дедупликация).

        :param items_with_source: список элементов с информацией об источнике.
        :param seen_ids: множество уже показанных ID.
        :return: кортеж (дедуплицированный список, словарь использованных элементов по source_id).
        """
        # Группируем по dedup_key
        by_dedup_key: Dict[Any, List[FeedResultItem]] = defaultdict(list)
        for item in items_with_source:
            dedup_key_value = self._get_dedup_key_value(item.item)
            by_dedup_key[dedup_key_value].append(item)

        result: List[FeedResultItem] = []
        used_by_source: Dict[str, int] = defaultdict(int)

        for dedup_key_value, group in by_dedup_key.items():
            # Пропускаем уже показанные элементы
            if dedup_key_value in seen_ids:
                continue

            if len(group) == 1:
                # Один элемент — просто добавляем
                result.append(group[0])
                used_by_source[group[0].source_id] += 1
            else:
                # Несколько элементов — группируем по приоритету
                by_priority: Dict[int, List[FeedResultItem]] = defaultdict(list)
                for item in group:
                    by_priority[item.priority].append(item)

                # Находим минимальный (лучший) приоритет
                min_priority = min(by_priority.keys())
                best_items = by_priority[min_priority]

                # Если несколько элементов с лучшим приоритетом — оставляем все (одинаковый приоритет)
                for item in best_items:
                    result.append(item)
                    used_by_source[item.source_id] += 1

        # Сортируем результат по оригинальной позиции для сохранения порядка
        result.sort(key=lambda x: (x.priority, x.position))

        return result, dict(used_by_source)

    def _recalculate_cursors(
        self,
        next_page: FeedResultNextPage,
        original_items_with_source: List[FeedResultItem],
        final_items: List[FeedResultItem],
    ) -> FeedResultNextPage:
        """
        Пересчет курсоров после дедупликации.

        Курсор каждого субфида устанавливается на последний реально использованный элемент,
        а не на последний запрошенный.

        :param next_page: оригинальный next_page (курсоры после запроса всех данных).
        :param original_items_with_source: оригинальный список элементов до дедупликации/обрезки.
        :param final_items: финальный список элементов после дедупликации и обрезки до limit.
        :return: пересчитанный next_page с курсорами на реально использованные элементы.
        """
        new_next_page = FeedResultNextPage(data={})

        # Группируем original_items по source_id для получения списка элементов каждого субфида
        items_by_source: Dict[str, List[FeedResultItem]] = defaultdict(list)
        for item in original_items_with_source:
            items_by_source[item.source_id].append(item)

        # Находим последний использованный элемент от каждого source_id
        last_used_by_source: Dict[str, FeedResultItem] = {}
        for item in final_items:
            last_used_by_source[item.source_id] = item

        # Подсчитываем количество использованных элементов по source_id
        used_count_by_source: Dict[str, int] = defaultdict(int)
        for item in final_items:
            used_count_by_source[item.source_id] += 1

        for source_id, cursor in next_page.data.items():
            source_items = items_by_source.get(source_id, [])
            fetched = len(source_items)
            used = used_count_by_source.get(source_id, 0)

            if used == 0:
                # Ничего не использовано — не двигаем курсор
                # Откатываем page на 1 назад, after оставляем прежний
                new_next_page.data[source_id] = FeedResultNextPageInside(
                    page=max(1, cursor.page - 1),
                    after=cursor.after,
                )
            elif used < fetched:
                # Использовано меньше чем запрошено — устанавливаем after на последний использованный элемент
                last_used = last_used_by_source[source_id]
                new_next_page.data[source_id] = FeedResultNextPageInside(
                    page=cursor.page,  # page оставляем как есть
                    after=last_used.item,  # after = последний использованный элемент
                )
            else:
                # Использовано всё что запрошено — курсор остаётся как есть
                new_next_page.data[source_id] = cursor

        return new_next_page

    async def _get_seen_ids(self, user_id: Any, **params: Any) -> Set[Any]:
        """
        Получение seen_ids из Redis.

        :param user_id: ID пользователя.
        :param params: дополнительные параметры.
        :return: множество seen_ids.
        """
        if not self.redis_client:
            return set()

        if custom_key := params.get("custom_dedup_session_key"):
            cache_key = f"smartfeed_seen:{user_id}:{custom_key}"
        else:
            cache_key = f"smartfeed_seen:{user_id}"

        try:
            if isinstance(self.redis_client, (AsyncRedis, AsyncRedisCluster)):
                cached_data = await self.redis_client.get(cache_key)
            else:
                cached_data = self.redis_client.get(cache_key)

            if cached_data:
                return set(json.loads(cached_data))
        except Exception:
            pass

        return set()

    async def _save_seen_ids(self, user_id: Any, seen_ids: Set[Any], **params: Any) -> None:
        """
        Сохранение seen_ids в Redis.

        :param user_id: ID пользователя.
        :param seen_ids: множество seen_ids.
        :param params: дополнительные параметры.
        """
        if not self.redis_client:
            return

        if custom_key := params.get("custom_dedup_session_key"):
            cache_key = f"smartfeed_seen:{user_id}:{custom_key}"
        else:
            cache_key = f"smartfeed_seen:{user_id}"

        ttl = self.feed_config.dedup_session_ttl

        try:
            seen_list = list(seen_ids)
            if isinstance(self.redis_client, (AsyncRedis, AsyncRedisCluster)):
                await self.redis_client.set(cache_key, json.dumps(seen_list))
                await self.redis_client.expire(cache_key, ttl)
            else:
                self.redis_client.set(cache_key, json.dumps(seen_list), ex=ttl)
        except Exception:
            pass

    async def _clear_seen_ids(self, user_id: Any, **params: Any) -> None:
        """
        Очистка seen_ids в Redis (для новой сессии).

        :param user_id: ID пользователя.
        :param params: дополнительные параметры.
        """
        if not self.redis_client:
            return

        if custom_key := params.get("custom_dedup_session_key"):
            cache_key = f"smartfeed_seen:{user_id}:{custom_key}"
        else:
            cache_key = f"smartfeed_seen:{user_id}"

        try:
            if isinstance(self.redis_client, (AsyncRedis, AsyncRedisCluster)):
                await self.redis_client.delete(cache_key)
            else:
                self.redis_client.delete(cache_key)
        except Exception:
            pass

    async def get_data(self, user_id: Any, limit: int, next_page: FeedResultNextPage, **params: Any) -> FeedResult:
        """
        Метод для получения данных согласно конфигурации.

        :param user_id: ID объекта для получения данных (например, ID пользователя).
        :param limit: лимит на выдачу данных.
        :param next_page: курсор для пагинации в формате SmartFeedResultNextPage.
        :param params: любые внешние параметры, передаваемые в исполняемую функцию на клиентской стороне.
        :return: результат получения данных согласно конфигурации фида.
        """
        # Проверяем, нужна ли дедупликация
        if not self.feed_config.deduplicate:
            # Без дедупликации — стандартное поведение
            result = await self.feed_config.feed.get_data(
                methods_dict=self.methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=self.redis_client,
                **params,
            )
            return result

        # С дедупликацией
        # Проверяем, это новая сессия (пустой next_page)
        is_new_session = len(next_page.data) == 0

        if is_new_session:
            # Очищаем seen_ids для новой сессии
            await self._clear_seen_ids(user_id, **params)
            seen_ids: Set[Any] = set()
        else:
            # Загружаем seen_ids из Redis
            seen_ids = await self._get_seen_ids(user_id, **params)

        # Цикл для гарантированного заполнения страницы до limit
        # Запрашиваем данные пока не наберём limit элементов или пока субфиды не закончатся
        all_items_with_source: List[FeedResultItem] = []
        all_deduplicated_items: List[FeedResultItem] = []
        current_next_page = next_page
        last_result: Optional[FeedResult] = None
        has_more_data = True

        # Максимум итераций для защиты от бесконечного цикла
        max_iterations = 5
        iteration = 0

        while len(all_deduplicated_items) < limit and has_more_data and iteration < max_iterations:
            iteration += 1

            # Сколько ещё нужно элементов
            needed = limit - len(all_deduplicated_items)

            # Запрашиваем с запасом (x2 от нужного, минимум limit)
            fetch_limit = max(needed * 2, limit)

            result = await self.feed_config.feed.get_data(
                methods_dict=self.methods_dict,
                user_id=user_id,
                limit=fetch_limit,
                next_page=current_next_page,
                redis_client=self.redis_client,
                **params,
            )
            last_result = result

            # Если нет items_with_source — дедупликация невозможна
            if not result.items_with_source:
                if not all_deduplicated_items:
                    # Первая итерация и нет данных
                    return FeedResult(
                        data=result.data[:limit],
                        next_page=result.next_page,
                        has_next_page=result.has_next_page or len(result.data) > limit,
                        items_with_source=[],
                    )
                break

            # Собираем все элементы
            all_items_with_source.extend(result.items_with_source)

            # Дедуплицируем по приоритету (включая seen_ids и уже собранные элементы)
            # Создаём временный seen_ids с уже добавленными элементами
            temp_seen_ids = seen_ids.copy()
            for item in all_deduplicated_items:
                temp_seen_ids.add(self._get_dedup_key_value(item.item))

            new_deduplicated, _ = self._deduplicate_by_priority(
                result.items_with_source,
                temp_seen_ids,
            )

            all_deduplicated_items.extend(new_deduplicated)

            # Обновляем курсоры для следующей итерации
            current_next_page = result.next_page

            # Проверяем, есть ли ещё данные
            has_more_data = result.has_next_page

        # Обрезаем до limit
        final_items = all_deduplicated_items[:limit]

        # Извлекаем данные
        final_data = [item.item for item in final_items]

        # Обновляем seen_ids
        for item in final_items:
            dedup_key_value = self._get_dedup_key_value(item.item)
            seen_ids.add(dedup_key_value)

        # Сохраняем seen_ids в Redis
        await self._save_seen_ids(user_id, seen_ids, **params)

        # Пересчитываем курсоры на основе реально использованных элементов
        if last_result:
            new_next_page = self._recalculate_cursors(
                current_next_page,
                all_items_with_source,
                final_items,
            )
            final_has_next_page = has_more_data or len(all_deduplicated_items) > limit
        else:
            new_next_page = next_page
            final_has_next_page = False

        return FeedResult(
            data=final_data,
            next_page=new_next_page,
            has_next_page=final_has_next_page,
            items_with_source=final_items,
        )
