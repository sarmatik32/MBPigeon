"""Main DM menu: Profile / Stats / Shop / Rating / Achievements / Settings / Language / Rules / Help.

All navigation via inline keyboards. Opens on /menu or /start in private chat.
"""
from __future__ import annotations

from contextlib import suppress
from html import escape
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from bot.keyboards import language_keyboard
from db.models import Language, Perk, PerkTranslation, User, UserPerk
from db.session import SessionLocal
from services import stats as stats_service
from services.i18n import t

router = Router()


# ------------- helpers -------------

async def _user(tg_id: int) -> User | None:
    async with SessionLocal() as s:
        return (await s.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()


async def _lang_for(tg_id: int) -> str:
    u = await _user(tg_id)
    return u.language.value if u else "ru"


def _menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    def b(label_key: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=t(lang, label_key), callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("menu_profile", "menu:profile"), b("menu_stats", "menu:stats")],
        [b("menu_shop", "menu:shop"), b("menu_rating", "menu:rating")],
        [b("menu_achievements", "menu:ach"), b("menu_inventory", "menu:inv")],
        [b("menu_language", "menu:lang"), b("menu_rules", "menu:rules")],
        [b("menu_help", "menu:help")],
    ])


def _back_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="menu:home")],
    ])


# ------------- /menu command -------------

@router.message(Command("menu"))
async def cmd_menu(msg: Message) -> None:
    if msg.chat.type != "private":
        return
    lang = await _lang_for(msg.from_user.id)
    await msg.answer(t(lang, "menu_header", name=msg.from_user.full_name),
                     reply_markup=_menu_keyboard(lang))


