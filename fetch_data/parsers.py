from fetch_data.akty import FetchAkty
from fetch_data.fb import OddsFetcher
from fetch_data.fav_check_parser import FavAkty

# Здесь указываем список парсеров, который запускается через Celery
parsers = {
    'FB': OddsFetcher,
    'FetchAkty': FetchAkty,
    'CheckAkty': FavAkty
}