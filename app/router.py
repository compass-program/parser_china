import os
import asyncio
import aiofiles
import subprocess
import dotenv
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal
from services_app.tasks import parse_some_data
from app.schema import ParserRequest, ResponseMatch
from transfer_data.database import get_async_session
from transfer_data.redis_client import RedisClient
from app.models import league, match, coefficient

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


@route.get("/get-match-history/", response_model=ResponseMatch)
async def get_match_history(
        league_name: str,
        match_name: str,
        session: AsyncSession = Depends(get_async_session)
) -> dict:
    """
    Эндпоинт для получения истории матча с коэффициентами.

    Args:
        league_name (str): Название лиги.
        match_name (str): Название матча.
        session (AsyncSession): Сессия для выполнения запросов к БД.

    Returns:
        dict: История матча с коэффициентами.
    """
    try:
        league_name = league_name.lower()
        match_name = match_name.lower()
        stmt = (
            select(coefficient, match.c.bookmaker)
            .select_from(coefficient)
            .join(match, coefficient.c.match_id == match.c.id)
            .join(league, match.c.league_id == league.c.id)
            .filter(
                league.c.name == league_name,
                match.c.name == match_name
            )
        )
        result = await session.execute(stmt)
        data = result.mappings().all()
        if not data:
            raise HTTPException(status_code=404, detail="not found")

        return {"history": data[::-1]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@route.get("/get-bet/")
async def get_bet(
        league_name: str,
        match_name: str,
        bookmaker: str,
        bet_filter: str,
        bet_type: str,
        session: AsyncSession = Depends(get_async_session)
) -> dict:
    """
    Эндпоинт для получения истории коэффициента.

    Args:
        league_name (str): Название лиги.
        match_name (str): Название матча.
        bookmaker (str): Название букмекера('ob', 'fb').
        bet_filter (str): Значения фильтра для ставки exmpl('223.5', '+14.5', '-14,5').
        bet_type (str): Тип ставки(total_bet0/1, handicap_bet0/1).
        session (AsyncSession): Сессия для выполнения запросов к БД.

    Returns:
        dict: История коэффициента.
    """
    try:
        if bet_type.startswith('total'):
            condition = 'total_point'
        elif bet_type[-1] == '0':
            condition = 'handicap_point_0'
        else:
            condition = 'handicap_point_1'

        league_name = league_name.lower()
        match_name = match_name.lower()
        bookmaker = bookmaker.lower()
        stmt = (
            select(coefficient.c.server_time, getattr(coefficient.c, bet_type))
            .select_from(coefficient)
            .join(match, coefficient.c.match_id == match.c.id)
            .join(league, match.c.league_id == league.c.id)
            .filter(
                league.c.name == league_name,
                match.c.name == match_name,
                match.c.bookmaker == bookmaker,
                getattr(coefficient.c, condition) == bet_filter
            )
        )
        result = await session.execute(stmt)
        data = result.mappings().all()
        if not data:
            raise HTTPException(status_code=404, detail="not found")

        val_data = []
        prev_bet = None

        for record in data:
            res = dict(record)
            curr_bet = Decimal(res[f'{bet_type}'])
            if prev_bet is not None:
                diff = curr_bet - prev_bet
                if diff > 0:
                    res['bet_diff'] = '+' + f'{diff}'
                else:
                    res['bet_diff'] = f'{diff}'
                val_data.append(res)

            prev_bet = curr_bet

        return {"coeff_history": val_data[::-1]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
