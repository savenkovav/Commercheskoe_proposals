from __future__ import annotations

import logging
import sys
import time

from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from src.bot.admin_handlers import (
    admin_help_command,
    cancel_command,
    price_add_command,
    price_remove_command,
    price_rename_command,
    price_replace_command,
    prices_list_command,
)
from src.bot.handlers import (
    demo_command,
    document_handler,
    find_command,
    help_command,
    start_command,
    status_command,
    text_message_handler,
)
from src.config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CONNECT_TIMEOUT,
    TELEGRAM_PROXY_AUTO,
    TELEGRAM_PROXY_URL,
    TELEGRAM_READ_TIMEOUT,
    TELEGRAM_VPN_APP,
    TELEGRAM_VPN_WAIT_SECONDS,
)
from src.telegram_network import _open_vpn_app, resolve_telegram_proxy

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning("Сбой сети Telegram: %s", error)
        message = getattr(update, "effective_message", None)
        if message:
            try:
                await message.reply_text(
                    "⚠️ Временная проблема с сетью. Подключите VPN и повторите запрос."
                )
            except (TimedOut, NetworkError):
                pass
        return

    logger.exception("Необработанная ошибка: %s", error)


async def on_bot_started(app: Application) -> None:
    logger.info("Бот запущен, polling активен")


def _build_application(proxy: str | None) -> Application:
    request = HTTPXRequest(
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_READ_TIMEOUT,
        proxy=proxy,
    )

    builder = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(on_bot_started)
    )
    if proxy:
        builder = builder.get_updates_request(request)

    app = builder.build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("demo", demo_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(CommandHandler("admin", admin_help_command))
    app.add_handler(CommandHandler("prices", prices_list_command))
    app.add_handler(CommandHandler("price_add", price_add_command))
    app.add_handler(CommandHandler("price_replace", price_replace_command))
    app.add_handler(CommandHandler("price_rename", price_rename_command))
    app.add_handler(CommandHandler("price_remove", price_remove_command))

    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    return app


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан. Скопируйте env.example в .env")
        sys.exit(1)

    retry_delay = 5

    while True:
        try:
            proxy = resolve_telegram_proxy(
                TELEGRAM_BOT_TOKEN,
                TELEGRAM_PROXY_URL,
                auto=TELEGRAM_PROXY_AUTO,
                vpn_app_path=TELEGRAM_VPN_APP,
                wait_seconds=TELEGRAM_VPN_WAIT_SECONDS,
            )
        except RuntimeError as exc:
            logger.error("%s", exc)
            sys.exit(1)

        app = _build_application(proxy)
        logger.info("Подключение к Telegram API...")

        try:
            app.run_polling(
                allowed_updates=["message"],
                drop_pending_updates=True,
                bootstrap_retries=-1,
            )
            break
        except (TimedOut, NetworkError) as exc:
            logger.warning(
                "Сбой сети при запуске polling (%s). Повтор через %s сек...",
                exc,
                retry_delay,
            )
            _open_vpn_app(TELEGRAM_VPN_APP)
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
