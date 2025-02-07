import os
import re
import asyncio
import undetected_chromedriver as uc
from typing import List, Dict, Any
from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from selenium.webdriver.common.keys import Keys
from app.logging import setup_logger


# Загрузка переменных окружения из .env файла
load_dotenv()
# Настройка логгеров
logger = setup_logger('fav_akty', 'fav_akty_debug.log')
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
REDIS_URL = os.getenv('REDIS_URL')
HEADLESS = True


class FavAkty:
    def __init__(
            self,
            url=URL,
            proxy=PROXY
    ):
        """
        Инициализация класса FavAkty. Устанавливает URL
        и инициализирует WebDriver.
        """
        self.url = url
        self.proxy = proxy
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.driver = self.loop.run_until_complete(
            self.get_driver(headless=HEADLESS)
        )
        self.debug = LOCAL_DEBUG
        self.action = ActionChains(self.driver)

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
                    options.add_argument(f'--proxy-server={self.proxy}')
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
        Загружает основную страницу по-заданному URL.

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
        scroll_pause_time = 0.1  # Уменьшенная пауза для плавности
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
    ):
        """
        Отправляет сообщение в логгер и выводит его в консоль.

        :param message: Сообщение для логгера.
        """
        if not self.debug:
            logger.info(message)
        print(f"Logger: {message}")

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
                await asyncio.sleep(15)
                ul_element = await self.wait_for_element(
                    By.CLASS_NAME,
                    "header__venue__3IZlT",
                    timeout=30
                )
                if ul_element:
                    span_element = ul_element.find_element(
                        By.XPATH,
                        ".//span[text()='体育']"
                    )
                    self.action.move_to_element(span_element).perform()
                    await asyncio.sleep(2)
                    # Проверка кликабельности элемента

                    h4_element = await self.wait_for_element(
                        By.XPATH,
                        "//img[@src='https://senbackkg.m42i79a.com/main-consumer-web/assets-oss/ak/images/header/ty-hq.862daf053a4b08ea6650a4e85ece1711.webp?x-oss-process=image/resize,w_210,h_210/quality,Q_100/sharpen,100/format,webp']"
                    )

                    if h4_element.is_displayed() and h4_element.is_enabled():
                        h4_element.click()
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

    async def check_favorite_pin(self, target_leagues: dict) -> None:
        """
        Проверяет наличие избранных лиг в блоке с лигами.
        При отсутствии статуса избранного у нужной лиги добавляет её в избранное.

        :param target_leagues: Словарь с названиями нужных лиг.
        """
        match_list_block = await self.wait_for_element(
            By.CSS_SELECTOR,
            "div[class*='v-scroll-content relative-position']"
        )
        await asyncio.sleep(5)

        button = match_list_block.find_element(By.CSS_SELECTOR, "div[class*='match-type']")
        button.click()
        print('Свернули карточки с лигами')
        await asyncio.sleep(5)

        leagues_cards = match_list_block.find_elements(
            By.CSS_SELECTOR,
            "div[class*='match-list-card v-scroll-item relative-position tid_title_num']"
        )
        await asyncio.sleep(5)

        if leagues_cards:
            print(f'Нашли карточки заголовков лиг в кол-ве: {len(leagues_cards)}')
            leagues = target_leagues.keys()
            for card in leagues_cards:
                try:
                    league_element = card.find_element(By.CSS_SELECTOR, "span[class*='ellipsis allow-user-select']")
                    league_name = league_element.text
                    if league_name in leagues:
                        print(f'Чекаем на избранное лигу: {league_name}')
                        fav_element = card.find_element(
                            By.CSS_SELECTOR,
                            "div[class*='icon-wrap m-star-wrap-pin specialty-collect']"
                        )
                        fav_icon_element = fav_element.find_element(By.TAG_NAME, "span")
                        fav_style = fav_icon_element.get_attribute('style')
                        pattern = r'resource/(.+?)\.svg'
                        match = re.search(pattern, fav_style)
                        if match:
                            check_value = "4dc811b0bc8e11efa267bd6434b1d6a3"
                            extract_value = match.group(1)
                            if extract_value == check_value:
                                fav_icon_element.click()
                                await self.send_to_logs(f'Лига "{league_name}" успешно добавлена в избранное')
                                await asyncio.sleep(5)
                            else:
                                await self.send_to_logs(f'Лига "{league_name}" уже находится в избранном')
                                await asyncio.sleep(1)

                except TimeoutException as e:
                    await self.send_to_logs(f'Время ожидания загрузки истекло: {str(e)}')
                    continue
                except NoSuchElementException as e:
                    await self.send_to_logs(f'Не найден элемент: {str(e)}')
                    continue

                else:
                    print(f'Лига "{league_name}" не найдена в списке нужных лиг')
                    await asyncio.sleep(1)

    async def change_zoom(
            self
    ):
        self.driver.get('chrome://settings/appearance')
        self.driver.execute_script(
            'chrome.settingsPrivate.setDefaultZoom(0.25);'
        )

    async def close(self):
        if self.driver:
            self.driver.quit()
            await self.send_to_logs("Драйвер был закрыт принудительно")

    def __del__(self):
        asyncio.run(self.close())

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
                await self.change_zoom()
                await self.authorization()
                await self.main_page()
                await self.aggregator_page()
                await self.check_favorite_pin(leagues)

                break

            except Exception as e:
                if self.driver and self.driver.session_id:
                    self.driver.save_screenshot(
                        f'screenshot_fav_akty_{attempt}.png')
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
                if self.driver:
                    self.driver.quit()
                return f"Избранное проверено"


if __name__ == "__main__":
    LOCAL_DEBUG = 1
    HEADLESS = False
    fav_akty = FavAkty()
    asyncio.run(fav_akty.run())
