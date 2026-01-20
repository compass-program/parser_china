import os
import re
import copy
import asyncio
import socketio
import hashlib
import traceback
import json
import undetected_chromedriver as uc
from typing import List, Dict, Any
from translatepy import Translator
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime, date
from sqlalchemy import insert, select
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from selenium.webdriver.common.keys import Keys
from app.logging import setup_logger
from app.models import coefficient, match
from transfer_data.redis_client import RedisClient
from transfer_data.telegram_bot import send_message_to_telegram
from transfer_data.database import db_connection
from scripts.translate_cash_load import load_translate_cash, save_translate_cash


# Загрузка переменных окружения из .env файла
load_dotenv()
# Настройка логгеров
logger = setup_logger('akty', 'akty_debug.log')
db_logger = setup_logger('db_requests', 'db_requests_debug.log')
LOCAL_DEBUG = 0
LEAGUES = {
    'IPBL篮球专业组': 'IPBL Pro Division',
    'IPBL女子篮球专业组': 'IPBL Pro Division Women',
    '火箭篮球联盟': 'Rocket Basketball League',
    '火箭女子篮球联盟': 'Rocket Basketball League Women',
}

PROXY = os.getenv('PROXY')
URL = os.getenv('AKTY_URL')
LOGIN = os.getenv('AKTY_LOGIN')
PASSWORD = os.getenv('AKTY_PASSWORD')
NAME_BOOKMAKER = 'akty.com'
REDIS_URL = os.getenv('REDIS_URL')
SOCKETIO_URL = os.getenv('SOCKETIO_URL')
SOCKET_KEY = os.getenv('SOCKET_KEY')
HEADLESS = True
# Пути для работы с файлами
REQUEST_FILE = 'request_ob.txt'
SCREENSHOT_FILE = 'screenshot_ob.png'


