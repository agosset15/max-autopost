import logging

from aiogram import Bot as TgBot, Dispatcher as TgDispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from maxapi import Bot as MaxBot, Dispatcher as MaxDispatcher

from src.config import (
    BOTAPI_FILE_URL,
    BOTAPI_URL,
    MAX_API_URL,
    MAX_BOT_TOKEN,
    MESSAGE_MAP_TTL,
    PROXY_URL,
    REDIS_URL,
    TELEGRAM_BOT_TOKEN,
)
from src.storage.message_map import MessageMapService

logger = logging.getLogger(__name__)

# Initialize Bots
max_bot = MaxBot(token=MAX_BOT_TOKEN)
if MAX_API_URL:
    max_bot.set_api_url(MAX_API_URL)
    logger.info(f"Using custom MAX API server: {MAX_API_URL}")
else:
    logger.info(f"Using MAX API server: {max_bot.api_url}")
max_dp = MaxDispatcher(router_id="main")

session_kwargs: dict = {}
if PROXY_URL:
    session_kwargs["proxy"] = PROXY_URL
if BOTAPI_URL:
    if BOTAPI_FILE_URL:
        logger.info(f"Using custom Telegram Bot API server: {BOTAPI_URL} (file: {BOTAPI_FILE_URL})")
        session_kwargs["api"] = TelegramAPIServer(base=BOTAPI_URL, file=BOTAPI_FILE_URL)
    else:
        logger.info(f"Using custom Telegram Bot API server: {BOTAPI_URL}")
        session_kwargs["api"] = TelegramAPIServer.from_base(BOTAPI_URL)
session = AiohttpSession(**session_kwargs) if session_kwargs else None
tg_bot = TgBot(
    token=TELEGRAM_BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
tg_dp = TgDispatcher(disable_fsm=True)

message_map = MessageMapService(url=REDIS_URL, ttl_seconds=MESSAGE_MAP_TTL)
