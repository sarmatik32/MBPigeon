"""Game stats, ELO and achievements service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    GameParticipant, GameRecord, User, UserAchievement, UserStats,
)
from game.engine import MAFIA_TEAM, Role, Winner

ELO_K = 32
STARTING_ELO = 1000


ACHIEVEMENTS: dict[str, dict] = {
    "first_game":  {"name_en": "Rookie",        "name_ru": "Новичок",        "desc_en": "Play your first game",            "desc_ru": "Сыграть первую партию",    "reward": 50},
    "first_win":   {"name_en": "First Blood",   "name_ru": "Первая кровь",   "desc_en": "Win your first game",             "desc_ru": "Выиграть первую партию",    "reward": 100},
    "veteran":     {"name_en": "Veteran",       "name_ru": "Ветеран",        "desc_en": "Play 25 games",                   "desc_ru": "Сыграть 25 партий",         "reward": 250},
    "mafia_10":    {"name_en": "Made Man",      "name_ru": "Свой парень",    "desc_en": "Win 10 games as Mafia",           "desc_ru": "10 побед за Мафию",         "reward": 500},
    "town_10":     {"name_en": "Upstanding",    "name_ru": "Гражданин",      "desc_en": "Win 10 games as Town",            "desc_ru": "10 побед за Мирных",        "reward": 500},
    "sherlock":    {"name_en": "Sherlock",      "name_ru": "Шерлок",         "desc_en": "Win 5 games as Sheriff",          "desc_ru": "5 побед за Шерифа",         "reward": 300},
    "hippocrates": {"name_en": "Hippocrates",   "name_ru": "Гиппократ",      "desc_en": "Win 5 games as Doctor",           "desc_ru": "5 побед за Доктора",        "reward": 300},
    "godfather":   {"name_en": "Godfather",     "name_ru": "Крёстный отец",  "desc_en": "Win 3 games as Don",              "desc_ru": "3 победы за Дона",          "reward": 400},
    "psycho":      {"name_en": "Psycho",        "name_ru": "Психопат",       "desc_en": "Win as Maniac",                   "desc_ru": "Победа за Маньяка",         "reward": 400},
    "elo_1200":    {"name_en": "Climber",       "name_ru": "Восходящий",     "desc_en": "Reach 1200 ELO",                  "desc_ru": "Достичь 1200 ELO",          "reward": 300},
    "elo_1500":    {"name_en": "Master",        "name_ru": "Мастер",         "desc_en": "Reach 1500 ELO",                  "desc_ru": "Достичь 1500 ELO",          "reward": 800},
}


@dataclass
class GameEndReport:
    deltas: dict[int, int]                    # tg_id -> ELO delta
    unlocked: dict[int, list[str]]            # tg_id -> achievement codes newly unlocked
    reward_coins: dict[int, int]              # tg_id -> coins from achievements


def role_team(role: Role) -> str:
    if role in MAFIA_TEAM:
        return "mafia"
    if role == Role.MANIAC:
        return "maniac"
    return "town"


def winner_team(w: Winner) -> str:
    return {Winner.TOWN: "town", Winner.MAFIA: "mafia", Winner.MANIAC: "maniac"}[w]


def _expected(rating: int, opponent: int) -> float:
    return 1.0 / (1 + 10 ** ((opponent - rating) / 400))


def _role_stat_attr(role: Role) -> str:
    return {
        Role.DON: "as_don", Role.MAFIA: "as_mafia",
        Role.SHERIFF: "as_sheriff", Role.DOCTOR: "as_doctor",
        Role.LOVER: "as_lover", Role.MANIAC: "as_maniac",
        Role.CIVILIAN: "as_civilian",
    }[role]


async def _get_or_create_stats(s: AsyncSession, user_id) -> UserStats:
    st = await s.get(UserStats, user_id)
    if st is None:
        st = UserStats(user_id=user_id)
        s.add(st)
        await s.flush()
    return st


async def record_game_end(s: AsyncSession, game, winner: Winner) -> GameEndReport:
    """Persist game record, update stats/ELO, check achievements. Non-DB fields
    (deltas/unlocked) are returned for UI announcement."""
    w_team = winner_team(winner)
    tg_ids = [p.tg_id for p in game.players]
    users_rows = (await s.execute(select(User).where(User.telegram_id.in_(tg_ids)))).scalars().all()
    by_tg = {u.telegram_id: u for u in users_rows}

    # Split teams for ELO averaging
    won_users: list[User] = []
    lost_users: list[User] = []
    for p in game.players:
        u = by_tg.get(p.tg_id)
        if not u:
            continue
        (won_users if role_team(p.role) == w_team else lost_users).append(u)

    avg_won = sum(u.elo for u in won_users) / max(1, len(won_users))
    avg_lost = sum(u.elo for u in lost_users) / max(1, len(lost_users))

    record = GameRecord(
        chat_id=game.chat_id, winner=w_team,
        round_count=game.round, players_count=len(game.players),
    )
    s.add(record)
    await s.flush()

    deltas: dict[int, int] = {}
    unlocked: dict[int, list[str]] = {}
    rewards: dict[int, int] = {}

    for p in game.players:
        u = by_tg.get(p.tg_id)
        if not u:
            continue
        team = role_team(p.role)
        won = team == w_team
        opp = avg_lost if won else avg_won
        expected = _expected(u.elo, int(opp))
        delta = round(ELO_K * ((1.0 if won else 0.0) - expected))
        u.elo += delta
        deltas[p.tg_id] = delta

        s.add(GameParticipant(
            game_id=record.game_id, user_id=u.user_id,
            role=p.role.value, survived=p.alive, won=won, elo_delta=delta,
        ))

        # Stats upsert
        st = await _get_or_create_stats(s, u.user_id)
        st.games_played += 1
        if won:
            st.wins += 1
            if team == "mafia": st.mafia_wins += 1
            elif team == "maniac": st.maniac_wins += 1
            else: st.town_wins += 1
        else:
            st.losses += 1
        if not p.alive:
            st.deaths += 1
        setattr(st, _role_stat_attr(p.role), getattr(st, _role_stat_attr(p.role)) + 1)
        st.last_game_at = datetime.utcnow()

        # Check achievements
        new_codes = await _check_achievements(s, u, st, p.role, won)
        if new_codes:
            unlocked[p.tg_id] = new_codes
            rewards[p.tg_id] = sum(ACHIEVEMENTS[c]["reward"] for c in new_codes)

    await s.flush()
    return GameEndReport(deltas=deltas, unlocked=unlocked, reward_coins=rewards)


async def _check_achievements(
    s: AsyncSession, u: User, st: UserStats, role: Role, won: bool,
) -> list[str]:
    """Return list of newly-unlocked achievement codes for this user."""
    owned = set((await s.execute(
        select(UserAchievement.code).where(UserAchievement.user_id == u.user_id)
    )).scalars().all())

    new_codes: list[str] = []

    def award(code: str) -> None:
        if code in owned or code in new_codes:
            return
        new_codes.append(code)

    if st.games_played >= 1:
        award("first_game")
    if st.wins >= 1:
        award("first_win")
    if st.games_played >= 25:
        award("veteran")
    if st.mafia_wins >= 10:
        award("mafia_10")
    if st.town_wins >= 10:
        award("town_10")
    if role == Role.SHERIFF and won and st.as_sheriff >= 5:
        award("sherlock")
    if role == Role.DOCTOR and won and st.as_doctor >= 5:
        award("hippocrates")
    if role == Role.DON and won and st.as_don >= 3:
        award("godfather")
    if role == Role.MANIAC and won:
        award("psycho")
    if u.elo >= 1200:
        award("elo_1200")
    if u.elo >= 1500:
        award("elo_1500")

    # Persist + grant coin reward.
    from services import economy  # avoid circular at module load
    for code in new_codes:
        s.add(UserAchievement(user_id=u.user_id, code=code))
        reward = ACHIEVEMENTS[code]["reward"]
        u.coin_balance += reward  # direct credit, bypasses daily cap on purpose
    return new_codes


# ---------------- read helpers for UI ----------------

async def get_stats(s: AsyncSession, user_id) -> UserStats:
    return await _get_or_create_stats(s, user_id)


async def top_rating(s: AsyncSession, limit: int = 10) -> list[User]:
    rows = (await s.execute(
        select(User).order_by(User.elo.desc()).limit(limit)
    )).scalars().all()
    return list(rows)


async def user_achievements(s: AsyncSession, user_id) -> list[str]:
    rows = (await s.execute(
        select(UserAchievement.code).where(UserAchievement.user_id == user_id)
    )).scalars().all()
    return list(rows)
