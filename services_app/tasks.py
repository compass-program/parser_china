import asyncio
import os
import subprocess
import time
import urllib3
from celery import current_app
from services_app.celery_app import celery_app, logger, redis_client
from fetch_data.parsers import parsers


PARSER_TIMEOUT = 60  # Таймаут для завершения старого инстанса


def stop_task(task_id):
    try:
        current_app.control.revoke(task_id, terminate=True)
        logger.info(f"Задача {task_id} была остановлена.")
    except Exception as e:
        logger.error(f"Не удалось остановить задачу {task_id}: {e}")


def clear_task_metadata(task_id):
    try:
        celery_result = current_app.AsyncResult(task_id)
        celery_result.forget()
        logger.info(f"Метаданные задачи {task_id} удалены.")
    except Exception as e:
        logger.error(f"Не удалось удалить метаданные задачи {task_id}: {e}")


def delete_celery_task_meta_keys():
    try:
        keys = redis_client.keys("celery-task-meta-*")
        if keys:
            redis_client.delete(*keys)
            logger.info(f"Удалены следующие ключи из Redis: {keys}")
        else:
            logger.info("Ключи для удаления не найдены.")
    except Exception as e:
        logger.error(f"Ошибка при удалении ключей celery-task-meta: {e}")


@celery_app.task(bind=True, max_retries=5, default_retry_delay=60)
def schedule_stop_previous_instance(self, parser_name, previous_task_id):
    """
    Планирует остановку предыдущего инстанса парсера через минуту.

    :param self: Ссылка на текущий экземпляр задачи.
    :param parser_name: Имя класса парсера, который необходимо запустить.
    :param previous_task_id: ID предыдущего таска парсера.
    """
    try:
        logger.info(
            f"Запланирована остановка предыдущей задачи {previous_task_id} "
            f"для парсера {parser_name} через {PARSER_TIMEOUT} секунд.")
        time.sleep(PARSER_TIMEOUT)
        stop_task(previous_task_id)
        clear_task_metadata(previous_task_id)
        logger.info(
            f"Предыдущая задача {previous_task_id} для парсера "
            f"{parser_name} остановлена.")
    except Exception as e:
        logger.error(
            f"Ошибка при остановке предыдущего инстанса"
            f" парсера {parser_name}: {e}")
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
    parser = None
    try:
        logger.info(f"Запуск парсера {parser_name} с task_id {self.request.id}")

        # Получаем класс парсера по имени
        parser_class = parsers.get(parser_name)
        if not parser_class:
            raise ValueError(f"Парсер с именем {parser_name} не найден")

        # Остановка предыдущего инстанса, если он существует
        previous_task_id = redis_client.get(f"active_parser_{parser_name}")
        if previous_task_id:
            previous_task_id = previous_task_id.decode()
            if previous_task_id != self.request.id and not kwargs.get(
                    'is_first_run', False):
                logger.info(
                    f"Найдена предыдущая задача {previous_task_id} "
                    f"для парсера {parser_name}, планируется остановка.")
                # Запускаем таск для остановки предыдущего инстанса через минуту
                schedule_stop_previous_instance.apply_async(
                    (parser_name, previous_task_id), countdown=60)
            else:
                logger.info(
                    f"Первый запуск или совпадение идентификаторов, "
                    f"остановка предыдущей задачи {previous_task_id} "
                    f"для парсера {parser_name} не требуется.")
        else:
            logger.info(
                f"Предыдущая задача для парсера {parser_name} не найдена.")

        # Удаление is_first_run из kwargs перед созданием парсера
        kwargs.pop('is_first_run', None)

        # Сохраняем текущий task_id в Redis сразу
        redis_client.set(f"active_parser_{parser_name}", self.request.id)
        logger.info(
            f"Установлена активная задача {self.request.id} "
            f"для парсера {parser_name} в Redis.")

        # Создаем новый инстанс парсера и запускаем его
        parser = parser_class(*args, **kwargs)
        asyncio.run(parser.run())
        logger.info(
            f"Парсер {parser_name} с task_id {self.request.id} успешно завершен.")

    except urllib3.exceptions.ProtocolError as e:
        logger.error(
            f"Ошибка протокола при выполнении парсера {parser_name}: {e}")
        self.retry(exc=e)
    except Exception as e:
        logger.error(f"Ошибка при выполнении парсера {parser_name}: {e}")
        self.retry(exc=e)
    finally:
        if parser:
            asyncio.run(parser.close())
        # Удаление метаданных задачи
        clear_task_metadata(self.request.id)


