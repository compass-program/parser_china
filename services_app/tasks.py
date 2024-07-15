import os
import asyncio
import time
from redis import Redis
from celery import current_app
from services_app.celery_app import celery_app, logger
from fetch_data.parsers import parsers

redis_client = Redis.from_url(os.getenv('REDIS_URL'))

PARSER_TIMEOUT = 1  # Таймаут для завершения старого инстанса


def stop_task(task_id):
    try:
        current_app.control.revoke(task_id, terminate=True)
        logger.info(f"Task {task_id} was revoked.")
    except Exception as e:
        logger.error(f"Failed to stop task {task_id}: {e}")

@celery_app.task(bind=True, max_retries=5, default_retry_delay=60)
def schedule_stop_previous_instance(self, parser_name, previous_task_id):
    """
    Планирует остановку предыдущего инстанса парсера через минуту.

    :param self: Ссылка на текущий экземпляр задачи.
    :param parser_name: Имя класса парсера, который необходимо запустить.
    :param previous_task_id: ID предыдущего таска парсера.
    """
    try:
        time.sleep(PARSER_TIMEOUT)
        stop_task(previous_task_id)
    except Exception as e:
        logger.error(f"Ошибка при остановке предыдущего инстанса парсера {parser_name}: {e}")
        self.retry(exc=e)

@celery_app.task(bind=True, max_retries=5, default_retry_delay=60)
def parse_some_data(self, parser_name, *args, **kwargs):
    """
    Запуск парсера для обработки данных.

    :param self: Ссылка на текущий экземпляр задачи.
    :param parser_name: Имя класса парсера, который необходимо запустить.
    :param args: Позиционные аргументы для инициализации парсера.
    :param kwargs: Именованные аргументы для инициализации парсера.
    """
    try:
        # Получаем класс парсера по имени
        parser_class = parsers.get(parser_name)
        if not parser_class:
            raise ValueError(f"Парсер с именем {parser_name} не найден")

        # Остановка предыдущего инстанса
        previous_task_id = redis_client.get(f"active_parser_{parser_name}")
        if previous_task_id:
            # Запускаем таск для остановки предыдущего инстанса через минуту
            schedule_stop_previous_instance.apply_async((parser_name, previous_task_id.decode()), countdown=60)

        # Создаем новый инстанс парсера и запускаем его
        parser = parser_class(*args, **kwargs)
        asyncio.run(parser.run())

        # Сохраняем текущий task_id в Redis
        redis_client.set(f"active_parser_{parser_name}", self.request.id)

    except urllib3.exceptions.ProtocolError as e:
        logger.error(f"Protocol error during execution of parser {parser_name}: {e}")
        self.retry(exc=e)
    except Exception as e:
        logger.error(f"Ошибка при выполнении парсера {parser_name}: {e}")
        self.retry(exc=e)