@router.callback_query(F.data == "menu:home")
async def cb_home(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    with suppress(Exception):
        await cb.message.edit_text(
            t(lang, "menu_header", name=cb.from_user.full_name),
            reply_markup=_menu_keyboard(lang),
        )
    await cb.answer()


# ------------- profile -------------

@router.message(Command("profile"))
async def cmd_profile(msg: Message) -> None:
    if msg.chat.type != "private":
        return
    lang = await _lang_for(msg.from_user.id)
    u = await _user(msg.from_user.id)
    if not u:
        await msg.answer(t(lang, "use_start"))
        return
    async with SessionLocal() as s:
        st = await stats_service.get_stats(s, u.user_id)
        owned = (await s.execute(
            select(Perk, UserPerk).join(UserPerk, UserPerk.perk_id == Perk.perk_id)
            .where(UserPerk.user_id == u.user_id).order_by(UserPerk.acquired_at.desc())
        )).all()
    now = datetime.utcnow()
    lines = [t(lang, "profile_body", name=msg.from_user.full_name, elo=u.elo, coins=u.coin_balance,
                 streak=u.daily_streak, games=st.games_played, wins=st.wins, losses=st.losses)]
    active_cos = []
    for perk, up in owned:
        if up.expires_at is not None and up.expires_at.replace(tzinfo=None) <= now:
            continue
        if perk.code == "vip_lobby_30d": active_cos.append("👑 VIP")
        elif perk.code == "title_mafioso": active_cos.append(str(perk.meta.get("title") or "😎 Мафіозо"))
        elif perk.code == "name_color": active_cos.append("🎨 Колір ніку")
        elif perk.code == "skin_noir_detective": active_cos.append("🕶️ Нуар")
        elif perk.code == "emote_pack_classic": active_cos.append("✨ Емоції")
        elif perk.code == "weapon_golden": active_cos.append("🔫 Золотий револьвер")
        elif perk.code == "night_vision": active_cos.append("🌌 Нічне бачення")
    if active_cos:
        lines.append("\n🎨 <b>Твои активные предметы:</b>\n" + "\n".join("• " + escape(x, quote=True) for x in active_cos))
    lines.append("\n🎒 <b>Инвентарь:</b> " + str(len(owned)) + " предмет(ов)")
    await msg.answer("\n".join(lines), reply_markup=_back_keyboard(lang))

@router.callback_query(F.data == "menu:profile")
async def cb_profile(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    u = await _user(cb.from_user.id)
    if not u:
        await cb.answer(t(lang, "use_start"), show_alert=True)
        return
    async with SessionLocal() as s:
        st = await stats_service.get_stats(s, u.user_id)
        owned = (await s.execute(
            select(Perk, UserPerk).join(UserPerk, UserPerk.perk_id == Perk.perk_id)
            .where(UserPerk.user_id == u.user_id).order_by(UserPerk.acquired_at.desc())
        )).all()
    now = datetime.utcnow()
    lines = [t(lang, "profile_body", name=cb.from_user.full_name, elo=u.elo, coins=u.coin_balance,
                 streak=u.daily_streak, games=st.games_played, wins=st.wins, losses=st.losses)]
    active = []
    for perk, up in owned:
        if up.expires_at is not None and up.expires_at.replace(tzinfo=None) <= now:
            continue
        labels = {
            "vip_lobby_30d": "👑 VIP", "title_mafioso": str(perk.meta.get("title") or "😎 Мафіозо"),
            "name_color": "🎨 Колір ніку", "skin_noir_detective": "🕶️ Нуар",
            "emote_pack_classic": "✨ Емоції", "weapon_golden": "🔫 Золотий револьвер",
            "night_vision": "🌌 Нічне бачення",
        }
        if perk.code in labels: active.append(labels[perk.code])
    if active:
        lines.append("\n🎨 <b>Активное оформление:</b>\n" + "\n".join("• " + escape(x, quote=True) for x in active))
    lines.append("\n🎒 <b>Купленные предметы:</b> <b>" + str(len(owned)) + "</b>")
    with suppress(Exception):
        await cb.message.edit_text("\n".join(lines), reply_markup=_back_keyboard(lang))
    await cb.answer()

# ------------- stats -------------

@router.callback_query(F.data == "menu:stats")
async def cb_stats(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    u = await _user(cb.from_user.id)
    if not u:
        await cb.answer(t(lang, "use_start"), show_alert=True)
        return
    async with SessionLocal() as s:
        st = await stats_service.get_stats(s, u.user_id)
    wr = (st.wins / st.games_played * 100) if st.games_played else 0
    text = t(lang, "stats_body",
             games=st.games_played, wins=st.wins, losses=st.losses,
             wr=f"{wr:.1f}", deaths=st.deaths,
             town_w=st.town_wins, mafia_w=st.mafia_wins, maniac_w=st.maniac_wins,
             as_don=st.as_don, as_mafia=st.as_mafia, as_sheriff=st.as_sheriff,
             as_doctor=st.as_doctor, as_lover=st.as_lover,
             as_maniac=st.as_maniac, as_civilian=st.as_civilian)
    with suppress(Exception):
        await cb.message.edit_text(text, reply_markup=_back_keyboard(lang))
    await cb.answer()


# ------------- rating -------------

@router.callback_query(F.data == "menu:rating")
async def cb_rating(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    async with SessionLocal() as s:
        top = await stats_service.top_rating(s, limit=10)
    lines = [t(lang, "rating_header")]
    for i, u in enumerate(top, start=1):
        lines.append(f"{i}. {escape(str(u.username), quote=True)} — {u.elo} ELO")
    if not top:
        lines.append(t(lang, "rating_empty"))
    with suppress(Exception):
        await cb.message.edit_text("\n".join(lines), reply_markup=_back_keyboard(lang))
    await cb.answer()


# ------------- achievements -------------

@router.callback_query(F.data == "menu:ach")
async def cb_ach(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    u = await _user(cb.from_user.id)
    if not u:
        await cb.answer(t(lang, "use_start"), show_alert=True)
        return
    async with SessionLocal() as s:
        owned = set(await stats_service.user_achievements(s, u.user_id))
    lines = [t(lang, "ach_header")]
    for code, meta in stats_service.ACHIEVEMENTS.items():
        mark = "✅" if code in owned else "🔒"
        name = meta.get(f"name_{lang}", meta["name_en"])
        desc = meta.get(f"desc_{lang}", meta["desc_en"])
        lines.append(f"{mark} <b>{escape(str(name), quote=True)}</b> — {escape(str(desc), quote=True)} (+{meta['reward']})")
    with suppress(Exception):
        await cb.message.edit_text("\n".join(lines), reply_markup=_back_keyboard(lang))
    await cb.answer()


# ------------- shop -------------

@router.callback_query(F.data == "menu:shop")
async def cb_shop(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Perk, PerkTranslation)
            .join(PerkTranslation, (PerkTranslation.perk_id == Perk.perk_id) & (PerkTranslation.language == Language(lang)))
            .where(Perk.is_active.is_(True))
            .order_by(Perk.cost_coins)
        )).all()
    lines = [t(lang, "shop_header")]
    for perk, tr in rows:
        lines.append(t(lang, "shop_item", code=perk.code, name=tr.name,
                       cost=perk.cost_coins, desc=tr.description))
    with suppress(Exception):
        await cb.message.edit_text("\n\n".join(lines), reply_markup=_back_keyboard(lang))
    await cb.answer()


# ------------- inventory -------------

@router.callback_query(F.data == "menu:inv")
async def cb_inv(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    u = await _user(cb.from_user.id)
    if not u:
        await cb.answer(t(lang, "use_start"), show_alert=True)
        return
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(UserPerk, PerkTranslation)
            .join(Perk, Perk.perk_id == UserPerk.perk_id)
            .join(PerkTranslation, (PerkTranslation.perk_id == Perk.perk_id) & (PerkTranslation.language == Language(lang)))
            .where(UserPerk.user_id == u.user_id)
        )).all()
    if not rows:
        text = t(lang, "inventory_empty")
        markup = _back_keyboard(lang)
    else:
        lines = [t(lang, "inventory_header")]
        buttons = []
        for up, tr in rows:
            state = []
            if up.uses_left is not None:
                state.append(f"осталось: {up.uses_left}")
            if up.expires_at:
                state.append(f"до: {up.expires_at.date().isoformat()}")
            lines.append(f"🎁 <b>{tr.name}</b> — {tr.description}" + (f" ({', '.join(state)})" if state else ""))
            buttons.append([InlineKeyboardButton(text=f"ℹ️ {tr.name}", callback_data=f"inv:info:{up.user_perk_id}")])
        buttons.append([InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="menu:home")])
        text = "\n\n".join(lines)
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    with suppress(Exception):
        await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("inv:info:"))
