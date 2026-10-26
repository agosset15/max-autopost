import asyncio
import logging

from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from maxapi.enums.update import UpdateType
from maxapi.webhook.aiohttp import AiohttpMaxWebhook

from src.handlers.telegram import tg_router
from src.handlers.max import max_router
from src.config import (
    MAX_WEBHOOK_PATH,
    MAX_WEBHOOK_URL,
    TELEGRAM_WEBHOOK_PATH,
    TELEGRAM_WEBHOOK_URL,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
    WEBHOOK_SECRET,
)
from src.loader import max_bot, max_dp, message_map, tg_bot, tg_dp

logging.basicConfig(level=logging.INFO, force=True)


def max_update_types() -> list[UpdateType]:
    """The MAX events the registered handlers actually consume.

    Subscribing explicitly keeps the subscription in step with the handlers:
    message_edited and message_removed only arrive if the subscription asks for
    them, and a handler added later is picked up here on its own.
    """
    used = {
        handler.update_type
        for router in (max_dp, *max_dp.routers)
        for handler in router.event_handlers
        if handler.update_type is not UpdateType.ON_STARTED
    }
    return sorted(used)


async def main():
    logging.info("Starting bots...")

    await message_map.connect()

    tg_dp.include_router(tg_router)
    max_dp.include_routers(max_router)

    max_bot.me = await max_bot.get_me()

    await tg_bot.set_webhook(
        url=TELEGRAM_WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=tg_dp.resolve_used_update_types(),
        secret_token=WEBHOOK_SECRET,
    )
    logging.info("Telegram webhook set to %s", TELEGRAM_WEBHOOK_URL)

    update_types = max_update_types()
    await max_bot.subscribe_webhook(
        url=MAX_WEBHOOK_URL, update_types=update_types, secret=WEBHOOK_SECRET,
    )
    logging.info(
        "MAX webhook set to %s (updates: %s)",
        MAX_WEBHOOK_URL, ", ".join(t.value for t in update_types),
    )

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=tg_dp, bot=tg_bot, secret_token=WEBHOOK_SECRET
    ).register(app, path=TELEGRAM_WEBHOOK_PATH)
    AiohttpMaxWebhook(dp=max_dp, bot=max_bot, secret=WEBHOOK_SECRET).setup(
        app, path=MAX_WEBHOOK_PATH
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    logging.info(
        "Webhook server listening on %s:%d (paths: %s; %s)",
        WEBHOOK_HOST,
        WEBHOOK_PORT,
        TELEGRAM_WEBHOOK_PATH,
        MAX_WEBHOOK_PATH
    )

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await tg_bot.delete_webhook()
        await max_bot.delete_webhook()
        await message_map.close()
        logging.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())