class FetchAkty:
    def __init__(
            self,
            url=URL,
            proxy=PROXY
    ):
        """
        Инициализация класса FetchAkty. Устанавливает URL
        и инициализирует WebDriver.
        """
        self.url = url
        self.proxy = proxy
        self.sio = socketio.AsyncSimpleClient()
        self.redis_client = None
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.driver = self.loop.run_until_complete(
            self.get_driver(headless=HEADLESS)
        )
        self.time_game_translate = {
            '第一节': 'I',
            '第二节': 'II',
            '第三节': 'III',
            '第四节': 'IV'
        }
        self.debug = LOCAL_DEBUG
        self.translate_cash = None
        self.action = ActionChains(self.driver)
        self.previous_data = {}
        self.translator = Translator()
        self.translate_cash = load_translate_cash()
        self.ended_games = {}
        self.history_data = []

    async def add_to_history(self, data: dict):
        """
        Добавляет данные в историю.

        Args:
            data (dict): Словарь с данными для сохранения.
        """
        try:
            if len(self.history_data) >= 18:
                matches = set()
                existing_data = self.history_data
                for item in existing_data:
                    matches.add((item['match'], item['league']))
                if not self.debug:
                    await self.write_to_db(existing_data, matches)
                self.history_data = []
            else:
                self.history_data.append(data)

        except Exception as e:
            await self.send_to_logs(f'Akty: Ошибка при сохранении данных в list(history_data): {str(e)}', db=True)

    @db_connection
    async def write_to_db(
            self,
            data: list,
            matches: set,
            session
    ):
        """
        Сохраняет данные в базу данных.

        Args:
            data (list): Список словарей с данными для сохранения.
            matches (set): Множество пар (игра, лига) для сохранения в базу.
            session : Сессия для работы с базой данных.
        """
        leagues_id = {
            "ipbl pro division": 1,
            "ipbl pro division women": 2,
            "rocket basketball league": 3,
            "rocket basketball league women": 4,
        }
        try:
            matches_id = {}
            try:
                for match_name, league_name in matches:
                    query = select(match.c.id).where((match.c.name == match_name) & (match.c.bookmaker == 'ob') & (match.c.league_id == leagues_id[league_name]))
                    result = await session.execute(query)
                    check = result.mappings().all()
                    if check:
                        matches_id[match_name] = check[0]['id']
                    else:
                        query = insert(match).values(name=match_name, league_id=leagues_id[league_name], bookmaker="ob")
                        result = await session.execute(query)
                        new_id, = result.inserted_primary_key
                        matches_id[match_name] = new_id
                        await session.commit()

            except Exception as e:
                await self.send_to_logs(f'Akty: Ошибка при сохранении матча: {str(e)}', db=True)

            try:
                for item in data:
                    stmt = insert(coefficient).values(
                        match_id=matches_id[item['match']],
                        score_game=item['score_game'],
                        total_point=item['total_point'],
                        total_bet_0=str(item['total_bet_0']),
                        total_bet_1=str(item['total_bet_1']),
                        handicap_bet_0=str(item['handicap_bet_0']),
                        handicap_bet_1=str(item['handicap_bet_1']),
                        handicap_point_0=item['handicap_point_0'],
                        handicap_point_1=item['handicap_point_1'],
                        time_game=item['time_game'],
                        server_time=item['server_time'],
                    )
                    result = await session.execute(stmt)
                    check, = result.inserted_primary_key
                    await session.commit()
                    if not check:
                        await self.send_to_logs('Akty: Нет отправленных данных', db=True)

                await self.send_to_logs('Akty: Данные сохранены', db=True)

            except Exception as e:
                await self.send_to_logs(f'Akty: Ошибка при сохранении коэффициентов: {str(e)}', db=True)

        except Exception as e:
            await self.send_to_logs(f'Akty: Ошибка при сохранении данных в базу данных: {str(e)}', db=True)

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
            key_for_all_data = (f"akty.com_all_data, {liga_name.lower()}, "
                   f"{opponent_0.lower()}, {opponent_1.lower()}")
            key_for_save = (f"akty.com, {liga_name.lower()}, "
                   f"{opponent_0.lower()}, {opponent_1.lower()}")
            data_rate['server_time'] = data.get('server_time', '')
            data_rate['time_game'] = data.get('time_game', '')
            json_data = json.dumps(data_rate, ensure_ascii=False)
            data_rate_match = copy.deepcopy(data_rate)
            data_rate_match['score_game'] = data.get('score_game')
            data_rate_match['match'] = f"{opponent_0.lower()}-{opponent_1.lower()}"
            data_rate_match['league'] = liga_name.lower()
            await self.add_to_history(data_rate_match)
            if not self.debug:
                await self.redis_client.add_to_list(key_for_all_data, json_data)
                if is_save:
                    await self.redis_client.add_to_list(key_for_save, json_data)
                # Проверяем, нужно ли отправить данные в Telegram
            is_send_tg = any(0 <
                 data_rate[rate_bet] <= 1.68 for rate_bet in rate_bets)
            if is_send_tg:
                key_fb = (f"fb.com_all_data, {liga_name.lower()}, "
                       f"{opponent_0.lower()}, {opponent_1.lower()}")
                # Получаем данные из Redis
                if not self.debug:
                    data_fb = await self.redis_client.get_last_item(key_fb)
                    if data_fb:
                        data_fb['site'] = 'FB'
                    data_rate.update({
                        'opponent_0': opponent_0,
                        'opponent_1': opponent_1,
                        'liga': liga_name,
                        'site': 'OB'
                    })
                    await send_message_to_telegram(
                        data_rate,
                        data_fb
                    )

        except Exception as e:
            await self.send_to_logs(f'Ошибка при сохранении данных: {str(e)}')

    async def send_data(
            self,
            data: dict
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
                f"Connecting to Socket.IO server at {SOCKETIO_URL}"
            )
            if not self.sio.connected:
                await self.sio.connect(SOCKETIO_URL,
                                   auth={'socket_key': SOCKET_KEY})
        except Exception as e:
            print(f"Error initializing async components: {e}")
            raise

    async def get_driver(
            self,
            headless: bool = False,
            retries: int = 3
    ) -> uc.Chrome:
        """
        Инициализирует и возвращает WebDriver для браузера Chrome.

        :param headless: Запуск браузера в headless режиме.
        :param retries: Количество попыток запуска WebDriver в случае ошибки.
        :return: WebDriver для браузера Chrome.
        """

        attempt = 0
        while attempt < retries:
            try:
                options = uc.ChromeOptions()
                if self.proxy:
                    proxy = f'socks5://{self.proxy}'
                    options.add_argument(f'--proxy-server={proxy}')
                driver = uc.Chrome(options=options, headless=headless)
                return driver
            except WebDriverException as e:
                attempt += 1
                logger.error(
                    f"Ошибка при запуске драйвера "
                    f"(попытка {attempt} из {retries}): {e}")
                if attempt >= retries:
                    raise e
                await asyncio.sleep(5)

    async def get_url(
            self,
            url: str
    ):
        """
        Загружает основную страницу по заданному URL.

        :param url: URL страницы для загрузки.
        """
        self.driver.get(url)

    async def scroll_to_element(
            self,
            element: WebElement
    ) -> None:
        """
        Прокручивает страницу до указанного элемента,
         чтобы он оказался по центру экрана.

        :param element: WebElement, до которого необходимо прокрутить страницу.
        """
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            element)

    async def scroll_to_bottom(
            self,
            wait_time: int = 5
    ) -> None:
        """
        Аккуратно прокручивает страницу до низа,
        ожидая указанное количество секунд.

        :param wait_time: Время ожидания в секундах перед началом прокрутки.
        """
        await asyncio.sleep(wait_time)
        scroll_pause_time = 0.1  # Уменьшенная пауза  для плавности
        scroll_step = 100  # Количество пикселей для каждой прокрутки

        last_height = self.driver.execute_script(
            "return document.body.scrollHeight")

        while True:
            self.driver.execute_script("window.scrollBy(0, arguments[0]);",
                                       scroll_step)
            await asyncio.sleep(scroll_pause_time)
            new_height = self.driver.execute_script(
                "return document.body.scrollHeight")

            if new_height == last_height:
                break
            last_height = new_height

    async def wait_for_element(
            self,
            by: By,
            value: str,
            timeout: int = 30
    ) -> WebElement:
        """
        Ожидает загрузки элемента на странице по заданным критериям.

        :param by: Стратегия поиска элемента (например, By.CSS_SELECTOR).
        :param value: Значение для поиска элемента.
        :param timeout: Время ожидания в секундах (по умолчанию 10 секунд).
        :return: Найденный элемент или None,
        если элемент не был найден в течение заданного времени.
        """
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            # Проверяем наличие элемента с сообщением о входе в другой сессии
            if await self.is_logged_in_elsewhere():

                await self.send_to_logs(
                    "Обнаружено сообщение о входе в другом месте. Перезапуск парсера."
                )
                await self.run()

            return element
        except TimeoutException:
            print(
                f"Элемент {by} {value} не был загружен в"
                f" течение заданного времени")
            if not self.debug:
                await self.sio.disconnect()
                self.driver.quit()
            else:
                breakpoint()

    async def is_logged_in_elsewhere(self) -> bool:
        """
        Проверяет, отображается ли на странице сообщение о входе в другом месте.

        :return: True, если сообщение отображается, иначе False.
        """
        try:
            message_element = self.driver.find_element(By.CSS_SELECTOR,
                                                       ".ant-mowin-s2-messageBox")
            if message_element and "账户在其它地方登录" in message_element.text:
                print(message_element.text())
                return True
        except NoSuchElementException:
            return False
        return False

    async def send_to_logs(
            self,
            message: str,
            db: bool = False,
    ):
        """
        Отправляет сообщение в логгер и выводит его в консоль.

        :param message: Сообщение для логгера.
        :param db: Сообщение для БД логгера (по умолчанию False).
        """
        if db:
            db_logger.info(message)
        else:
            if not self.debug:
                logger.info(message)
        print(f"Logger: {message}")

    async def check_changed_dict(
            self,
            existing_list: List[Dict[str, Any]],
            game_info: Dict[str, Any],
            liga_name: str,
    ) -> bool | None:
        """
        Проверяет и обновляет список словарей, если конкретный словарь изменился, или добавляет его, если его нет.

        :param existing_list: Список существующих словарей.
        :param game_info: Новый словарь для добавления или обновления.
        :param liga_name: Наименование лиги.
        :return: True, если данные изменились и были сохранены, иначе False.
        """
        new_dict = copy.deepcopy(game_info)
        for existing_dict in existing_list:
            if (existing_dict['opponent_0'] == new_dict['opponent_0'] and
                    existing_dict['opponent_1'] == new_dict['opponent_1']):
                if (existing_dict['rate'] != new_dict['rate']) and (
                        existing_dict['opponent_0'] != existing_dict[
                    'opponent_1']):
                    await self.save_games(new_dict, liga_name)
                    return True
                return False
        return True

    async def authorization(
            self
    ) -> None:
        """
        Авторизация на странице.
        """
        try:
            await self.get_url(self.url)
            login_input = await self.wait_for_element(By.CSS_SELECTOR,
                                                "input[placeholder*='账号']",
                                                timeout=90)
            await asyncio.sleep(15)
            login_input.send_keys(LOGIN)
            password_input = await self.wait_for_element(By.CSS_SELECTOR,
                                               "input[placeholder='密码']")
            password_input.clear()
            password_input.send_keys(PASSWORD)
            await asyncio.sleep(3)
            password_input.send_keys(Keys.ENTER)
            password_input.send_keys(Keys.ENTER)
            await self.send_to_logs('Авторизация успешно пройдена')
        except Exception as e:
            await self.send_to_logs(f"Ошибка авторизации: {e}")
            self.driver.save_screenshot(f'screenshot_{str(e)}.png')

    async def translate_and_cache(self, text: str) -> str:
        """
        Перевод строки на русский язык с кэшированием результата.

        Если строка пустая или после очистки становится пустой, возвращает исходную строку.
        """
        try:
            if not text:
                return text

            sanitized_name = text.translate(
                str.maketrans('', '', ' (),女')).strip().lower()

            if not sanitized_name:  # Проверяем, не стала ли строка пустой после очистки
                return text

            for key in self.translate_cash:
                if key in sanitized_name:
                    return self.translate_cash[key]

            translation = self.translator.translate(sanitized_name,
                                                    "english").result.lower()
            self.translate_cash = load_translate_cash()
            self.translate_cash[sanitized_name] = translation
            save_translate_cash(self.translate_cash)

            await self.send_to_logs(
                f"Перевод текста: '{sanitized_name}' перевод: '{translation}'"
            )

            return translation

        except Exception as e:
            await self.send_to_logs(
                f"Ошибка при переводе: {e}, текст: '{sanitized_name}'"
            )
            return text

    async def main_page(
            self
    ) -> None:
        """
        Переход с главной страницы.
        """
        """
        Загружает основную страницу по заданному URL с проверкой на элемент загрузки.
        """
        max_retries = 3

        for attempt in range(max_retries):
            try:
                await asyncio.sleep(5)
                self.driver.execute_script("window.scrollBy(0, 800);")
                await asyncio.sleep(5)
                bks_element = await self.wait_for_element(
                    By.CSS_SELECTOR,
                    "div[class*='styles__item_content__2IMkn']",
                    timeout=30
                )
                await self.scroll_to_element(bks_element)
                if bks_element:
                    await asyncio.sleep(10)
                    button_element = bks_element.find_element(
                        By.XPATH,
                        "//img[@src='/client/./assets/pic_sport_huanqiu@2x.668be1cd.png?x-oss-process=image/quality,Q_90/format,webp']"
                    )
                    if button_element.is_displayed() and button_element.is_enabled():
                        button_element.click()
                        return
                    else:
                        # Если элемент не кликабелен, перезагружаем страницу и повторяем
                        self.driver.refresh()
                        await asyncio.sleep(5)
                        continue
                else:
                    self.driver.refresh()
                    await asyncio.sleep(5)
                    continue

            except Exception as e:
                logger.error(
                    f"Попытка {attempt + 1}/{max_retries} не удалась: {str(e)}")
                if attempt < max_retries - 1:
                    self.driver.refresh()
                    await asyncio.sleep(5)
                else:
                    raise e

    async def aggregator_page(
            self
    ) -> None:
        """
        Переход на страницу агрегатора.
        """
        copyright_paragraph = await self.wait_for_element(
            By.CSS_SELECTOR,
            "p[class*='style__copyright']",
            timeout=30
        )
        await self.scroll_to_element(copyright_paragraph)
        await self.send_to_logs('Успешный вход в систему')
        iframe_element = await self.wait_for_element(
            By.CSS_SELECTOR,
            "iframe[title='venuIframe']",
            timeout=60
        )
        await asyncio.sleep(20)
        self.driver.switch_to.frame(iframe_element)
        basketball_element = await self.wait_for_element(
            By.XPATH, '//span[@class="menu-text" and text()="篮球"]',
            timeout=60
        )
        if basketball_element:
            basketball_element.click()
        else:
            window_size = self.driver.get_window_size()
            window_width = window_size['width']
            # Вычисляем координаты для клика в правый верхний угол
            right_upper_x = window_width - 1  # 1 пиксель левее правой границы
            right_upper_y = 1
            self.action.move_by_offset(right_upper_x, right_upper_y).click().perform()
            await self.aggregator_page()

        await self.send_to_logs('Успешный переход в раздел баскетбола')
        await asyncio.sleep(5)

        # Алгоритм проверки избранных лиг
        try:
            leagues_block = await self.wait_for_element(
                By.CSS_SELECTOR,
                "div.layout_main_center",
                timeout=30
            )

            hide_scroll = leagues_block.find_element(By.CSS_SELECTOR, "div.hide-scrollbar")
            items = hide_scroll.find_elements(By.CSS_SELECTOR, "div[class*='item yb-flex-center']")

            if not items or len(items) < 2:
                raise NoSuchElementException("Не найдены элементы меню (item yb-flex-center)")

            fav = items[1]

            # важно: дождаться кликабельности, а не просто presence
            WebDriverWait(self.driver, 15).until(EC.element_to_be_clickable(fav))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", fav)
            await asyncio.sleep(1)
            self.driver.execute_script("arguments[0].click();", fav)  # JS-клик как фолбек
            await self.send_to_logs("Нажали на кнопку избранное (items[1])")
            await asyncio.sleep(5)

        except Exception:
            await self.send_to_logs("Ищем и жмём '收藏' альтернативным способом")

            fav_xpath = (
                "//span[contains(@class,'text-name')][contains(normalize-space(.),'收藏')]"
                "/ancestor::div[contains(@class,'item')][1]"
            )

            fav_item = await self.wait_for_element(By.XPATH, fav_xpath, timeout=30)

            try:
                WebDriverWait(self.driver, 15).until(EC.element_to_be_clickable((By.XPATH, fav_xpath)))
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", fav_item)
                await asyncio.sleep(1)
                fav_item.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", fav_item)

            await asyncio.sleep(5)
            await self.send_to_logs("Нажали на кнопку избранное (fallback 收藏)")

    async def change_zoom(
            self
    ):
        self.driver.get('chrome://settings/appearance')
        self.driver.execute_script(
            'chrome.settingsPrivate.setDefaultZoom(0.25);'
        )

    async def get_content(
            self
    ):
        """
        Получение контента с страницы с 5 попытками.

        :return: Объект BeautifulSoup с содержимым HTML или None, если контент не найден.
        """
        max_attempts = 6
        attempt = 0

        while attempt < max_attempts:
            element = await self.wait_for_element(
                By.CSS_SELECTOR,
                "div[class*='v-scroll-content relative-position']",
                timeout=30
            )

            if element:
                html = element.get_attribute('outerHTML')
                soup = BeautifulSoup(html, 'html.parser')
                return soup
            logger.info(
                f"Внимание! Отсутствие контента на странице,"
                f" Попытка {attempt + 1} из {max_attempts} получить контент.")
            attempt += 1
            await asyncio.sleep(
                30)

        await self.send_to_logs(
            'Остановка парсера, не найден <div> с играми после 5 попыток.'
        )
        await self.sio.disconnect()
        self.driver.quit()
        return None

    async def get_container_hash(self) -> str:
        """
        Получение хэш-суммы контейнера с играми.
        :return: str
        """
        soup = await self.get_content()

        if not soup:
            return ''
        return hashlib.md5(str(soup).encode('utf-8')).hexdigest()

    async def click_element_by_text(self, card, league_name) -> None:
        """
        Нажимает на элемент для изменения его видимости,
        если текущий элемент не находится в нужном состоянии.

        :param card: HTML-элемент, содержащий информацию о лиге.
        :param league_name: Название лиги, для которой нужно изменить видимость.
        """
        try:
            # Проверяем стиль элемента card для определения текущего состояния
            if 'style' in card.attrs:
                card_style = card['style']

                # Блок кода ниже - возможное решение проблемы с исчезновением лиг
                if re.search(r'height:\s*37px;', card_style):
                    xpath = f"//div[contains(@class, 'list-card-wrap v-scroll-item relative-position')]//span[contains(text(), '{league_name}')]/ancestor::div[contains(@class, 'list-card-wrap v-scroll-item relative-position')]"

                    target = await self.wait_for_element(
                        By.XPATH,
                        xpath,
                        timeout=30
                    )

                    target.click()
                    await asyncio.sleep(5)
                # конец возможного решения проблемы, далее изначальный вид алгоритма функции

                # Если есть скрытые элементы (высота 37px), нажимаем кнопку для изменения состояния
                # if re.search(r'height:\s*37px;', card_style):
                #     spoiler_button = await self.wait_for_element(
                #         By.CSS_SELECTOR,
                #         "div[class*='match-type']",
                #         timeout=30)
                #     current_style = spoiler_button.get_attribute('style')
                #
                #     # Нажимаем на кнопку, если она в состоянии скрытия элементов
                #     if re.search(r'height:\s*37px;', current_style):
                #         spoiler_button.click()
                #         await asyncio.sleep(5)
                #         await self.send_to_logs(
                #             'Переключение видимости лиг произошло успешно')

            # Если элементы уже раскрыты или отсутствует стиль, ничего не делаем
        except Exception as e:
            await self.send_to_logs(f'При переключении произошла ошибка: {e}')
            await self.run()

        except Exception as e:
            await self.send_to_logs(f'При переключении произошла ошибка: {e}')
            await self.run()

    async def extract_league_data(self, target_leagues: dict) -> dict:
        """
        Парсит лиги/матчи из текущей разметки:
          - matc-type-card: секция (滚球盘/未开赛 и т.п.)
          - tid_title_num: заголовок лиги
          - tid_container_num: контейнер матчей для последнего заголовка лиги
        Возвращает только изменившиеся игры (как раньше), обновляет self.previous_data.
        """

        soup = await self.get_content()
        leagues_data = {NAME_BOOKMAKER: {}}
        previous_leagues_data = {NAME_BOOKMAKER: {}}

        if not soup:
            return leagues_data

        scroll_content = soup.find("div", class_="v-scroll-content relative-position")
        if not scroll_content:
            return leagues_data

        cards = scroll_content.find_all(
            "div",
            class_=re.compile(r"\bmatch-list-card\b"),
            recursive=False
        )
        if not cards:
            return leagues_data

        current_league_raw = None

        seen_in_pass = set()

        for card in cards:
            cls_list = card.get("class", [])
            cls = " ".join(cls_list)

            # секции типа "滚球盘/未开赛" нам не нужны
            if "matc-type-card" in cls:
                continue

            # заголовок лиги
            if "tid_title_num" in cls:
                span = card.select_one("span.ellipsis.allow-user-select")
                current_league_raw = span.get_text(strip=True) if span else None
                continue

            # контейнер матчей
            if "tid_container_num" not in cls:
                continue

            if not current_league_raw or current_league_raw not in target_leagues:
                continue

            league_name = target_leagues[current_league_raw]

            match_items = card.select("div.c-match-item")
            if not match_items:
                continue

            leagues_data[NAME_BOOKMAKER].setdefault(league_name, [])
            previous_leagues_data[NAME_BOOKMAKER].setdefault(league_name, [])

            for m_i, item in enumerate(match_items):
                try:
                    # команды: иногда классы team-home/team-away могут быть кривыми, поэтому берём первые 2 team-item
                    teams = item.select("div.row-item.team-item")
                    if len(teams) < 2:
                        continue
                    team0, team1 = teams[0], teams[1]

                    def team_name(t) -> str:
                        el = t.select_one(".team-name span.ellipsis1") or t.select_one("span.ellipsis1")
                        return el.get_text(strip=True) if el else t.get_text(" ", strip=True)

                    opponent_0_name = team_name(team0)
                    opponent_1_name = team_name(team1)
                    if not opponent_0_name or not opponent_1_name:
                        continue

                    tr_0 = await self.translate_and_cache(opponent_0_name)
                    tr_1 = await self.translate_and_cache(opponent_1_name)

                    match_key = (league_name, tr_0.strip().lower(), tr_1.strip().lower())
                    if match_key in seen_in_pass:
                        continue
                    seen_in_pass.add(match_key)

                    # счёт: в твоём HTML score — это div.score с текстом (без span)
                    def score(t) -> str:
                        sc = t.select_one("div.score")
                        return sc.get_text(strip=True) if sc else ""

                    sc0 = score(team0)
                    sc1 = score(team1)

                    # время/статус
                    process_time = ""
                    span_time = item.select_one("span.timer-layout2")
                    if span_time:
                        process_time = span_time.get_text(strip=True)

                    process_time_text = ""
                    div_proc = item.select_one("div.process_name")
                    if div_proc:
                        process_time_text = self.time_game_translate.get(div_proc.get_text(strip=True), "")

                    # рынки
                    oneXtwo_home = oneXtwo_away = oneXtwo_draw = ""
                    h_bet_0 = h_bet_1 = ""
                    h_point_0 = h_point_1 = ""
                    t_bet_0 = t_bet_1 = ""
                    t_point = ""

                    cols = item.select("div.handicap-col")
                    # 1X2
                    if len(cols) > 0:
                        cells = cols[0].select("div.c-bet-item")
                        for c in cells:
                            label_el = c.select_one("div.handicap-value-text")
                            odd_el = c.select_one("div.highlight-odds")
                            label = label_el.get_text(strip=True) if label_el else ""
                            odd = odd_el.get_text(strip=True) if odd_el else ""
                            if label == "主":
                                oneXtwo_home = odd
                            elif label == "客":
                                oneXtwo_away = odd
                            elif label == "平":
                                oneXtwo_draw = odd

                    # фора
                    if len(cols) > 1:
                        odds = cols[1].select("div.highlight-odds")
                        pts = cols[1].select("div.handicap-value-text")
                        h_bet_0 = odds[0].get_text(strip=True).replace("EU ", "") if len(odds) > 0 else ""
                        h_bet_1 = odds[1].get_text(strip=True).replace("EU ", "") if len(odds) > 1 else ""
                        h_point_0 = pts[0].get_text(strip=True) if len(pts) > 0 else ""
                        h_point_1 = pts[1].get_text(strip=True) if len(pts) > 1 else ""

                    # тотал
                    if len(cols) > 2:
                        odds = cols[2].select("div.highlight-odds")
                        pts = cols[2].select("div.handicap-value-text")
                        t_bet_0 = odds[0].get_text(strip=True).replace("EU ", "") if len(odds) > 0 else ""
                        t_bet_1 = odds[1].get_text(strip=True).replace("EU ", "") if len(odds) > 1 else ""
                        # обычно тотал лежит в первом handicap-value-text ("3.5")
                        t_point = pts[0].get_text(strip=True) if len(pts) > 0 else ""

                    server_time = datetime.now(tz=ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S")

                    game_info = {
                        "opponent_0": tr_0,
                        "opponent_1": tr_1,
                        "score_game": f"{sc0}:{sc1}",
                        "time_game": f"{process_time_text} {process_time}".strip(),
                        "rate": {
                            "oneXtwo_home": oneXtwo_home,
                            "oneXtwo_away": oneXtwo_away,
                            "oneXtwo_draw": oneXtwo_draw,
                            "total_point": t_point,
                            "total_bet_0": t_bet_0,
                            "total_bet_1": t_bet_1,
                            "handicap_point_0": h_point_0,
                            "handicap_bet_0": h_bet_0,
                            "handicap_point_1": h_point_1,
                            "handicap_bet_1": h_bet_1,
                        },
                        "server_time": server_time,
                    }

                    # changed-логика как у тебя
                    if self.previous_data and league_name in self.previous_data.get(NAME_BOOKMAKER, {}):
                        changed = await self.check_changed_dict(
                            self.previous_data[NAME_BOOKMAKER][league_name],
                            game_info,
                            league_name,
                        )
                        if changed:
                            leagues_data[NAME_BOOKMAKER][league_name].append(game_info)

                    previous_leagues_data[NAME_BOOKMAKER][league_name].append(game_info)

                except Exception as e:
                    await self.send_to_logs(f"extract: match[{m_i}] parse error: {e}", db=True)
                    continue

        # финализация как раньше
        await self.update_ended_games(leagues_data, previous_leagues_data)
        self.previous_data = previous_leagues_data

        leagues_data[NAME_BOOKMAKER] = {
            k: v for k, v in leagues_data[NAME_BOOKMAKER].items() if v
        }
        return leagues_data

    async def update_ended_games(self, leagues_data: dict, previous_leagues_data: dict) -> None:
        # Быстрое множество текущих игр по уникальному ключу
        current_keys = set()
        for league, games in previous_leagues_data.get(NAME_BOOKMAKER, {}).items():
            for g in games:
                current_keys.add((league, g.get("opponent_0"), g.get("opponent_1")))

        # ended_games: увеличиваем счётчик если игра исчезла
        for league, games in previous_leagues_data.get(NAME_BOOKMAKER, {}).items():
            for g in games:
                key = (league, g.get("opponent_0"), g.get("opponent_1"))
                if key not in current_keys:
                    continue  # на всякий

        # ВАЖНО: сравниваем “новые распарсенные” (previous_leagues_data) с “теми что реально есть сейчас”
        # Тут текущими считаем previous_leagues_data (это “сейчас”), а прошлым — self.previous_data (это “вчера”)
        prev = self.previous_data.get(NAME_BOOKMAKER, {}) if self.previous_data else {}

        now_keys = set()
        for league, games in previous_leagues_data.get(NAME_BOOKMAKER, {}).items():
            for g in games:
                now_keys.add((league, g.get("opponent_0"), g.get("opponent_1")))

        prev_keys = set()
        for league, games in prev.items():
            for g in games:
                prev_keys.add((league, g.get("opponent_0"), g.get("opponent_1")))

        disappeared = prev_keys - now_keys

        for key in disappeared:
            if key not in self.ended_games:
                self.ended_games[key] = {"info": None, "count": 1}
            else:
                self.ended_games[key]["count"] += 1

            if self.ended_games[key]["info"] is None:
                # найдём последний известный game_info
                league, o0, o1 = key
                for g in prev.get(league, []):
                    if g.get("opponent_0") == o0 and g.get("opponent_1") == o1:
                        self.ended_games[key]["info"] = g
                        break

            if self.ended_games[key]["count"] >= 2000:
                info = self.ended_games[key]["info"]
                if info:
                    info["is_end_game"] = True
                    await self.delete_games(info, key[0])
                del self.ended_games[key]

        # если игра снова появилась — убираем из ended_games
        for key in list(self.ended_games.keys()):
            if key in now_keys:
                del self.ended_games[key]

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

            # Логируем успешное удаление
            await self.send_to_logs(
                f"Данные для ключей '{', '.join(keys)}' успешно удалены.")

        except Exception as e:
            await self.send_to_logs(f'Ошибка при удалении данных: {e}')

    async def monitor_leagues(
        self,
        target_leagues: dict,
        check_interval: int = 1
    ) -> None:
        """
        Мониторинг данных лиг.

        :param target_leagues: Словарь с данными целевых лиг.
        :param check_interval: Интервал проверки в секундах.
        """
        previous_hash = await self.get_container_hash()
        unchanged_count = 0
        max_unchanged_checks = 3600

        while True:
            await asyncio.sleep(check_interval)
            await self.request_check()
            current_hash = await self.get_container_hash()

            if current_hash != previous_hash:
                try:
                    leagues_data = await self.extract_league_data(target_leagues)
                    previous_hash = current_hash
                    unchanged_count = 0
                    if leagues_data:
                        await self.send_data(leagues_data)
                except Exception:
                    await self.send_to_logs(f'Ошибка: {traceback.format_exc()}')
            else:
                unchanged_count += 1

            # Если данные не изменялись в течение max_unchanged_checks раз
            if unchanged_count >= max_unchanged_checks:
                await self.send_to_logs(
                    f"Данные не изменились более {max_unchanged_checks} раз. Перезапуск."
                )
                await self.run()

    async def close(self):
        if self.driver:
            self.driver.quit()
            await self.send_to_logs("Драйвер был закрыт принудительно")
        if self.redis_client:
            await self.redis_client.close()

    def __del__(self):
        asyncio.run(self.close())

    async def request_check(self):
        # Проверяем наличие запроса
        if os.path.exists(REQUEST_FILE):
            print("Запрос на создание скриншота получен.")
            try:
                # Создаем скриншот
                self.driver.save_screenshot(SCREENSHOT_FILE)
                print(f"Скриншот сохранен в {SCREENSHOT_FILE}")
            except Exception as e:
                print(f"Ошибка при создании скриншота: {e}")

            # Удаляем файл-запрос
            os.remove(REQUEST_FILE)

    async def run(self, *args, **kwargs):
        """
        Запуск парсера с указанными параметрами и перезапуском при ошибках.

        Args:
            *args: Позиционные аргументы.
            **kwargs: Именованные аргументы.
        """
        leagues = kwargs.get('leagues', LEAGUES)
        attempt = 0
        max_retries = 5

        while attempt < max_retries:
            try:
                if not self.debug:
                    self.redis_client = RedisClient()
                    await self.redis_client.connect()

                # await self.change_zoom()
                await self.init_async_components()

                await self.authorization()
                await self.main_page()
                await self.aggregator_page()

                await self.monitor_leagues(leagues)

            except Exception as e:
                if self.driver and self.driver.session_id:
                    self.driver.save_screenshot(
                        f'screenshot_akty_{attempt}.png')
                await self.send_to_logs(
                    f"Произошла ошибка: {str(e)}. Попытка {attempt + 1} из {max_retries}."
                )
                await asyncio.sleep(10)
                attempt += 1
                if attempt >= max_retries:
                    await self.send_to_logs(
                        "Достигнуто максимальное количество попыток. Остановка.")
                    break
            finally:
                if self.redis_client:
                    await self.redis_client.close()
                if self.driver:
                    self.driver.quit()


if __name__ == "__main__":
    LOCAL_DEBUG = 1
    HEADLESS = False
    fetch_akty = FetchAkty()
    asyncio.run(fetch_akty.run())