async def cb_inventory_info(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    try:
        up_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer("Ошибка", show_alert=True)
        return
    async with SessionLocal() as s:
        user = (await s.execute(select(User).where(User.telegram_id == cb.from_user.id))).scalar_one_or_none()
        if not user:
            await cb.answer("Сначала /start", show_alert=True)
            return
        row = (await s.execute(
            select(UserPerk, PerkTranslation).join(Perk, Perk.perk_id == UserPerk.perk_id)
            .join(PerkTranslation, (PerkTranslation.perk_id == Perk.perk_id) & (PerkTranslation.language == Language(lang)))
            .where(UserPerk.user_perk_id == up_id, UserPerk.user_id == user.user_id)
        )).first()
    if not row:
        await cb.answer("Предмет не найден", show_alert=True)
        return
    up, tr = row
    await cb.answer(f"{tr.name}: предмет применяется автоматически при следующей подходящей игре.", show_alert=True)


# ------------- language -------------

@router.callback_query(F.data == "menu:lang")
async def cb_lang(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    with suppress(Exception):
        await cb.message.edit_text(t(lang, "pick_language"),
                                   reply_markup=language_keyboard())
    await cb.answer()


# ------------- rules -------------

@router.callback_query(F.data == "menu:rules")
async def cb_rules(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    with suppress(Exception):
        await cb.message.edit_text(t(lang, "rules"), reply_markup=_back_keyboard(lang))
    await cb.answer()


# ------------- help -------------

@router.callback_query(F.data == "menu:help")
async def cb_help(cb: CallbackQuery) -> None:
    lang = await _lang_for(cb.from_user.id)
    with suppress(Exception):
        await cb.message.edit_text(t(lang, "help_body"),
                                   reply_markup=_back_keyboard(lang))
    await cb.answer()
