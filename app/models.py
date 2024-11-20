from sqlalchemy import Integer, String, Table, Column, ForeignKey, Date, Boolean

from transfer_data.database import metadata

league = Table(
    "league",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(50), nullable=False, unique=True),
)

match = Table(
    "match",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("league_id", Integer, ForeignKey(league.c.id)),
    Column("bookmaker", String(10), nullable=False),
    Column("name", String(50), nullable=False),
)

coefficient = Table(
    "coefficient",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("match_id", Integer, ForeignKey(match.c.id, ondelete='CASCADE')),
    Column("score_game", String(10)),
    Column("total_point", String(10)),
    Column("total_bet_0", String(10)),
    Column("total_bet_1", String(10)),
    Column("handicap_point_0", String(10)),
    Column("handicap_bet_0", String(10)),
    Column("handicap_point_1", String(10)),
    Column("handicap_bet_1", String(10)),
    Column("time_game", String(10)),
    Column("server_time", String(10)),
)
