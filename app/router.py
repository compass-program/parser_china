import os
import asyncio
import aiofiles
import subprocess
import dotenv
from fastapi import APIRouter, HTTPException
from services_app.tasks import parse_some_data
from app.schema import ParserRequest
from transfer_data.redis_client import RedisClient
from datetime import datetime, timedelta

route = APIRouter()
# Удаляем loop = asyncio.get_event_loop() так как оно не используется


@route.post("/run_parser/")
async def run_parser(request: ParserRequest):
    """
    Эндпоинт для запуска парсера.

    :param request: Данные для запуска парсера (имя класса парсера, аргументы и именованные аргументы)
    :return: Сообщение о статусе запуска парсера
    """
    parsers_name = [
        'FetchAkty',
        'FB'
    ]
    try:
        if request.parser_name not in parsers_name:
            raise HTTPException(status_code=400, detail="Parser class not found")

        # Запускаем задачу Celery
        parse_some_data.delay(request.parser_name, *request.args, **request.kwargs)

        return {"status": "Parser is running", "parser": request.parser_name}
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@route.get("/logs/akty")
async def get_akty_logs():
    """
    Эндпоинт для получения последних 50 строк из файла логов akty_debug.log.

    :return: Содержимое последних 50 строк лог-файла
    """
    log_file_path = 'logs/akty_debug.log'
    try:
        async with aiofiles.open(log_file_path, 'r') as log_file:
            lines = await log_file.readlines()
            # Получаем последние 50 строк
            last_lines = lines[-50:]
            return {"logs": last_lines}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Log file not found")
    except Exception as e:
        # Детализированный ответ об ошибке
        raise HTTPException(status_code=500, detail=f"Error reading log file: {str(e)}")


@route.get("/logs/fb")
async def get_fb_logs():
    """
    Эндпоинт для получения последних 50 строк из файла логов fb_debug.log.

    :return: Содержимое последних 50 строк лог-файла
    """
    log_file_path = 'logs/fb_debug.log'
    try:
        async with aiofiles.open(log_file_path, 'r') as log_file:
            lines = await log_file.readlines()
            # Получаем последние 50 строк
            last_lines = lines[-50:]
            return {"logs": last_lines}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Log file not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading log file: {str(e)}")


@route.get("/get-game/{site}/{league}/{opponent_0}/{opponent_1}")
async def get_game(
        site: str,
        league: str,
        opponent_0: str,
        opponent_1: str
) -> dict:
    """
     Получает данные игры по составному ключу.

     Args:
         site (str): Сайт, откуда пришли данные.
         league (str): Название лиги.
         opponent_0 (str): Имя первой команды.
         opponent_1 (str): Имя второй команды.

     Returns:
         dict: Данные игры или сообщение об ошибке, если игра не найдена.
     """
    try:
        redis_client = RedisClient()
        await redis_client.connect()

        # Формируем ключ в нижнем регистре
        key = (f"{site.lower()}, {league.lower()}, "
               f"{opponent_0.lower()}, {opponent_1.lower()}")

        # Получаем данные из Redis
        data = await redis_client.get_last_items(key)

        if not data:
            raise HTTPException(status_code=404, detail=f"Игра {key} не найдена")

        return {"games": data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@route.get("/get-league-games/{league}")
async def get_league_games(
        league: str,
) -> dict:
    try:
        redis_client = RedisClient()
        await redis_client.connect()

        # Формируем ключ в нижнем регистре
        key_akty = f"akty.com_all_data, {league.lower()}"
        key_fb = f"fb.com_all_data, {league.lower()}"

        # Получаем данные всех игр лиги из Redis
        data_akty = await redis_client.get_last_items(key_akty, count=2400)
        data_fb = await redis_client.get_last_items(key_fb, count=2400)

        # Отбрасываем неактуальные матчи
        current_time = datetime.now()
        check_time = current_time - timedelta(minutes=45)
        check_time_str = check_time.strftime("%H:%M:%S")
        expired_matches_akty = [record['match'] for record in data_akty if record['server_time'] < check_time_str]
        expired_matches_fb = [record['match'] for record in data_fb if record['server_time'] < check_time_str]
        data_akty = [record for record in data_akty if record['match'] not in expired_matches_akty]
        data_fb = [record for record in data_fb if record['match'] not in expired_matches_fb]

        # Выбираем матчи идущие в лигах
        akty_matches = set(record['match'] for record in data_akty)
        fb_matches = set(record['match'] for record in data_fb)
        league_data = {
            'ob': list(akty_matches),
            'fb': list(fb_matches)
        }

        return {league: league_data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@route.get("/get-game-bets/{site}/{league}")
async def get_game_bets(
        site: str,
        league: str,
        match: str = None,
        bet: str = None
) -> dict:
    """
     Получает все данные коэффициентов игры по составному ключу.
    """
    try:
        redis_client = RedisClient()
        await redis_client.connect()

        # Формируем ключ в нижнем регистре
        key = f"{site.lower()}_all_data, {league.lower()}"

        # Получаем данные всех матчей лиги из Redis
        data = await redis_client.get_last_items(key, count=2400)

        if not data:
            raise HTTPException(status_code=404, detail=f"Информация по лиге {key} не найдена")

        # Если запрошен конкретный матч лиги, получаем данные по этому матчу
        if match:
            match_data = [record for record in data if record['match'] == match]

            if not match_data:
                raise HTTPException(status_code=404, detail=f"Игра {match} не найдена в лиге {key}")
            # Если запрошены коэффициенты, получаем по ним данные
            if bet:
                handicap = f"handicap_point_{bet[-1]}"
                bets = {record['server_time']: [record[bet], record[handicap]] for record in match_data}
                return {match: bets}

            return {"game": match_data}

        return {"games": data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@route.post("/update-token/")
async def update_token(new_token: str):
    """
    Эндпоинт для обновления токена в файле .env и перезапуска приложения.

    Args:
        new_token (str): Новый токен для обновления.

    Returns:
        dict: Статус обновления токена.
    """
    env_file_path = '/var/www/fastuser/data/www/api.parserchina.com/china_parser/.env'
    services_name = [
        'api.parserchina.service',
        'celery_service_akty.service',
        'celery_service_fb.service',
        'celery_beat_parser_china.service',
        'flower.service'
    ]

    try:
        # Загружаем текущее содержимое файла .env
        dotenv.load_dotenv(env_file_path)

        # Обновляем значение токена в окружении
        os.environ['TELEGRAM_BOT_TOKEN'] = new_token

        # Записываем обновленный токен в файл .env
        async with aiofiles.open(env_file_path, mode='w') as env_file:
            for key, value in os.environ.items():
                if key == 'TELEGRAM_BOT_TOKEN':
                    await env_file.write(f'TELEGRAM_BOT_TOKEN={new_token}\n')
                else:
                    await env_file.write(f'{key}={value}\n')

        # Формирование команды для перезапуска всех сервисов
        restart_command = " && ".join([f"systemctl restart {service}" for service in services_name])

        # Выполнение команды перезапуска
        subprocess.run(restart_command, shell=True, check=True)

        return {"status": "Token updated and application is restarting"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating token: {str(e)}")
