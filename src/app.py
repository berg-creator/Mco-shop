"""Точка входа: поднимает бота и витрину в одном процессе.

Разделять их незачем — у магазина одна база и один жизненный цикл; два
процесса означали бы два юнита systemd, два лога и вопрос «а этот запущен?».
Веб слушает порт, бот работает long polling, оба живут в одном asyncio-цикле.

    python -m src.app            запустить магазин
    python -m src.app --check    проверить настройки и базу, ничего не поднимая
    python -m src.app --web-only только витрина, без бота (отладка в браузере)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiohttp import web as aiohttp_web

from . import admin, config, db, server
from .bot import notify_admins, router as shop_router, setup_menu_button

log = logging.getLogger("магазин")


async def run(settings: config.Settings, web_only: bool = False) -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage

    db.init()

    bot: Bot | None = None
    if not web_only:
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

    async def notify(order_id: int) -> None:
        if bot is not None:
            await notify_admins(bot, settings, order_id)
        else:
            log.info("заявка №%s создана (бот выключен, письмо не ушло)", order_id)

    application = server.create_app(
        settings,
        notify_order=notify,
        allow_unsigned=settings.dev_allow_unsigned or web_only,
    )
    runner = aiohttp_web.AppRunner(application)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, host="0.0.0.0", port=settings.port)
    await site.start()
    log.info("витрина слушает http://127.0.0.1:%s/app/", settings.port)

    if bot is None:
        log.info("бот не запущен (--web-only). Остановить: Ctrl+C")
        await asyncio.Event().wait()
        return

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings
    # Админка идёт первой: пока владелец заводит товар, его текст должен
    # попадать в мастер, а не в общие обработчики магазина.
    dispatcher.include_router(admin.router)
    dispatcher.include_router(shop_router)

    try:
        await setup_menu_button(bot, settings)
        me = await bot.get_me()
        log.info("бот @%s на связи, витрина: %s", me.username, settings.webapp_url)
        await dispatcher.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Магазин в Telegram")
    parser.add_argument("--check", action="store_true", help="проверить настройки и базу")
    parser.add_argument("--web-only", action="store_true", help="поднять только витрину")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = config.load()

    if args.check:
        return config._check()

    # Витрине хватает базы: без токена её можно открыть в браузере и посмотреть,
    # как выглядит каталог. Боту токен нужен обязательно.
    if not args.web_only and not settings.ready:
        print("Запуск невозможен, не хватает настроек:")
        for problem in settings.problems:
            print(f"  — {problem}")
        print("\nПодробности: python -m src.app --check")
        return 1

    try:
        asyncio.run(run(settings, web_only=args.web_only))
    except KeyboardInterrupt:
        log.info("остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
