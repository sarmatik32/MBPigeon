from __future__ import annotations

import asyncio
from html import escape
import logging
from contextlib import suppress
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    FSInputFile,
)
from sqlalchemy import select
from db.models import Perk, UserPerk

from config import settings
from db.models import User
from db.session import SessionLocal
from game import manager
from game.engine import Game, MAFIA_TEAM, Phase, Player, Role, Winner
from services import economy, stats as stats_service
from services.i18n import SafeHtml, t

log = logging.getLogger(__name__)
router = Router()

GAME_LANG = "uk"
NIGHT_IMAGE = "assets/game/night.jpg"
DAY_IMAGE = "assets/game/day.jpg"
VOTE_IMAGE = "assets/game/vote.jpg"
BOT_URL = "https://t.me/krrig_alerts_gamebot"
SURVIVOR_REWARD = 200


def _role_name(lang: str, role: Role) -> str:
    return t(lang, f"role_{role.value}")


async def _lang_for(tg_id: int) -> str:
    # The game UI is intentionally Ukrainian regardless of the user's general bot locale.
    return GAME_LANG


async def _chat_lang(chat_id: int) -> str:
    return GAME_LANG


async def _safe_send(bot: Bot, chat_id: int, text: str, **kwargs):
    """Send a message robustly: handle flood control and malformed HTML.

    The bot uses global HTML parse mode. Any accidental/unsupported tag in a
    dynamic string must never be allowed to kill the game phase loop, so a
    Telegram entity parsing error falls back to plain text.
    """
    for attempt in range(4):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramRetryAfter as e:
            delay = max(1, int(e.retry_after))
            log.warning("Telegram flood control for chat %s; retrying in %ss (attempt %s/4)",
                        chat_id, delay, attempt + 1)
            await asyncio.sleep(delay)
        except TelegramBadRequest as e:
            if "can't parse entities" not in str(e).lower():
                log.error("Telegram rejected message in chat %s: %s", chat_id, e)
                return None
            plain = _plain_text(text)
            if plain == str(text) and kwargs.get("parse_mode") is None:
                log.error("Telegram entity parsing failed in chat %s: %s", chat_id, e)
                return None
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["parse_mode"] = None
            try:
                return await bot.send_message(chat_id, plain, **fallback_kwargs)
            except TelegramRetryAfter as retry:
                await asyncio.sleep(max(1, int(retry.retry_after)))
            except Exception:
                log.exception("Plain-text fallback failed in chat %s", chat_id)
                return None
    log.error("Telegram flood control persisted for chat %s; skipping this message after retries", chat_id)
    return None


def _plain_text(text: str) -> str:
    """Remove Telegram HTML tags/entities for safe parse_mode=None fallback."""
    import re
    from html import unescape
    return unescape(re.sub(r"<[^>]*>", "", str(text)))


async def _safe_edit_text(message: Message, text: str, **kwargs):
    """Edit a game message without letting malformed Telegram HTML crash a handler."""
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "can't parse entities" not in str(e).lower():
            log.error("Telegram rejected edited message: %s", e)
            return None
        fallback_kwargs = dict(kwargs)
        fallback_kwargs["parse_mode"] = None
        try:
            return await message.edit_text(_plain_text(text), **fallback_kwargs)
        except Exception:
            log.exception("Plain-text edit fallback failed")
            return None
    except Exception:
        log.exception("Failed to edit game message")
        return None


def _mention(player: Player) -> str:
    """Safe clickable Telegram profile mention with owned cosmetic title/badges."""
    name = escape(str(player.name), quote=True)
    prefix = ""
    if getattr(player, "badges", ""):
        prefix += escape(str(player.badges), quote=True) + " "
    if getattr(player, "title", ""):
        prefix += f"[{escape(str(player.title), quote=True)}] "
    return SafeHtml(f'{prefix}<a href="tg://user?id={player.tg_id}">{name}</a>')


def _host_name(game: Game) -> str:
    host = next((p for p in game.players if p.tg_id == game.host_id), None)
    if host:
        return _mention(host)
    return SafeHtml(escape(str(getattr(game, "host_name", "Host")), quote=True))


def _players_block(game: Game) -> SafeHtml:
    return SafeHtml("\n".join(
        f"{p.number or '?'} — {_mention(p)}" + ("" if p.alive else " ☠️")
        for p in game.players
    ))


async def _has_active_perk(tg_id: int, code: str) -> bool:
    async with SessionLocal() as s:
        user = (await s.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()
        if not user:
            return False
        rows = (await s.execute(
            select(UserPerk).join(Perk, Perk.perk_id == UserPerk.perk_id)
            .where(UserPerk.user_id == user.user_id, Perk.code == code)
        )).scalars().all()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return any((r.expires_at is None or r.expires_at > now) and (r.uses_left is None or r.uses_left > 0) for r in rows)


def _lobby_text(lang: str, game: Game) -> str:
    lst = SafeHtml("\n".join(f"• {_mention(p)}" for p in game.players) or "—")
    return t(lang, "lobby_board",
             host=_host_name(game), count=len(game.players),
             min=game.MIN_PLAYERS, list=lst)


def _lobby_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_join"), callback_data="lobby:join")],
        [InlineKeyboardButton(text=t(lang, "btn_start"), callback_data="lobby:start"),
         InlineKeyboardButton(text=t(lang, "btn_settings"), callback_data="lobby:settings")],
        [InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="lobby:cancel")],
    ])


