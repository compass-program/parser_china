from datetime import datetime, timezone, timedelta

MSK_TIMEZONE = timezone(timedelta(hours=3))

def get_moscow_time() -> datetime:
    """
    Получает текущее время в московском часовом поясе (UTC+3)
    """
    return datetime.now(MSK_TIMEZONE)

def convert_to_moscow_time(dt: datetime) -> datetime:
    """
    Конвертирует время в московский часовой пояс (UTC+3)
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK_TIMEZONE) 