from __future__ import annotations

import asyncio
from typing import Optional

from game.engine import Game

# In-memory registry. For multi-process deployments, back this with Redis.
_games: dict[int, Game] = {}
_phase_tasks: dict[int, asyncio.Task] = {}
_phase_events: dict[int, asyncio.Event] = {}


def phase_event(chat_id: int) -> asyncio.Event:
    ev = _phase_events.get(chat_id)
    if ev is None:
        ev = asyncio.Event()
        _phase_events[chat_id] = ev
    return ev


def reset_phase_event(chat_id: int) -> asyncio.Event:
    ev = asyncio.Event()
    _phase_events[chat_id] = ev
    return ev


def get(chat_id: int) -> Optional[Game]:
    return _games.get(chat_id)


def create(chat_id: int, host_id: int) -> Game:
    game = Game(chat_id=chat_id, host_id=host_id)
    _games[chat_id] = game
    return game


def drop(chat_id: int) -> None:
    _games.pop(chat_id, None)
    _phase_events.pop(chat_id, None)
    task = _phase_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def set_task(chat_id: int, task: asyncio.Task) -> None:
    old = _phase_tasks.get(chat_id)
    if old and not old.done():
        old.cancel()
    _phase_tasks[chat_id] = task


def find_game_by_player(tg_id: int) -> Optional[Game]:
    for game in _games.values():
        if game.by_tg(tg_id):
            return game
    return None
