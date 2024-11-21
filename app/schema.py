from pydantic import BaseModel
from typing import List, Optional


class ParserRequest(BaseModel):
    """
    Валидация данных для проверки
    """
    parser_name: str
    args: list = []
    kwargs: dict = {}


class MatchCoeff(BaseModel):
    """
    Модель для коэффициентов матчей
    """
    id: int
    match_id: int
    score_game: str
    total_point: str
    total_bet_0: str
    total_bet_1: str
    handicap_point_0: str
    handicap_bet_0: str
    handicap_point_1: str
    handicap_bet_1: str
    time_game: str
    server_time: str
    bookmaker: str


class ResponseMatch(BaseModel):
    """
    Модель для ответа с информацией о матче
    """
    history: List[MatchCoeff | dict]
