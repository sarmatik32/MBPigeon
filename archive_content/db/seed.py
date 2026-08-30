from __future__ import annotations

from sqlalchemy import select

from db.models import Language, Perk, PerkCategory, PerkTranslation
from db.session import SessionLocal

PERKS = [
    ("skin_noir_detective", PerkCategory.cosmetic,   500,  {"asset": "skins/noir_detective"}),
    ("name_color",          PerkCategory.cosmetic,   250,  {"effect": "profile_name_badge"}),
    ("title_mafioso",       PerkCategory.cosmetic,   400,  {"title": "😎 Мафиозо"}),
    ("emote_pack_classic",  PerkCategory.cosmetic,   350,  {"count": 5}),
    ("weapon_golden",       PerkCategory.cosmetic,   800,  {"asset": "weapons/golden_revolver"}),
    ("night_vision",        PerkCategory.cosmetic,   450,  {"effect": "night_ui"}),
    ("xp_boost_x2",         PerkCategory.consumable, 300,  {"matches": 3, "multiplier": 2}),
    ("role_choice_mafia",   PerkCategory.consumable, 700,  {"uses": 1, "role": "mafia"}),
    ("role_choice_sheriff", PerkCategory.consumable, 700,  {"uses": 1, "role": "sheriff"}),
    ("role_choice_doctor",  PerkCategory.consumable, 700,  {"uses": 1, "role": "doctor"}),
    ("role_choice_detective", PerkCategory.consumable, 700, {"uses": 1, "role": "sheriff"}),
    ("first_night_shield",  PerkCategory.consumable, 650,  {"uses": 1, "effect": "shield_first_night"}),
    ("check_immunity",      PerkCategory.consumable, 900,  {"uses": 1, "effect": "hide_from_check"}),
    ("revive_charm",        PerkCategory.consumable, 1000, {"uses": 1, "cooldown_days": 7}),
    ("event_pass",          PerkCategory.access,     750,  {"duration_days": 3}),
    ("vip_lobby_30d",       PerkCategory.subscription,1200,{"duration_days": 30, "reward_multiplier": 1.25}),
]


TRANSLATIONS = {
    "skin_noir_detective": {Language.ru: ("Скин «Нуар-детектив»", "Стильный плащ в духе нуара."), Language.uk: ("Скін «Нуар-детектив»", "Стильний плащ у стилі нуар.")},
    "name_color": {Language.ru: ("Цвет ника", "Особое оформление имени в профиле и игре."), Language.uk: ("Колір ніку", "Особливе оформлення імені у профілі та грі.")},
    "title_mafioso": {Language.ru: ("Титул «Мафиозо»", "Эксклюзивный титул для профиля."), Language.uk: ("Титул «Мафіозо»", "Ексклюзивний титул для профілю.")},
    "emote_pack_classic": {Language.ru: ("Набор эмоций", "5 игровых эмоций."), Language.uk: ("Набір емоцій", "5 ігрових емоцій.")},
    "weapon_golden": {Language.ru: ("Золотой револьвер", "Косметическое оформление оружия."), Language.uk: ("Золотий револьвер", "Косметичне оформлення зброї.")},
    "night_vision": {Language.ru: ("Ночное видение", "Специальное оформление ночной фазы."), Language.uk: ("Нічне бачення", "Спеціальне оформлення нічної фази.")},
    "xp_boost_x2": {Language.ru: ("Опыт x2", "Удваивает XP на следующие 3 матча."), Language.uk: ("Досвід x2", "Подвоює XP на наступні 3 матчі.")},
    "role_choice_mafia": {Language.ru: ("Карта роли: Мафия", "Повышает шанс получить роль Мафия в следующей игре."), Language.uk: ("Карта ролі: Мафія", "Підвищує шанс отримати роль Мафія у наступній грі.")},
    "role_choice_sheriff": {Language.ru: ("Карта роли: Шериф", "Повышает шанс получить роль Шериф в следующей игре."), Language.uk: ("Карта ролі: Шериф", "Підвищує шанс отримати роль Шериф у наступній грі.")},
    "role_choice_doctor": {Language.ru: ("Карта роли: Доктор", "Повышает шанс получить роль Доктор у наступній грі."), Language.uk: ("Карта ролі: Лікар", "Підвищує шанс отримати роль Лікар у наступній грі.")},
    "role_choice_detective": {Language.ru: ("Карта роли: Детектив", "Карта выбора роли детектива (реализуется как Шериф)."), Language.uk: ("Карта ролі: Детектив", "Карта вибору ролі детектива (у грі реалізується як Шериф).")},
    "first_night_shield": {Language.ru: ("Щит первой ночи", "Защищает от первого ночного убийства."), Language.uk: ("Щит першої ночі", "Захищає від першого нічного вбивства.")},
    "check_immunity": {Language.ru: ("Иммунитет проверки", "Скрывает настоящую роль при одной проверке Шерифа."), Language.uk: ("Імунітет перевірки", "Приховує справжню роль під час однієї перевірки Шерифа.")},
    "revive_charm": {Language.ru: ("Амулет возрождения", "Позволяет вернуться наблюдателем после гибели."), Language.uk: ("Амулет відродження", "Дозволяє повернутися спостерігачем після загибелі.")},
    "event_pass": {Language.ru: ("Пропуск на ивент", "Доступ к тематическим событиям."), Language.uk: ("Перепустка на подію", "Доступ до тематичних подій.")},
    "vip_lobby_30d": {Language.ru: ("VIP на 30 дней", "VIP-статус и бонус к наградам."), Language.uk: ("VIP на 30 днів", "VIP-статус і бонус до нагород.")},
}

# Fill untranslated languages with the Russian catalog so every installed locale remains usable.
for _code in [p[0] for p in PERKS]:
    _ru = TRANSLATIONS[_code][Language.ru]
    for _lang in Language:
        TRANSLATIONS[_code].setdefault(_lang, _ru)



async def seed_catalog() -> None:
    async with SessionLocal() as s:
        existing = (await s.execute(select(Perk.code))).scalars().all()
        existing_set = set(existing)
        for code, category, cost, meta in PERKS:
            if code in existing_set:
                continue
            perk = Perk(code=code, category=category, cost_coins=cost, meta=meta)
            s.add(perk)
            await s.flush()
            for lang, (name, desc) in TRANSLATIONS[code].items():
                s.add(PerkTranslation(perk_id=perk.perk_id, language=lang, name=name, description=desc))
        # Backfill missing translations for perks that were seeded by an older version.
        perks = (await s.execute(select(Perk))).scalars().all()
        for perk in perks:
            translations = TRANSLATIONS.get(perk.code)
            if not translations:
                continue
            existing_rows = (await s.execute(select(PerkTranslation).where(PerkTranslation.perk_id == perk.perk_id))).scalars().all()
            existing_langs = {row.language for row in existing_rows}
            for lang, (name, desc) in translations.items():
                if lang not in existing_langs:
                    s.add(PerkTranslation(perk_id=perk.perk_id, language=lang, name=name, description=desc))
        await s.commit()
