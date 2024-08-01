import os
import socketio
from dotenv import load_dotenv
from app.logging import setup_logger

# Загрузка переменных окружения из .env файла
load_dotenv()

# Настройка логгера
logger = setup_logger('socketio', 'socketio_debug.log')

origins = [
    "https://api.parserchina.com",
    "http://api.parserchina.com",
    "http://parserchina.com",
    "https://parserchina.com",
]

sio = socketio.AsyncServer(
    async_mode="asgi",
    allow_upgrades=False,
    cors_allowed_origins=origins,
    namespaces='/socket.io',
    max_http_buffer_size=10 * 1024 * 1024  # 10 MB
)

app = socketio.ASGIApp(sio)

# Предопределенные пароли
SOCKET_KEY = os.getenv('SOCKET_KEY')


async def send_to_logs(message: str):
    """
    Отправляет сообщение в логгер и выводит его в консоль.

    :param message: Сообщение для логгера.
    """
    logger.info(message)
    print(f"Logger: {message}")


@sio.event
async def connect(sid: str, environ: dict, auth: dict):
    """
    Обработчик события подключения клиента.

    :param sid: Идентификатор сессии клиента.
    :param environ: Среда окружения.
    :param auth: Данные для авторизации.
    """
    if auth is None or 'socket_key' not in auth or auth['socket_key'] != SOCKET_KEY:
        await send_to_logs(f"Неудачная попытка подключения: {sid}, {auth}")
        return False  # Отклонить подключение
    await send_to_logs(f"Клиент подключился: {sid}")

@sio.on('disconnect')
async def disconnect(sid: str):
    """
    Обработчик события отключения клиента.

    :param sid: Идентификатор сессии клиента.
    """

    await send_to_logs(f"Клиент отключился: {sid}")

@sio.on('message')
async def message(sid: str, data: str):
    """
    Обработчик события получения сообщения от клиента.

    :param sid: Идентификатор сессии клиента.
    :param data: Данные, полученные от клиента.
    """
    await send_to_logs(f"Получено сообщение от {sid}: {data}")
    await sio.send(data)