def _settings_keyboard(lang: str, game: Game) -> InlineKeyboardMarkup:
    s = game.settings
    def tog(label_key: str, flag: bool, action: str) -> InlineKeyboardButton:
        mark = "✅" if flag else "⬜️"
        return InlineKeyboardButton(text=f"{mark} {t(lang, label_key)}",
                                    callback_data=f"set:{action}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [tog("set_don", s.allow_don, "don")],
        [InlineKeyboardButton(text=t(lang, "set_mode", mode=s.mode), callback_data="set:mode")],
        [InlineKeyboardButton(text=t(lang, "set_night", secs=s.night_seconds), callback_data="set:night")],
        [InlineKeyboardButton(text=t(lang, "set_discussion", secs=s.discussion_seconds), callback_data="set:discussion"),
         InlineKeyboardButton(text=t(lang, "set_vote", secs=s.vote_seconds), callback_data="set:vote")],
        [InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="set:back")],
    ])


def _targets_keyboard(game: Game, action: str, actor: Player, lang: str, *,
                      exclude_self: bool = False, exclude_mafia: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for p in game.alive_players():
        if exclude_self and p.tg_id == actor.tg_id:
            continue
        if exclude_mafia and p.role in MAFIA_TEAM:
            continue
        rows.append([InlineKeyboardButton(
            text=f"#{p.number} {str(p.name)}",
            callback_data=f"night:{action}:{p.number}",
        )])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_nothing"), callback_data="night:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _vote_keyboard(game: Game, lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"#{p.number} {str(p.name)}",
                                  callback_data=f"day:vote:{p.number}")]
            for p in game.alive_players()]
    rows.append([InlineKeyboardButton(text=t(lang, "btn_no_vote"), callback_data="day:no_vote")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tally_text(lang: str, game: Game) -> str:
    tally: dict[int, int] = {}
    for num in game.day_votes.votes.values():
        tally[num] = tally.get(num, 0) + 1
    no_votes = sum(1 for num in game.day_votes.votes.values() if num == 0)
    lines = []
    for num, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        p = next((x for x in game.players if x.number == num), None)
        if p:
            lines.append(f"#{num} {_mention(p)}: {count}")
    if no_votes:
        lines.append(f"🚫: {no_votes}")
    if not lines:
        return SafeHtml(t(lang, "vote_tally"))
    return SafeHtml(t(lang, "vote_tally") + "\n" + "\n".join(lines))


# ----------------- dead-player chat restriction -----------------

@router.message(F.chat.type.in_({"group", "supergroup"}))
async def block_dead_player_messages(msg: Message) -> None:
    """Prevent eliminated players from writing in the game chat until the game ends.

    The bot must have Telegram permission to delete messages in the group.
    All message types are covered because this handler is registered on Message.
    """
    game = manager.get(msg.chat.id)
    if not game or game.is_over() or not msg.from_user:
        raise SkipHandler

    player = game.by_tg(msg.from_user.id)
    if not player or player.alive:
        raise SkipHandler

    # Telegram only permits this if the bot has delete-message permission.
    with suppress(Exception):
        await msg.delete()
    raise SkipHandler


# ----------------- lobby -----------------

@router.message(Command("newgame"))
async def cmd_newgame(msg: Message) -> None:
    lang = await _lang_for(msg.from_user.id)
    if msg.chat.type == "private":
        await _safe_send(msg.bot, msg.chat.id, t(lang, "game_need_group"))
        return
    existing = manager.get(msg.chat.id)
    if existing and not existing.is_over():
        await _safe_send(msg.bot, msg.chat.id, t(lang, "game_already"))
        return
    game = manager.create(msg.chat.id, msg.from_user.id)
    game.host_name = msg.from_user.full_name
    game.add_player(msg.from_user.id, msg.from_user.full_name)
    await _safe_send(msg.bot, msg.chat.id, _lobby_text(lang, game), reply_markup=_lobby_keyboard(lang))


@router.message(Command("cancelgame"))
async def cmd_cancel(msg: Message) -> None:
    lang = await _lang_for(msg.from_user.id)
    game = manager.get(msg.chat.id)
    if not game:
        await _safe_send(msg.bot, msg.chat.id, t(lang, "game_not_found"))
        return
    if msg.from_user.id != game.host_id:
        return
    manager.drop(msg.chat.id)
    await _safe_send(msg.bot, msg.chat.id, t(lang, "game_cancel"))


@router.message(Command("players"))
async def cmd_players(msg: Message) -> None:
    lang = await _lang_for(msg.from_user.id)
    game = manager.get(msg.chat.id) if msg.chat.type != "private" else manager.find_game_by_player(msg.from_user.id)
    if not game:
        await _safe_send(msg.bot, msg.chat.id, t(lang, "game_not_found"))
        return
    await _safe_send(msg.bot, msg.chat.id, t(lang, "game_players_list", list=_players_block(game)))


# ----------------- lobby callbacks -----------------

@router.callback_query(F.data == "lobby:join")
async def cb_join(cb: CallbackQuery, bot: Bot) -> None:
    lang = await _lang_for(cb.from_user.id)
    if not cb.message:
        await cb.answer()
        return
    game = manager.get(cb.message.chat.id)
    if not game or game.phase != Phase.LOBBY:
        await cb.answer(_pop_text(t(lang, "game_not_found")), show_alert=True)
        return
    if game.by_tg(cb.from_user.id):
        await cb.answer(_pop_text(t(lang, "game_already_joined")), show_alert=True)
        return
    # Verify DM channel (needed to send role + action buttons later)
    try:
        await _safe_send(bot, cb.from_user.id, t(lang, "game_dm_ready"))
    except Exception:
        await cb.answer(_pop_text(t(lang, "game_need_dm")), show_alert=True)
        return
    game.add_player(cb.from_user.id, cb.from_user.full_name)
    with suppress(Exception):
        await _safe_edit_text(cb.message, _lobby_text(lang, game), reply_markup=_lobby_keyboard(lang))
    await cb.answer(t(lang, "game_joined_short"))


@router.callback_query(F.data == "lobby:settings")
async def cb_lobby_settings(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    if not cb.message:
        await cb.answer(); return
    game = manager.get(cb.message.chat.id)
    if not game or game.phase != Phase.LOBBY:
        await cb.answer(_pop_text(t(lang, "game_not_found")), show_alert=True); return
    if cb.from_user.id != game.host_id:
        await cb.answer(_pop_text(t(lang, "not_host")), show_alert=True); return
    with suppress(Exception):
        await _safe_edit_text(cb.message, t(lang, "settings_header"),
                                   reply_markup=_settings_keyboard(lang, game))
    await cb.answer()


@router.callback_query(F.data.startswith("set:"))
async def cb_settings_action(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    if not cb.message:
        await cb.answer(); return
    game = manager.get(cb.message.chat.id)
    if not game or game.phase != Phase.LOBBY:
        await cb.answer(_pop_text(t(lang, "game_not_found")), show_alert=True); return
    if cb.from_user.id != game.host_id:
        await cb.answer(_pop_text(t(lang, "not_host")), show_alert=True); return
    action = cb.data.split(":", 1)[1]
    s = game.settings
    if action == "don":     s.allow_don = not s.allow_don
    elif action == "mode":
        if s.mode == "classic":
            s.mode = "fast"; s.night_seconds = 40; s.discussion_seconds = 60; s.vote_seconds = 30
        else:
            s.mode = "classic"; s.night_seconds = 40; s.discussion_seconds = 60; s.vote_seconds = 30
    elif action == "night":
        cycle = [30, 40, 50, 60, 90]
        s.night_seconds = cycle[(cycle.index(s.night_seconds) + 1) % len(cycle)] if s.night_seconds in cycle else 40
    elif action == "vote":
        cycle = [20, 30, 40, 60, 90]
        s.vote_seconds = cycle[(cycle.index(s.vote_seconds) + 1) % len(cycle)] if s.vote_seconds in cycle else 30
    elif action == "discussion":
        cycle = [30, 45, 60, 90, 120]
        s.discussion_seconds = cycle[(cycle.index(s.discussion_seconds) + 1) % len(cycle)] if s.discussion_seconds in cycle else 60
    elif action == "back":
        with suppress(Exception):
            await _safe_edit_text(cb.message, _lobby_text(lang, game), reply_markup=_lobby_keyboard(lang))
        await cb.answer(); return
    with suppress(Exception):
        await _safe_edit_text(cb.message, t(lang, "settings_header"),
                                   reply_markup=_settings_keyboard(lang, game))
    await cb.answer()


@router.callback_query(F.data == "lobby:cancel")
async def cb_lobby_cancel(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    if not cb.message:
        await cb.answer()
        return
    game = manager.get(cb.message.chat.id)
    if not game:
        await cb.answer()
        return
    if cb.from_user.id != game.host_id:
        await cb.answer(_pop_text(t(lang, "not_host")), show_alert=True)
        return
    manager.drop(cb.message.chat.id)
    with suppress(Exception):
        await _safe_edit_text(cb.message, t(lang, "game_cancel"))
    await cb.answer()


@router.callback_query(F.data.in_({"lobby:start", "start_game", "game:start"}))
async def cb_lobby_start(cb: CallbackQuery, bot: Bot) -> None:
    log.info("lobby start callback received: chat=%s user=%s data=%r",
             cb.message.chat.id if cb.message else None, cb.from_user.id, cb.data)
    lang = await _lang_for(cb.from_user.id)
    if not cb.message:
        await cb.answer()
        return
    chat_id = cb.message.chat.id
    game = manager.get(chat_id)
    if not game or game.phase != Phase.LOBBY:
        await cb.answer(_pop_text(t(lang, "game_not_found")), show_alert=True)
        return
    if cb.from_user.id != game.host_id:
        await cb.answer(_pop_text(t(lang, "not_host")), show_alert=True)
        return
    if len(game.players) < game.MIN_PLAYERS:
        await cb.answer(
            t(lang, "game_need_more", min=game.MIN_PLAYERS, count=len(game.players)),
            show_alert=True,
        )
        return

    # Inventory/cosmetic data must NEVER be able to block the game start.
    # Prepare perks on a best-effort basis, then start the state machine.
    # A broken/expired inventory row is logged and skipped.
    await _prepare_game_perks(game)
    try:
        game.start()
    except Exception as exc:
        log.exception("GAME START FAILED: chat=%s players=%s phase=%s",
                      chat_id, len(game.players), game.phase.value)
        await cb.answer(f"Не удалось запустить игру: {type(exc).__name__}", show_alert=True)
        return

    await _safe_edit_text(cb.message, t(lang, "game_started"))
    await cb.answer()

    # The game is already in NIGHT after game.start(). Create the phase task
    # immediately; DM delivery and the phase announcement must never be able
    # to prevent the state machine from starting.
    manager.reset_phase_event(chat_id)
    manager.set_task(chat_id, asyncio.create_task(_phase_loop(bot, chat_id)))
    log.info("Game started in chat %s: players=%s phase=%s", chat_id, len(game.players), game.phase.value)

    with suppress(Exception):
        await _dm_roles(bot, game)
    try:
        await _announce_night(bot, chat_id, game)
    except Exception:
        log.exception("Initial night announcement failed in chat %s; phase loop continues", chat_id)


async def _prepare_game_perks(game: Game) -> None:
    """Load inventory into the game without ever blocking game startup.

    SQLite may return DateTime(timezone=True) values as naive datetimes, while
    PostgreSQL normally returns aware values. Normalize both forms before
    comparing expiration dates. One malformed player's inventory is isolated
    so the remaining players can still start the match.
    """
    try:
        async with SessionLocal() as s:
            now = datetime.now(timezone.utc)
            for p in game.players:
                p.title = ""
                p.badges = ""
                try:
                    user = (await s.execute(select(User).where(User.telegram_id == p.tg_id))).scalar_one_or_none()
                    if not user:
                        continue
                    perks = (await s.execute(
                        select(UserPerk, Perk).join(Perk, Perk.perk_id == UserPerk.perk_id)
                        .where(UserPerk.user_id == user.user_id)
                    )).all()
                    role_codes = {
                        "role_choice_mafia": Role.MAFIA,
                        "role_choice_sheriff": Role.SHERIFF,
                        "role_choice_doctor": Role.DOCTOR,
                        "role_choice_detective": Role.SHERIFF,
                    }

                    def add_badge(label: str) -> None:
                        p.badges += (" " if p.badges else "") + f"[{label}]"

                    for up, perk in perks:
                        expires = up.expires_at
                        if expires is not None:
                            if expires.tzinfo is None:
                                expires = expires.replace(tzinfo=timezone.utc)
                            if expires <= now:
                                continue

                        if perk.code == "title_mafioso":
                            p.title = str((perk.meta or {}).get("title") or "😎 Мафиозо")
                        elif perk.code == "vip_lobby_30d":
                            add_badge("👑 VIP")
                        elif perk.code == "name_color":
                            add_badge("🎨")
                        elif perk.code == "skin_noir_detective":
                            add_badge("🕶️")
                        elif perk.code == "emote_pack_classic":
                            add_badge("✨")
                        elif perk.code == "weapon_golden":
                            add_badge("🔫")
                        elif perk.code == "night_vision":
                            add_badge("🌌")

                        if perk.code in role_codes and (up.uses_left is None or up.uses_left > 0):
                            game.role_preferences[p.tg_id] = role_codes[perk.code]
                            if up.uses_left is not None:
                                up.uses_left -= 1
                                if up.uses_left <= 0:
                                    await s.delete(up)
                            continue

                        if perk.code == "first_night_shield" and (up.uses_left is None or up.uses_left > 0):
                            p.protected = True
                            if up.uses_left is not None:
                                up.uses_left -= 1
                                if up.uses_left <= 0:
                                    await s.delete(up)
                        elif perk.code == "check_immunity" and (up.uses_left is None or up.uses_left > 0):
                            p.hidden_from_checks = True
                            if up.uses_left is not None:
                                up.uses_left -= 1
                                if up.uses_left <= 0:
                                    await s.delete(up)
                except Exception:
                    log.exception("Skipping broken inventory for player %s in chat %s", p.tg_id, game.chat_id)
                    continue
            try:
                await s.commit()
            except Exception:
                await s.rollback()
                log.exception("Could not save consumed game perks in chat %s; game will continue", game.chat_id)
    except Exception:
        log.exception("Inventory preparation failed in chat %s; starting without inventory effects", game.chat_id)


# ----------------- phase announcements -----------------

def _pop_text(text: str) -> str:
    """Callback-query popups do not parse HTML; strip markup so <b> never leaks."""
    import re
    return re.sub(r"<[^>]+>", "", str(text)).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _night_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Перейти в бот", url=BOT_URL)
    ]])


async def _send_phase_post(bot: Bot, chat_id: int, image_path: str, caption: str, reply_markup=None) -> None:
    """Send a phase card as a photo, falling back to text if the asset is unavailable."""
    try:
        photo = FSInputFile(image_path)
        await _safe_send_photo(bot, chat_id, photo, caption, reply_markup=reply_markup)
    except Exception:
        await _safe_send(bot, chat_id, caption, reply_markup=reply_markup)


async def _safe_send_photo(bot: Bot, chat_id: int, photo, caption: str, **kwargs):
    for attempt in range(4):
        try:
            return await bot.send_photo(chat_id, photo=photo, caption=caption, **kwargs)
        except TelegramRetryAfter as e:
            delay = max(1, int(e.retry_after))
            log.warning("Telegram photo flood control for chat %s; retrying in %ss (attempt %s/4)", chat_id, delay, attempt + 1)
            await asyncio.sleep(delay)
        except TelegramBadRequest as e:
            if "can't parse entities" not in str(e).lower():
                log.error("Telegram rejected photo caption in chat %s: %s", chat_id, e)
                return None
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["parse_mode"] = None
            try:
                return await bot.send_photo(
                    chat_id, photo=photo, caption=_plain_text(caption), **fallback_kwargs
                )
            except TelegramRetryAfter as retry:
                await asyncio.sleep(max(1, int(retry.retry_after)))
            except Exception:
                log.exception("Plain-text photo fallback failed in chat %s", chat_id)
                return None
    log.error("Telegram photo flood control persisted for chat %s; skipping photo", chat_id)
    return None


async def _dm_roles(bot: Bot, game: Game) -> None:
    mafia_allies = SafeHtml(", ".join(
        f"#{p.number} {_mention(p)}" for p in game.players if p.role in {Role.DON, Role.MAFIA}
    ))
    ROLE_KEY = {
        Role.DON: "game_role_don",
        Role.MAFIA: "game_role_mafia",
        Role.SHERIFF: "game_role_sheriff",
        Role.DOCTOR: "game_role_doctor",
        Role.LOVER: "game_role_lover",
        Role.MANIAC: "game_role_maniac",
        Role.CIVILIAN: "game_role_civilian",
    }
    for p in game.players:
        plang = await _lang_for(p.tg_id)
        key = ROLE_KEY[p.role]
        if p.role in {Role.DON, Role.MAFIA}:
            text = t(plang, key, allies=mafia_allies)
        else:
            text = t(plang, key)
        text += "\n\n" + t(plang, "game_players_list", list=_players_block(game))
        with suppress(Exception):
            await _safe_send(bot, p.tg_id, text)


async def _announce_night(bot: Bot, chat_id: int, game: Game) -> None:
    lang = GAME_LANG
    secs = game.settings.night_seconds
    caption = t(lang, "game_night", secs=secs) + "\n\n" + t(lang, "game_players_list", list=_players_block(game))
    await _send_phase_post(bot, chat_id, NIGHT_IMAGE, caption, reply_markup=_night_keyboard())
    for p in game.alive_players():
        plang = GAME_LANG
        if p.role == Role.MAFIA:
            await _send_night_prompt(bot, p, plang, game, "kill", "prompt_kill", exclude_self=True, exclude_mafia=True)
        elif p.role == Role.DON:
            await _send_night_prompt(bot, p, plang, game, "donkill", "prompt_don_kill", exclude_self=True, exclude_mafia=True)
        elif p.role == Role.SHERIFF:
            await _send_night_prompt(bot, p, plang, game, "sheriff_action", "prompt_sheriff_action", exclude_self=True)
        elif p.role == Role.DOCTOR:
            await _send_night_prompt(bot, p, plang, game, "heal", "prompt_heal", exclude_self=True)
        elif p.role == Role.LOVER:
            await _send_night_prompt(bot, p, plang, game, "block", "prompt_block", exclude_self=True)
        elif p.role == Role.MANIAC:
            await _send_night_prompt(bot, p, plang, game, "mkill", "prompt_mkill", exclude_self=True)


async def _send_night_prompt(bot: Bot, actor: Player, lang: str, game: Game, action: str, key: str, **kwargs) -> None:
    with suppress(Exception):
        if action == "sheriff_action":
            markup = _sheriff_keyboard(game, actor, lang)
        else:
            markup = _targets_keyboard(game, action, actor, lang, **kwargs)
        await _safe_send(bot, actor.tg_id, t(lang, key), reply_markup=markup)


def _sheriff_keyboard(game: Game, actor: Player, lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔎 Перевірити", callback_data=f"night:check:{p.number}")]
        for p in game.alive_players() if p.tg_id != actor.tg_id
    ]
    rows += [
        [InlineKeyboardButton(text="🔫 Зробити постріл", callback_data=f"night:shoot:{p.number}")]
        for p in game.alive_players() if p.tg_id != actor.tg_id
    ]
    rows.append([InlineKeyboardButton(text=t(lang, "btn_nothing"), callback_data="night:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _announce_day(bot: Bot, chat_id: int, game: Game) -> None:
    lang = GAME_LANG
    secs = game.settings.discussion_seconds
    caption = t(lang, "game_day", secs=secs) + "\n\n" + t(lang, "game_players_list", list=_players_block(game))
    await _send_phase_post(bot, chat_id, DAY_IMAGE, caption)
    await _safe_send(bot, chat_id, t(lang, "game_discussion", secs=secs))


async def _announce_vote(bot: Bot, chat_id: int, game: Game) -> None:
    lang = GAME_LANG
    caption = t(lang, "game_voting_open", secs=game.settings.vote_seconds) + "\n\n" + t(lang, "game_players_list", list=_players_block(game))
    await _send_phase_post(bot, chat_id, VOTE_IMAGE, caption, reply_markup=_vote_keyboard(game, lang))


# ----------------- night action callbacks (DM) -----------------

@router.callback_query(F.data.startswith("night:"))
async def cb_night(cb: CallbackQuery, bot: Bot) -> None:
    lang = await _lang_for(cb.from_user.id)
    game = manager.find_game_by_player(cb.from_user.id)
    if not game:
        await cb.answer(_pop_text(t(lang, "game_not_found")), show_alert=True)
        return
    if game.phase != Phase.NIGHT:
        await cb.answer(_pop_text(t(lang, "game_action_wrong_phase")), show_alert=True)
        return
    player = game.by_tg(cb.from_user.id)
    if not player or not player.alive:
        await cb.answer(_pop_text(t(lang, "game_action_dead")), show_alert=True)
        return

    parts = cb.data.split(":")
    action = parts[1]
    extra: str | None = None

    if action == "skip":
        if not game.skip_night_action(cb.from_user.id):
            await cb.answer(_pop_text(t(lang, "game_action_not_role")), show_alert=True)
            return
    else:
        try:
            target_num = int(parts[2])
        except (IndexError, ValueError):
            await cb.answer(_pop_text(t(lang, "game_action_invalid_target")), show_alert=True)
            return
        ok = False
        if action == "kill" and player.role == Role.MAFIA:
            ok = game.submit_mafia_kill(cb.from_user.id, target_num)
        elif action == "donkill" and player.role == Role.DON:
            ok = game.submit_don_kill(cb.from_user.id, target_num)
        elif action == "check" and player.role == Role.SHERIFF:
            res = game.submit_sheriff_check(cb.from_user.id, target_num)
            ok = res is not None
            if ok:
                tgt = game.by_number(target_num)
                if tgt:
                    key = "game_sheriff_result_mafia" if res else "game_sheriff_result_clean"
                    extra = t(lang, key, name=_mention(tgt))
        elif action == "shoot" and player.role == Role.SHERIFF:
            ok = game.submit_sheriff_kill(cb.from_user.id, target_num)
        elif action == "heal" and player.role == Role.DOCTOR:
            ok = game.submit_doctor_heal(cb.from_user.id, target_num)
        elif action == "block" and player.role == Role.LOVER:
            ok = game.submit_lover_block(cb.from_user.id, target_num)
        elif action == "mkill" and player.role == Role.MANIAC:
            ok = game.submit_maniac_kill(cb.from_user.id, target_num)
        else:
            await cb.answer(_pop_text(t(lang, "game_action_not_role")), show_alert=True)
            return
        if not ok:
            await cb.answer(_pop_text(t(lang, "game_action_invalid_target")), show_alert=True)
            return

    # The Don sees the current target selected by the Mafia team.
    if action == "kill" and player.role == Role.MAFIA and 'target_num' in locals():
        selected = game.by_number(target_num)
        if selected:
            for don in game.players_of(Role.DON):
                with suppress(Exception):
                    await _safe_send(
                        bot, don.tg_id,
                        t(lang, "game_mafia_target_for_don", target=_mention(selected)),
                    )

    if action == "skip":
        status_key = {
            Role.MAFIA: "game_action_nothing_mafia", Role.DON: "game_action_nothing_don",
            Role.SHERIFF: "game_action_nothing_sheriff", Role.DOCTOR: "game_action_nothing_doctor",
            Role.LOVER: "game_action_nothing_lover", Role.MANIAC: "game_action_nothing_maniac",
        }.get(player.role)
    else:
        status_key = {
            Role.MAFIA: "game_action_done_mafia", Role.DON: "game_action_done_don",
            Role.SHERIFF: "game_action_done_sheriff", Role.DOCTOR: "game_action_done_doctor",
            Role.LOVER: "game_action_done_lover", Role.MANIAC: "game_action_done_maniac",
        }.get(player.role)
    if status_key:
        with suppress(Exception):
            await _safe_send(bot, game.chat_id, t(lang, status_key))

    # Keep a permanent Telegram DM record of the player's exact choice.
    if action == "skip":
        choice_text = t(lang, "game_choice_nothing")
    else:
        chosen = game.by_number(target_num) if 'target_num' in locals() else None
        choice_text = t(lang, "game_choice_target", target=_mention(chosen)) if chosen else t(lang, "game_choice_recorded")
    with suppress(Exception):
        await _safe_send(bot, cb.from_user.id, choice_text)

    await cb.answer(_pop_text(t(lang, "game_action_recorded")))
    if extra:
        with suppress(Exception):
            await _safe_send(bot, cb.from_user.id, extra)
    with suppress(Exception):
        await cb.message.edit_reply_markup(reply_markup=None)

    if game.night_complete():
        manager.phase_event(game.chat_id).set()


# ----------------- private Mafia/Don night chat -----------------

@router.message(F.chat.type == "private")
async def mafia_night_chat(msg: Message) -> None:
    """Relay ordinary DM text between the living Mafia team during the night."""
    if not msg.from_user or not msg.text or msg.text.startswith("/"):
        raise SkipHandler
    game = manager.find_game_by_player(msg.from_user.id)
    if not game or game.phase != Phase.NIGHT:
        raise SkipHandler
    sender = game.by_tg(msg.from_user.id)
    if not sender or not sender.alive or sender.role not in MAFIA_TEAM:
        raise SkipHandler

    relay = f"💬 <b>{_mention(sender)}</b>: {escape(msg.text)}"
    recipients = [p for p in game.alive_players() if p.role in MAFIA_TEAM and p.tg_id != sender.tg_id]
    for recipient in recipients:
        with suppress(Exception):
            await _safe_send(bot=msg.bot, chat_id=recipient.tg_id, text=relay)
    with suppress(Exception):
        await msg.answer("💬 Повідомлення надіслано команді мафії.")


# ----------------- day vote callback (group) -----------------

@router.callback_query(F.data == "day:no_vote")
async def cb_no_vote(cb: CallbackQuery, bot: Bot) -> None:
    lang = await _lang_for(cb.from_user.id)
    if not cb.message:
        await cb.answer()
        return
    game = manager.get(cb.message.chat.id)
    if not game or game.phase != Phase.DAY:
        await cb.answer(_pop_text(t(lang, "game_action_wrong_phase")), show_alert=True)
        return
    if not game.submit_no_vote(cb.from_user.id):
        await cb.answer(_pop_text(t(lang, "game_action_invalid_target")), show_alert=True)
        return
    with suppress(Exception):
        await _safe_send(bot, cb.from_user.id, t(lang, "game_choice_no_vote"))
    await cb.answer(_pop_text(t(lang, "game_action_recorded")))
    tally = _tally_text(lang, game)
    body = (t(lang, "prompt_vote") + "\n\n" +
            t(lang, "game_players_list", list=_players_block(game)) +
            (("\n\n" + tally) if tally else ""))
    with suppress(Exception):
        await _safe_edit_text(cb.message, body, reply_markup=_vote_keyboard(game, lang))
    if game.day_complete():
        manager.phase_event(game.chat_id).set()


@router.callback_query(F.data.startswith("day:vote:"))
async def cb_vote(cb: CallbackQuery, bot: Bot) -> None:
    lang = await _lang_for(cb.from_user.id)
    if not cb.message:
        await cb.answer()
        return
    game = manager.get(cb.message.chat.id)
    if not game or game.phase != Phase.DAY:
        await cb.answer(_pop_text(t(lang, "game_action_wrong_phase")), show_alert=True)
        return
    try:
        target_num = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer(_pop_text(t(lang, "game_action_invalid_target")), show_alert=True)
        return
    if not game.submit_vote(cb.from_user.id, target_num):
        await cb.answer(_pop_text(t(lang, "game_action_invalid_target")), show_alert=True)
        return
    chosen = game.by_number(target_num)
    with suppress(Exception):
        if chosen:
            await _safe_send(bot, cb.from_user.id, t(lang, "game_choice_target", target=_mention(chosen)))
    await cb.answer(_pop_text(t(lang, "game_action_recorded")))
    tally = _tally_text(lang, game)
    body = (
        t(lang, "prompt_vote") + "\n\n"
        + t(lang, "game_players_list", list=_players_block(game))
        + ("\n\n" + tally if tally else "")
    )
    with suppress(Exception):
        await _safe_edit_text(cb.message, body, reply_markup=_vote_keyboard(game, lang))
    if game.day_complete():
        manager.phase_event(game.chat_id).set()


@router.callback_query()
async def cb_unhandled_callback(cb: CallbackQuery) -> None:
    # Keep diagnostics useful: an old/stale inline keyboard must not look like
    # a silent failure. Do not interfere with known handlers (this is reached
    # only when no earlier callback handler matched).
    log.warning("Unhandled callback: data=%r chat=%s user=%s",
                cb.data, cb.message.chat.id if cb.message else None, cb.from_user.id)
    with suppress(Exception):
        await cb.answer("⚠️ Кнопка більше не актуальна. Створіть нову гру.", show_alert=True)


# ----------------- phase loop -----------------

async def _phase_loop(bot: Bot, chat_id: int) -> None:
    try:
        while True:
            game = manager.get(chat_id)
            if not game or game.is_over():
                return

            # NIGHT
            ev = manager.phase_event(chat_id)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout=game.settings.night_seconds)
            game = manager.get(chat_id)
            if not game or game.phase != Phase.NIGHT:
                return
            res = game.resolve_night()
            lang = await _chat_lang(chat_id)
            # Notify blocked target privately
            if res.blocked:
                blang = await _lang_for(res.blocked.tg_id)
                with suppress(Exception):
                    await _safe_send(bot, res.blocked.tg_id, t(blang, "game_blocked_notice"))
            for victim in res.killed:
                with suppress(Exception):
                    vlang = await _lang_for(victim.tg_id)
                    await _safe_send(bot, victim.tg_id, t(vlang, "game_you_had_mafia"))
            for saved in res.saved:
                with suppress(Exception):
                    slang = await _lang_for(saved.tg_id)
                    await _safe_send(bot, saved.tg_id, t(slang, "game_you_had_doctor"))
            if res.sheriff_learned:
                checked, _ = res.sheriff_learned
                with suppress(Exception):
                    clang = await _lang_for(checked.tg_id)
                    await _safe_send(bot, checked.tg_id, t(clang, "game_checked_target"))
            if res.killed:
                names = SafeHtml(", ".join(
                    f"{_mention(p)} ({escape(_role_name(lang, p.role), quote=True)})" for p in res.killed
                ))
                await _safe_send(bot, chat_id, t(lang, "game_killed_multi", names=names))
            elif res.saved:
                await _safe_send(bot, chat_id, t(lang, "game_saved"))
            else:
                await _safe_send(bot, chat_id, t(lang, "game_nokill"))
            if await _maybe_end(bot, chat_id, game, lang):
                return

            # DAY: first discussion, then voting.
            manager.reset_phase_event(chat_id)
            await _announce_day(bot, chat_id, game)
            ev = manager.phase_event(chat_id)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout=game.settings.discussion_seconds)
            game = manager.get(chat_id)
            if not game or game.phase != Phase.DISCUSSION:
                return
            game.phase = Phase.DAY
            manager.reset_phase_event(chat_id)
            await _announce_vote(bot, chat_id, game)
            ev = manager.phase_event(chat_id)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout=game.settings.vote_seconds)
            game = manager.get(chat_id)
            if not game or game.phase != Phase.DAY:
                return
            dres = game.resolve_day()
            if dres.lynched:
                await _safe_send(bot, 
                    chat_id,
                    t(lang, "game_lynch", name=_mention(dres.lynched),
                      role=_role_name(lang, dres.lynched.role)),
                )
            else:
                await _safe_send(bot, chat_id, t(lang, "game_novote"))
            if await _maybe_end(bot, chat_id, game, lang):
                return

            manager.reset_phase_event(chat_id)
            await _announce_night(bot, chat_id, game)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("phase loop crashed for chat %s", chat_id)


async def _maybe_end(bot: Bot, chat_id: int, game: Game, lang: str) -> bool:
    if not game.is_over():
        return False
    key = {
        Winner.TOWN: "game_win_town",
        Winner.MAFIA: "game_win_mafia",
        Winner.MANIAC: "game_win_maniac",
    }[game.winner]
    await _safe_send(bot, chat_id, t(lang, key))
    # Reveal final roles
    roster = "\n".join(
        f"#{p.number} {_mention(p)} — {_role_name(lang, p.role)}" + ("" if p.alive else " ☠️")
        for p in sorted(game.players, key=lambda x: x.number)
    )
    await _safe_send(bot, chat_id, t(lang, "game_final_roles") + "\n" + roster)
    # Survivor coin reward + stats/ELO/achievements in one DB transaction.
    async with SessionLocal() as s:
        survivors = [p for p in game.players if p.alive]
        for p in survivors:
            user = (await s.execute(select(User).where(User.telegram_id == p.tg_id))).scalar_one_or_none()
            if not user:
                continue
            base_reward = SURVIVOR_REWARD
            active_perks = (await s.execute(
                select(Perk.code, UserPerk).join(UserPerk, UserPerk.perk_id == Perk.perk_id)
                .where(UserPerk.user_id == user.user_id)
            )).all()
            vip = any(code == "vip_lobby_30d" for code, _ in active_perks)
            xp = next((up for code, up in active_perks if code == "xp_boost_x2" and (up.uses_left is None or up.uses_left > 0)), None)
            reward = int(base_reward * 1.25) if vip else base_reward
            if xp:
                reward *= 2
            try:
                await economy.earn(s, user, reward, "game_survivor", settings.daily_earn_cap)
            except economy.DailyCapReached:
                pass
            if xp and xp.uses_left is not None:
                xp.uses_left -= 1
                if xp.uses_left <= 0:
                    await s.delete(xp)
        report = await stats_service.record_game_end(s, game, game.winner)
        await s.commit()

    if survivors:
        await _safe_send(bot, chat_id, t(lang, "game_rewards", coins=SURVIVOR_REWARD))

    # Per-player DM: ELO delta + any newly-unlocked achievements.
    for p in game.players:
        delta = report.deltas.get(p.tg_id, 0)
        unlocked = report.unlocked.get(p.tg_id, [])
        if delta == 0 and not unlocked:
            continue
        plang = await _lang_for(p.tg_id)
        lines = [t(plang, "game_elo_delta", delta=f"{delta:+d}")]
        for code in unlocked:
            meta = stats_service.ACHIEVEMENTS[code]
            lines.append(t(plang, "achievement_unlocked",
                           name=meta[f"name_{plang}" if f"name_{plang}" in meta else "name_en"],
                           reward=meta["reward"]))
        with suppress(Exception):
            await _safe_send(bot, p.tg_id, "\n".join(lines))

    manager.drop(chat_id)
    return True
