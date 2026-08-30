from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

LANGS = [
    ("Українська", "uk"), ("English", "en"), ("Español", "es"),
    ("Français", "fr"), ("Deutsch", "de"), ("中文", "zh"), ("Русский", "ru"),
]


def language_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"setlang:{code}")] for label, code in LANGS]
    return InlineKeyboardMarkup(inline_keyboard=rows)
