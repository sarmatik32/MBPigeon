from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.handlers import router as community_router
from bot.game_handlers import router as game_router
from bot.menu import router as menu_router
from config import settings
from db.seed import seed_catalog
from db.session import init_db


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    await init_db()
    await seed_catalog()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(menu_router)
    # Game router goes before community handlers so eliminated players are
    # blocked before generic group commands can process their messages.
    dp.include_router(game_router)
    dp.include_router(community_router)

    logging.info("Mafia Community bot starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
