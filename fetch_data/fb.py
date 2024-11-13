import os
import re
import copy
import socketio
import json
import asyncio
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from translatepy import Translator
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.remote.webelement import WebElement
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from app.logging import setup_logger
from transfer_data.redis_client import RedisClient
from transfer_data.telegram_bot import send_message_to_telegram
from scripts.translate_cash_load import save_translate_cash, load_translate_cash

# Загрузка переменных окружения из .env файла
load_dotenv()
URL = "https://test.f66b88sport.com/pc/index.html#/"

LEAGUES = {
    'IPBL篮球专业组': 'IPBL Pro Division',
    'IPBL女子篮球专业组': 'IPBL Pro Division Women',
    '火箭篮球联盟': 'Rocket Basketball League',
    '火箭女子篮球联盟': 'Rocket Basketball League Women',
}
LOCAL_DEBUG = 0
REDIS_URL = os.getenv('REDIS_URL')
SOCKETIO_URL = os.getenv('SOCKETIO_URL')
SOCKET_KEY = os.getenv('SOCKET_KEY')
HEADLESS = True

# Настройка логгера
logger = setup_logger('fb', 'fb_debug.log')


class OddsFetcher:
    def __init__(
            self,
    ):
        """
        Инициализация класса OddsFetcher.
        Устанавливает URL и инициализирует WebDriver.

        :param url: URL страницы для загрузки.
        :param leagues: Список целевых лиг.
        :param headless: Запуск браузера в headless режиме.
        """
        self.url = URL
        self.sio = socketio.AsyncSimpleClient()
        self.redis_client = None
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.driver_fb = self.loop.run_until_complete(
            self.get_driver(headless=HEADLESS)
        )
        self.time_game_translate = {
            '第一节': 'I',
            '第二节': 'II',
            '第三节': 'III',
            '第四节': 'IV'
        }
        self.debug = LOCAL_DEBUG
        self.actions = None
        self.translator = Translator()
        self.previous_data = {}
        self.translate_cash = load_translate_cash()
        self.ended_games = {}
        self.connection_error_count = 0
        self.max_connection_errors = 5

    async def get_driver(
            self,
            headless: bool = False,
    ) -> uc.Chrome:
        """
        Инициализирует и возвращает WebDriver для браузера Chrome.
        :param headless: Запуск браузера в headless режиме.
        """
        return uc.Chrome(options=uc.ChromeOptions(), headless=headless)

    async def get_url(
            self
    ):
        """
        Загружает основную страницу по заданному URL.

        :param url: URL страницы для загрузки.
        """
        self.driver_fb.get(self.url)

    async def get_page(self):
        """
        Загружает основную страницу по заданному URL с проверкой на элемент загрузки.
        Если элемент загрузки виден слишком долго, перезагружает страницу.
        """
        max_retries = 3
        wait_time = 10  # Время ожидания для исчезновения элемента

        for attempt in range(max_retries):
            try:
                # Перезагрузка страницы
                await self.get_url()
                await asyncio.sleep(5)  # Ожидание перед проверкой
                # Проверка наличия элемента загрузки
                try:
                    loading_element = WebDriverWait(self.driver_fb,
                                                    wait_time).until(
                        EC.presence_of_element_located((
                            By.CSS_SELECTOR,
                            'div.q-loading.fullscreen.column.flex-center.z-max.text-black'
                        ))
                    )
                    await self.send_to_logs(
                        f"Элемент загрузки найден на странице {self.url} (попытка {attempt + 1})")
                except TimeoutException:
                    # Элемент загрузки не найден, продолжаем выполнение
                    await self.send_to_logs(
                        f"Элемент загрузки не найден на странице {self.url}. Продолжаем выполнение (попытка {attempt + 1})")
                    break

                # Ожидание исчезновения элемента загрузки
                try:
                    WebDriverWait(self.driver_fb, wait_time).until_not(
                        EC.visibility_of(loading_element)
                    )
                    await self.send_to_logs(
                        f"Элемент загрузки исчез, страница загружена {self.url} (попытка {attempt + 1})")
                    break  # Элемент загрузки исчез, продолжаем выполнение

                except TimeoutException:
                    # Элемент загрузки не исчез
                    await self.send_to_logs(
                        f"Элемент загрузки не исчез на странице {self.url}, перезагрузка страницы... (попытка {attempt + 1})")
                    self.driver_fb.refresh()
                    continue  # Повторная попытка загрузки страницы

            except Exception as e:
                await self.send_to_logs(
                    f"Произошла ошибка: {e}. Попытка {attempt + 1} из {max_retries}.")
                if attempt + 1 >= max_retries:
                    raise e
                await asyncio.sleep(5)  # Ожидание перед повторной попыткой

        else:
            await self.send_to_logs(
                f"Не удалось загрузить страницу без элемента загрузки после {max_retries} попыток")
            raise Exception(
                "Не удалось загрузить страницу без элемента загрузки.")

    async def save_games(self, data: dict, liga_name: str):
        """
        Сохраняет игры по отдельным ключам в Redis.

        Args:
            data (dict): Данные в формате JSON для сохранения.
            liga_name (str): Наименование лиги для сохранения в Redis.
        """
        try:
            rate_bets = [
                'total_bet_0',
                'total_bet_1',
                'handicap_bet_0',
                'handicap_bet_1'
            ]
            data_rate = data.get('rate', {})

            # Преобразуем значения в data_rate
            for rate_bet in rate_bets:
                value = data_rate.get(rate_bet, '0')
                if value in ('-', '', None):
                    data_rate[rate_bet] = 0.0
                else:
                    try:
                        data_rate[rate_bet] = float(value)
                    except (ValueError, TypeError):
                        data_rate[rate_bet] = 0.0

            # Проверяем, нужно ли сохранять данные в Redis
            is_save = any(0 < data_rate[rate_bet] <= 1.73 for rate_bet in (
                    rate_bets))
            opponent_0 = data.get('opponent_0', '')
            opponent_1 = data.get('opponent_1', '')

            data_rate['server_time'] = data.get('server_time', '')
            data_rate['time_game'] = data.get('time_game', '')
            json_data = json.dumps(data_rate, ensure_ascii=False)
            data_rate_match = data_rate.copy()
            data_rate_match['match'] = f"{opponent_0.lower()}:{opponent_1.lower()}"
            json_all_data = json.dumps(data_rate_match, ensure_ascii=False)
            key_for_league_data = f"fb.com_all_data, {liga_name.lower()}"
            key_for_all_data = (f"fb.com_all_data, {liga_name.lower()}, "
                            f"{opponent_0.lower()}, {opponent_1.lower()}")
            key_for_save = (f"fb.com, {liga_name.lower()}, "
                   f"{opponent_0.lower()}, {opponent_1.lower()}")
            if not self.debug:
                await self.redis_client.add_to_list(key_for_all_data, json_data)
                await self.redis_client.add_to_list(key_for_league_data, json_all_data, max_len=2400)
                if is_save:
                    await self.redis_client.add_to_list(
                        key_for_save,
                        json_data
                    )
                # Проверяем, нужно ли отправить данные в Telegram
            is_send_tg = any(0 <
                data_rate[rate_bet] <= 1.68 for rate_bet in rate_bets)

            if is_send_tg:
                key_akty = (f"akty.com_all_data, {liga_name.lower()}, "
                       f"{opponent_0.lower()}, {opponent_1.lower()}")
                # Получаем данные из Redis
                data_akty = await self.redis_client.get_last_item(key_akty)
                if data_akty:
                    data_akty['site'] = 'OB'
                data_rate.update({
                    'opponent_0': opponent_0,
                    'opponent_1': opponent_1,
                    'liga': liga_name,
                    'site': 'FB'
                })
                if not self.debug:
                    await send_message_to_telegram(
                        data_rate,
                        data_akty
                    )

        except Exception as e:
            await self.send_to_logs(f'Ошибка при сохранении данных: {str(e)}')

    async def delete_games(self, data: dict, liga_name: str):
        """
        Удаляет игры по ключам из Redis.

        Args:
            data (dict): Данные с информацией о ключах для удаления.
            liga_name (str): Наименование лиги для удаления данных в Redis.
        """
        try:
            # Получаем оппонентов и преобразуем их к нижнему регистру
            opponent_0 = data.get('opponent_0', '').lower()
            opponent_1 = data.get('opponent_1', '').lower()
            liga_name_lower = liga_name.lower()

            # Базовая часть ключей
            base_key = f"{liga_name_lower}, {opponent_0}, {opponent_1}"

            # Генерируем ключи с использованием замены части строки
            keys = [
                f"akty.com, {base_key}",
                f"akty.com_all_data, {base_key}",
                f"fb.com, {base_key}",
                f"fb.com_all_data, {base_key}"
            ]

            if not self.debug:
                # Удаляем данные из Redis по ключам
                for key in keys:
                    await self.redis_client.delete_data(key)

        except Exception as e:
            await self.send_to_logs(f'Ошибка при удалении данных: {e}')

    async def send_data(
            self,
            data: dict,
    ):
        """
        Отправка данных на Socket.IO сервер и сохранение в Redis.

        :param data: Данные для отправки и сохранения.
        """

        if self.debug:
            await self.send_to_logs(
                "Режим отладки включен, данные не отправляются."
            )
            await self.send_to_logs(
                f'{data}'
            )
            return
        try:
            json_data = json.dumps(data, ensure_ascii=False)
            # Отправляем данные на Socket.IO сервер напрямую
            await self.sio.emit('message', json_data)
        except Exception as e:
            await self.send_to_logs(f'Ошибка при отправке данных: {str(e)}')

    async def init_async_components(self):
        """
        Инициализация асинхронных компонентов, таких как Redis клиент и подключение к Socket.IO.
        """

        if self.debug:
            return None
        try:
            await self.send_to_logs(
                f"Connecting to Redis at {REDIS_URL}"
            )
            await self.send_to_logs(
                f"Connecting to Socket.IO server at {SOCKETIO_URL}"
            )
            if not self.sio.connected:
                await self.sio.connect(SOCKETIO_URL, auth={'socket_key': SOCKET_KEY})
        except Exception as e:
            print(f"Error initializing async components: {e}")
            raise

    async def send_to_logs(
            self,
            message: str
    ):
        """
        Логирование сообщений.

        :param message: Сообщение для логирования.
        """
        if not self.debug:
            logger.info(message)
        print(f"Logger: {message}")

    async def wait_for_element(
            self,
            by: By,
            value: str,
            timeout: int = 10,
    ) -> WebElement:
        """
        Ожидает загрузки элемента на странице по заданным критериям.

        :param by: Стратегия поиска элемента (например, By.CSS_SELECTOR).
        :param value: Значение для поиска элемента.
        :param timeout: Время ожидания в секундах (по умолчанию 10 секунд).
        :param is_debug: Для остановки во время отладки.
        :return: Найденный элемент или None,
        если элемент не был найден в течение заданного времени.
        """
        try:
            element = WebDriverWait(self.driver_fb, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            return element
        except TimeoutException:
            return None

    async def main_page(
            self
    ) -> None:
        max_attempts = 6
        attempt = 0

        while attempt < max_attempts:
            try:
                # Ожидаем исчезновения элемента загрузки
                loading_element = await self.wait_for_element(
                    By.CSS_SELECTOR,
                    'div.q-loading.fullscreen.column.flex-center.z-max.text-black',
                    timeout=10
                )
                if loading_element:
                    await self.send_to_logs(
                        f"Элемент загрузки найден на странице (попытка {attempt + 1}). Ожидание его исчезновения..."
                    )
                    await asyncio.sleep(
                        5)  # Ждем некоторое время перед повторной попыткой
                    attempt += 1
                    continue

                # Ищем и кликаем на кнопку баскетбола
                basketball_button = await self.wait_for_element(
                    By.CSS_SELECTOR,
                    '.ui-carousel-item.sport-type-item img[src="sport-svg/sport_id_3.svg"]',
                    timeout=30
                )
                if basketball_button:
                    basketball_button.click()
                    await self.send_to_logs(
                        'Успешный переход в баскетбольную лигу')
                    return

                # Если кнопка не найдена, перезагружаем страницу
                logger.info(
                    f"Внимание! Отсутствие контента на странице, попытка {attempt + 1} из {max_attempts} получить контент.")
                attempt += 1
                self.driver_fb.refresh()
                await asyncio.sleep(10)

            except Exception as e:
                await self.send_to_logs(
                    f"Произошла ошибка: {str(e)}. Попытка {attempt + 1} из {max_attempts}.")
                attempt += 1
                await asyncio.sleep(5)

        # Если все попытки не увенчались успехом, отключаемся и закрываем браузер
        await self.send_to_logs(
            'Остановка парсера, не удалось перейти в баскетбольную лигу после 5 попыток.')
        await self.sio.disconnect()
        self.driver_fb.quit()

    async def get_translate(self, short_name: str) -> str:
        """
        Получает полное название команды, используя кэш или выполнив перевод текста на английский.

        Если частичное совпадение с ключом найдено, возвращает значение.
        Если перевод не найден, переводит текст на английский и добавляет в кэш.

        Args:
            short_name (str): Короткое название команды.

        Returns:
            str: Полное название команды на английском языке.
        """
        if not short_name:
            return short_name

        # Убираем символы из short_name
        sanitized_name = short_name.translate(
            str.maketrans('', '', ' (),女')).strip().lower()

        if not sanitized_name:
            return short_name

        # Ищем частичное совпадение
        for key in self.translate_cash:
            if sanitized_name in key:
                return self.translate_cash[key]

        # Если совпадение не найдено, выполняем перевод
        try:
            translation = self.translator.translate(sanitized_name,
                                                    "english").result.lower()
            self.translate_cash = load_translate_cash()
            self.translate_cash[sanitized_name] = translation
            save_translate_cash(self.translate_cash)

            # Логируем новый перевод
            await self.send_to_logs(
                f"Перевод текста: '{sanitized_name}', перевод: '{translation}'"
            )

            return translation

        except Exception as e:
            # Логируем ошибку перевода
            await self.send_to_logs(
                f"Ошибка перевода текста: '{short_name}', ошибка: {str(e)}"
            )
            return short_name

    async def check_changed_dict(
            self,
            existing_list: List[Dict[str, Any]],
            game_info: Dict[str, Any],
            liga_name: str
    ) -> bool:
        """
        Проверяет и обновляет список словарей, если конкретный словарь изменился, или добавляет его, если его нет.

        :param existing_list: Список существующих словарей.
        :param game_info: Новый словарь для добавления или обновления.
        :param liga_name: Наименование лиги.
        :return: True, если данные изменились и были сохранены, иначе False.
        """
        new_dict = copy.deepcopy(game_info)
        opponent_0 = game_info['opponent_0']
        opponent_1 = game_info['opponent_1']

        # Проверка существования игры в списке существующих
        for existing_dict in existing_list:
            if (existing_dict['opponent_0'] == opponent_0 and
                    existing_dict['opponent_1'] == opponent_1):
                if (existing_dict['rate'] != new_dict['rate']) and (
                        existing_dict['opponent_0'] != existing_dict['opponent_1']):
                    await self.save_games(new_dict, liga_name)
                    return True
                return False
        return False

    async def collect_odds_data(self, target_leagues: dict):
        """
        Сбор данных о коэффициентах для заданных лиг.

        :param target_leagues: dict, содержащий целевые лиги для извлечения данных.
        """
        active_matches = {"fb.com": {}}
        previous_leagues_data = {"fb.com": {}}
        try:
            html = self.driver_fb.page_source
            soup = BeautifulSoup(html, 'html.parser')
            match_groups = soup.select('.home-match-list-box .group-matches')

            for group in match_groups:
                league_name_element = group.select_one('.league-name')
                if league_name_element is None:
                    continue
                league_name = league_name_element.text
                if league_name in target_leagues:
                    liga_name_translate = target_leagues[league_name]
                    if liga_name_translate not in active_matches["fb.com"]:
                        active_matches["fb.com"][liga_name_translate] = []

                    matches = group.select(
                        '.home-match-list__item.home-match-info')

                    for match in matches:
                        team_names = match.select(
                            '.match-teams-name .team-name')
                        if len(team_names) != 2:
                            continue

                        short_team1_name = team_names[0].text.strip()
                        short_team2_name = team_names[1].text.strip()
                        translate_opponent_0_name = await self.get_translate(
                            short_team1_name) if short_team1_name != '' else ''
                        translate_opponent_1_name = await self.get_translate(
                            short_team2_name) if short_team2_name != '' else ''

                        scores = match.select('.match-score p span')
                        if len(scores) != 2:
                            continue

                        opponent_0_score = scores[0].text
                        opponent_1_score = scores[1].text

                        game_info = {
                            'opponent_0': translate_opponent_0_name,
                            'opponent_1': translate_opponent_1_name,
                            'score_game': f'{opponent_0_score}:{opponent_1_score}',
                            'time_game': '',
                            'rate': {
                                'total_point': '',
                                'total_bet_0': '',
                                'total_bet_1': '',
                                'handicap_point_0': '',
                                'handicap_bet_0': '',
                                'handicap_point_1': '',
                                'handicap_bet_1': '',
                            },
                            'server_time': '',
                            'is_end_game': False
                        }

                        process_time_elements = match.select('.time')
                        if process_time_elements:
                            process_time_text_element = process_time_elements[
                                0].select_one('.match-left-text.font-din')
                            process_time_text = self.time_game_translate.get(
                                process_time_text_element.get_text().strip(),
                                '') if process_time_text_element else ""
                            game_info['time_game'] += process_time_text

                            process_time_element = process_time_elements[
                                0].select_one('.match-left-time.font-din')
                            if process_time_element and process_time_element.text.strip():
                                process_time = process_time_element.text.strip()
                                game_info['time_game'] += ' ' + process_time

                        game_info['server_time'] = datetime.now(
                            tz=ZoneInfo("Europe/Moscow")
                        ).strftime("%H:%M:%S")

                        odds_boxes = match.select('.home-match-odds-box')
                        found_handicap = False
                        found_ou = False
                        for odds_box in odds_boxes:
                            category = odds_box.get('class', '')
                            if 'match-full-odds-handicap' in category and not found_handicap:
                                found_handicap = True
                                odds_items = odds_box.select(
                                    '.team-odds-list .value.font-din')
                                if len(odds_items) >= 2:
                                    game_info['rate']['handicap_bet_0'] = \
                                    odds_items[0].text
                                    game_info['rate']['handicap_bet_1'] = \
                                    odds_items[1].text
                                handicap_points = odds_box.select(
                                    '.team-odds-list .prefix-text.text-grey-disable')
                                if len(handicap_points) >= 2:
                                    game_info['rate']['handicap_point_0'] = \
                                    handicap_points[0].text
                                    game_info['rate']['handicap_point_1'] = \
                                    handicap_points[1].text
                            elif 'match-full-odds-total' in category and not found_ou:
                                found_ou = True
                                odds_items = odds_box.select(
                                    '.team-odds-list .value.font-din')
                                if len(odds_items) >= 2:
                                    game_info['rate']['total_bet_0'] = \
                                    odds_items[0].text
                                    game_info['rate']['total_bet_1'] = \
                                    odds_items[1].text
                                total_points = odds_box.select(
                                    '.team-odds-list .prefix-text.text-grey-disable')
                                if len(total_points) >= 2:
                                    point_0 = total_points[0].text
                                    point_0_cleared = re.sub(r'[大小 ]', '',
                                                             point_0)
                                    game_info['rate'][
                                        'total_point'] = point_0_cleared if point_0_cleared else \
                                    game_info['rate']['total_point']

                        if (
                                self.previous_data and liga_name_translate in self.previous_data.get(
                                "fb.com", {})):
                            changed_data = await self.check_changed_dict(
                                self.previous_data["fb.com"][
                                    liga_name_translate],
                                game_info,
                                liga_name_translate
                            )
                            if changed_data:
                                active_matches["fb.com"][
                                    liga_name_translate].append(game_info)
                        else:
                            active_matches["fb.com"][
                                liga_name_translate].append(game_info)

                        # Обновляем previous_leagues_data после каждой итерации
                        if liga_name_translate not in previous_leagues_data[
                            "fb.com"]:
                            previous_leagues_data["fb.com"][
                                liga_name_translate] = []
                        previous_leagues_data["fb.com"][
                            liga_name_translate].append(game_info)
                        # Проверка завершенных игр перед обновлением данных
            await self.check_finished_games(
                previous_leagues_data,
                active_matches,
            )

            self.previous_data = previous_leagues_data

            # Удаляем пустые словари перед отправкой
            active_matches["fb.com"] = {k: v for k, v in
                                        active_matches["fb.com"].items() if v}

            # Отправляем данные, только если они изменились
            if any(active_matches["fb.com"].values()):
                await self.send_data(active_matches)
            self.connection_error_count = 0
        except Exception as e:
            logger.error(f"Error in collect_odds_data: {str(e)}")
            if "Connection refused" in str(
                    e) or "Max retries exceeded with url" in str(e):
                self.connection_error_count += 1  # Увеличиваем счетчик
                await self.send_to_logs(
                    f"Ошибка подключения: {str(e)}. Попытка {self.connection_error_count} из {self.max_connection_errors}.")

                if self.connection_error_count >= self.max_connection_errors:
                    await self.send_to_logs(
                        "Превышено максимальное количество попыток восстановления соединения. Перезапуск процесса.")
                    await self.restart_fetcher()
                else:
                    await asyncio.sleep(10)  # Пауза перед повторной попыткой
            else:
                # Если другая ошибка, логируем и перезапускаем весь процесс
                await self.send_to_logs(
                    f"Произошла ошибка: {str(e)}. Перезапуск.")
                await self.restart_fetcher()

    async def restart_fetcher(self):
        """
        Перезапускает процесс сбора данных путем повторного запуска методов.
        """
        # Закрываем WebDriver, если он открыт
        if self.driver_fb:
            self.driver_fb.quit()

        # Инициализируем драйвер заново и сбрасываем счетчик ошибок
        self.driver_fb = await self.get_driver(headless=HEADLESS)
        self.connection_error_count = 0  # Сброс счетчика ошибок

        # Повторно запускаем процесс
        await self.run(leagues=LEAGUES)

    async def check_finished_games(self, current_data: Dict[str, Any],
                                   active_matches: Dict[str, Any]) -> None:
        """
        Проверяет и отмечает завершенные игры, которые пропали из текущих данных.

        :param current_data: Текущие данные о лигах и играх.
        :param active_matches: Словарь с активными матчами, куда добавляются завершенные игры.
        """
        for site, leagues in self.previous_data.items():
            for league, games in leagues.items():
                for game in games:
                    # Проверка на наличие лиги в текущих данных
                    if league not in current_data.get(site, {}):
                        await self._mark_game_as_ended(site, league, game)
                        continue

                    # Проверка на наличие игры в текущих данных
                    if game not in current_data[site][league]:
                        await self._mark_game_as_ended(site, league, game)

        # Обработка завершенных игр с учетом счетчика
        for game_key, game_info in list(self.ended_games.items()):
            if game_info['counter'] >= 2000:
                game = game_info['game']
                game['is_end_game'] = True
                league = game.get('league_name')
                if league:
                    active_matches["fb.com"].setdefault(league, []).append(game)
                    await self.delete_games(game, league)
                del self.ended_games[game_key]

    async def _mark_game_as_ended(self, site: str, league: str,
                                  game: dict) -> None:
        """
        Отмечает игру как завершенную или увеличивает счетчик завершенных игр.

        :param site: Название сайта.
        :param league: Название лиги.
        :param game: Данные игры.
        """
        game_key = await self._generate_game_key(site, league, game)
        if game_key in self.ended_games:
            self.ended_games[game_key]['counter'] += 1
        else:
            game['league_name'] = league
            self.ended_games[game_key] = {'game': game, 'counter': 1}

    async def _generate_game_key(self, site: str, league: str,
                           game: Dict[str, Any]) -> str:
        """
        Генерирует уникальный ключ для игры на основе сайта, лиги и имен команд.

        :param site: Название сайта, откуда получены данные.
        :param league: Название лиги.
        :param game: Данные о конкретной игре.
        :return: Уникальный строковый ключ для игры.
        """
        return f"{site}_{league}_{game['opponent_0']}_{game['opponent_1']}"

    async def close(self):
        if self.driver_fb:
            self.driver_fb.quit()
            await self.send_to_logs("Драйвер был закрыт принудительно")
        if self.redis_client:
            await self.redis_client.close()

    def __del__(self):
        asyncio.run(fetcher.close())

    async def run(self, *args, **kwargs):
        """
        Основной метод для запуска парсера с перезапуском при ошибках.
        """
        leagues = kwargs.get('leagues', LEAGUES)
        attempt = 0
        max_retries = 5

        while attempt < max_retries:
            try:
                if not self.debug:
                    self.redis_client = RedisClient()
                    await self.redis_client.connect()

                await self.init_async_components()
                await self.get_page()
                await self.main_page()

                while True:
                    await self.collect_odds_data(leagues)
                    await asyncio.sleep(1)  # Пауза между циклами сбора данных
            except Exception as e:
                if self.driver_fb and self.driver_fb.session_id:
                    self.driver_fb.save_screenshot(
                        f'screenshot_fb_{attempt}.png'
                    )
                await self.send_to_logs(
                    f"Произошла ошибка: {str(e)}. "
                    f"Попытка {attempt + 1} из {max_retries}."
                )
                attempt += 1
                await asyncio.sleep(10)

                if attempt >= max_retries:
                    await self.send_to_logs(
                        "Достигнуто максимальное количество попыток. Остановка.")
                    break
            finally:
                if self.redis_client is not None:
                    await self.redis_client.close()
                if self.driver_fb:
                    self.driver_fb.quit()

if __name__ == "__main__":
    LOCAL_DEBUG = 1
    HEADLESS = False
    fetcher = OddsFetcher()
    asyncio.run(fetcher.run())
