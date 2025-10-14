import asyncio
import json
import pytest
import redis
from redis.asyncio import Redis as AsyncRedis
import time

from smartfeed.schemas import FeedResultNextPage, MergerViewSession
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_VIEW_SESSION_CONFIG


class RedisReplicationSimulator:
    """
    Симулятор задержки репликации Redis для тестирования проблемы кластера.
    """
    def __init__(self, real_client):
        self.real_client = real_client
        self.write_delay = 0.1  # Задержка для имитации репликации
        self.pending_writes = {}  # Ключи которые только что записали
    
    def exists(self, cache_key):
        return self.real_client.exists(cache_key)
    
    def set(self, name, value, ex=None):
        # Записываем в реальный Redis
        result = self.real_client.set(name, value, ex=ex)
        # Помечаем что этот ключ только что записан (имитация репликации)
        self.pending_writes[name] = time.time()
        return result
    
    def get(self, name):
        # Если ключ только что записан (в течение write_delay секунд), возвращаем None
        if name in self.pending_writes:
            write_time = self.pending_writes[name]
            if time.time() - write_time < self.write_delay:
                return None  # Имитация задержки репликации
            else:
                # Задержка прошла, можно удалить из pending
                del self.pending_writes[name]
        
        # Обычное чтение из Redis
        return self.real_client.get(name)


@pytest.mark.asyncio
async def test_redis_replication_delay_problem():
    """
    Тест для воспроизведения проблемы репликации Redis с использованием
    RedisReplicationSimulator для имитации задержки.
    """
    
    # Подключаемся к Redis (должен быть запущен локально)
    try:
        real_client = redis.Redis(host='localhost', port=6379, db=0)
        real_client.ping()  # Проверяем соединение
    except (redis.ConnectionError, redis.ResponseError):
        pytest.skip("Redis not available for live testing")
    
    # Очищаем тестовый ключ
    test_key = "test_merger_view_session_test_user"
    real_client.delete(test_key)
    
    # Используем симулятор задержки репликации
    redis_client = RedisReplicationSimulator(real_client)
    merger_vs = MergerViewSession.parse_obj(MERGER_VIEW_SESSION_CONFIG)
    
    print("\n=== Демонстрация проблемы с задержкой репликации ===")
    
    try:
        # Этот вызов должен воспроизвести проблему с оригинальным кодом
        result = await merger_vs.get_data(
            methods_dict=METHODS_DICT,
            limit=10,
            next_page=FeedResultNextPage(data={}),
            user_id="test_user",
            redis_client=redis_client,
        )
        
        print("✅ Исправление работает! Получили результат без ошибки:")
        print(f"   Данные: {result.data[:5]}... (показаны первые 5)")
        print(f"   Размер: {len(result.data)}")
        print(f"   Есть следующая страница: {result.has_next_page}")
        
        # Проверяем что получили валидные данные
        assert len(result.data) == 10
        assert result.data[0] == "test_user_1"
        assert result.has_next_page is True
        
    except TypeError as e:
        if "the JSON object must be str, bytes or bytearray, not NoneType" in str(e):
            print("❌ Проблема НЕ исправлена! Все еще получаем TypeError")
            raise
        else:
            print(f"❓ Неожиданная ошибка: {e}")
            raise
    
    finally:
        # Очистка
        real_client.delete(test_key)
        real_client.close()


@pytest.mark.asyncio 
async def test_redis_multiple_requests():
    """
    Тест множественных запросов для проверки стабильности исправления.
    """
    
    try:
        real_client = redis.Redis(host='localhost', port=6379, db=0)
        real_client.ping()
    except (redis.ConnectionError, redis.ResponseError):
        pytest.skip("Redis not available for live testing")
    
    test_key = "test_merger_multiple_test_user"
    real_client.delete(test_key)
    
    redis_client = RedisReplicationSimulator(real_client)
    merger_vs = MergerViewSession.parse_obj(MERGER_VIEW_SESSION_CONFIG)
    
    print("\n=== Тест множественных запросов ===")
    
    try:
        # Первый запрос - создает кэш
        result1 = await merger_vs.get_data(
            methods_dict=METHODS_DICT,
            limit=5,
            next_page=FeedResultNextPage(data={}),
            user_id="test_user",
            redis_client=redis_client,
        )
        
        print(f"Первый запрос: получили {len(result1.data)} элементов")
        
        # Ждем чтобы задержка репликации прошла
        await asyncio.sleep(0.2)
        
        # Второй запрос - должен использовать кэш  
        from smartfeed.schemas import FeedResultNextPageInside
        result2 = await merger_vs.get_data(
            methods_dict=METHODS_DICT,
            limit=5, 
            next_page=FeedResultNextPage(
                data={"merger_view_session_example": FeedResultNextPageInside(page=2, after=None)}
            ),
            user_id="test_user",
            redis_client=redis_client,
        )
        
        print(f"Второй запрос: получили {len(result2.data)} элементов")
        print(f"Данные второй страницы: {result2.data}")
        
        # Проверяем что получили разные данные (пагинация работает)
        assert result1.data != result2.data
        assert len(result2.data) == 5
        
        print("✅ Множественные запросы работают корректно!")
        
    finally:
        real_client.delete(test_key)
        real_client.close()


if __name__ == "__main__":
    # Для запуска напрямую без pytest
    asyncio.run(test_redis_replication_delay_problem())