@celery_app.task
def check_and_start_parsers(is_first_run: bool = False):
    """
    Проверяет активные задачи парсеров и запускает их в нужном порядке.

    Если это первый запуск (`is_first_run=True`), выполняется очистка всех данных в Redis.

    Args:
        is_first_run (bool): Флаг, указывающий, является ли это первым запуском.
    """
    logger.info("Запуск проверки активных задач парсеров.")

    if is_first_run:
        logger.info(
            "Первый запуск, удаление всех celery-task-meta ключей из Redis.")
        delete_celery_task_meta_keys()
        logger.info("Очистка всех данных в Redis.")
        subprocess.run(['redis-cli', 'FLUSHALL'], check=True)

        # Очистка таблицы матчей в БД
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts', 'reset_table_match.sh')
        os.chmod(script_path, 0o755)
        subprocess.run([script_path], check=True)

    inspect = current_app.control.inspect()
    active_tasks = inspect.active()  # Получаем активные задачи

    # Сначала проверяем и запускаем FB
    fb_tasks = []
    for worker, tasks in active_tasks.items():
        for task in tasks:
            if task['name'] == 'services_app.tasks.parse_some_data' and \
                    task['args'][0] == 'FB':
                fb_tasks.append(task)

    if fb_tasks:
        logger.info("Активная задача FB уже работает.")
    else:
        logger.info("Запуск новой задачи для FB через 30 секунд.")
        time.sleep(30)
        # Перед запуском задачи, проверяем снова, чтобы убедиться, что инстанс не был запущен в промежутке
        active_tasks = inspect.active()  # Обновляем список активных задач
        fb_tasks = [task for worker, tasks in active_tasks.items() for task in
                    tasks if
                    task['name'] == 'services_app.tasks.parse_some_data' and
                    task['args'][0] == 'FB']
        if not fb_tasks:
            parse_some_data.apply_async(args=('FB',),
                                        kwargs={'is_first_run': is_first_run})
        else:
            logger.info(
                "Инстанс FB был запущен другим процессом, пропускаем запуск.")

    # Проверяем и запускаем FetchAkty только после FB
    fetch_akty_tasks = []
    for worker, tasks in active_tasks.items():
        for task in tasks:
            if task['name'] == 'services_app.tasks.parse_some_data' and \
                    task['args'][0] == 'FetchAkty':
                fetch_akty_tasks.append(task)

    if fetch_akty_tasks:
        logger.info("Активная задача FetchAkty уже работает.")
    else:
        # Ждем 1,5 минуты перед запуском FetchAkty
        logger.info(
            "Запуск новой задачи для FetchAkty через 1,5 минуты после запуска FB.")
        time.sleep(90)
        # Перед запуском задачи, проверяем снова, чтобы убедиться, что инстанс не был запущен в промежутке
        active_tasks = inspect.active()  # Обновляем список активных задач
        fetch_akty_tasks = [task for worker, tasks in active_tasks.items() for
                            task in tasks if task[
                                'name'] == 'services_app.tasks.parse_some_data' and
                            task['args'][0] == 'FetchAkty']
        if not fetch_akty_tasks:
            parse_some_data.apply_async(args=('FetchAkty',),
                                        kwargs={'is_first_run': is_first_run})
        else:
            logger.info(
                "Инстанс FetchAkty был запущен другим процессом, пропускаем запуск.")
