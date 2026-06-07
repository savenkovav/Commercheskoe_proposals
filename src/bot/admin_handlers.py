from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.bot.app_state import reload_processor
from src.config import ADMIN_USER_IDS
from src.services.price_list_manager import get_price_list_manager

logger = logging.getLogger(__name__)

PENDING_UPLOAD_KEY = "pending_price_upload"
PRICE_EXTENSIONS = (".xls", ".xlsx")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def _require_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return False
    return True


def _get_price_manager(context: ContextTypes.DEFAULT_TYPE):
    return get_price_list_manager()


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PENDING_UPLOAD_KEY, None)


def _set_pending(
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    price_id: str | None = None,
    name: str = "",
    supplier: str = "",
) -> None:
    context.user_data[PENDING_UPLOAD_KEY] = {
        "action": action,
        "price_id": price_id,
        "name": name,
        "supplier": supplier,
    }


def _parse_name_supplier(args: list[str]) -> tuple[str, str]:
    text = " ".join(args).strip()
    if not text:
        return "", ""

    if "|" in text:
        name, supplier = text.split("|", 1)
        return name.strip(), supplier.strip()

    return text, text


async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
        return

    text = (
        "🛠 *Админ-команды: управление прайсами*\n\n"
        "/prices — список загруженных прайсов\n"
        "/price\\_add *Название\\|Поставщик* — добавить новый прайс\n"
        "  _Пример:_ `/price_add Природоведение|ООО Природоведение`\n"
        "  Затем отправьте файл `.xls` или `.xlsx`\n\n"
        "/price\\_replace *id* — заменить файл существующего прайса\n"
        "  _Пример:_ `/price_replace prirodovedenie`\n"
        "  Затем отправьте новый файл\n\n"
        "/price\\_rename *id* *Название\\|Поставщик* — изменить название/поставщика\n"
        "/price\\_remove *id* — удалить прайс\n"
        "/cancel — отменить ожидание загрузки файла"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def prices_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
        return

    manager = _get_price_manager(context)
    await update.message.reply_text(
        manager.format_list_text(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def price_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
        return

    name, supplier = _parse_name_supplier(context.args or [])
    if not name:
        await update.message.reply_text(
            "Использование: `/price_add Название|Поставщик`\n"
            "Пример: `/price_add Природоведение|ООО Природоведение`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _set_pending(context, action="add", name=name, supplier=supplier or name)
    await update.message.reply_text(
        f"📥 Ожидаю файл прайса для *{name}*.\n"
        f"Поставщик: _{supplier or name}_\n\n"
        "Отправьте `.xls` или `.xlsx`, либо /cancel для отмены.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def price_replace_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: `/price_replace id`\n"
            "Список id: /prices",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    price_id = context.args[0].strip()
    manager = _get_price_manager(context)
    entry = manager.get_entry(price_id)
    if not entry:
        await update.message.reply_text(f"❌ Прайс `{price_id}` не найден.", parse_mode=ParseMode.MARKDOWN)
        return

    _set_pending(context, action="replace", price_id=entry.id)
    await update.message.reply_text(
        f"📥 Ожидаю новый файл для прайса *{entry.name}* (`{entry.id}`).\n"
        "Отправьте `.xls` или `.xlsx`, либо /cancel для отмены.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def price_rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: `/price_rename id Название|Поставщик`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    price_id = context.args[0].strip()
    name, supplier = _parse_name_supplier(context.args[1:])

    manager = _get_price_manager(context)
    try:
        entry = manager.update_meta(price_id, name=name or None, supplier=supplier or None)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    reload_processor(context.application.bot_data)

    await update.message.reply_text(
        f"✅ Прайс `{entry.id}` обновлён.\n"
        f"Название: *{entry.name}*\n"
        f"Поставщик: _{entry.supplier}_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def price_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin(update):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: `/price_remove id`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    price_id = context.args[0].strip()
    manager = _get_price_manager(context)

    try:
        entry = manager.remove(price_id)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    reload_processor(context.application.bot_data)

    await update.message.reply_text(
        f"🗑 Прайс *{entry.name}* (`{entry.id}`) удалён.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get(PENDING_UPLOAD_KEY)
    if not pending:
        await update.message.reply_text("Нет активной операции загрузки.")
        return

    _clear_pending(context)
    await update.message.reply_text("✅ Загрузка прайса отменена.")


async def handle_price_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает True, если документ обработан как загрузка прайса."""
    pending = context.user_data.get(PENDING_UPLOAD_KEY)
    if not pending or not update.message or not update.message.document:
        return False

    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("⛔ Загрузка прайсов доступна только администраторам.")
        _clear_pending(context)
        return True

    doc = update.message.document
    filename = doc.file_name or "price.xls"
    if not filename.lower().endswith(PRICE_EXTENSIONS):
        await update.message.reply_text(
            "⚠️ Для прайса отправьте файл `.xls` или `.xlsx`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    status_msg = await update.message.reply_text("⏳ Загружаю и проверяю прайс...")

    manager = _get_price_manager(context)

    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / filename
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(str(source_path))

        try:
            if pending["action"] == "add":
                entry = manager.add(
                    name=pending["name"],
                    supplier=pending["supplier"],
                    source_path=source_path,
                )
                action_text = "добавлен"
            elif pending["action"] == "replace":
                entry = manager.replace(pending["price_id"], source_path)
                action_text = "обновлён"
            else:
                raise ValueError("Неизвестная операция загрузки")
        except ValueError as exc:
            await status_msg.edit_text(f"❌ {exc}")
            return True

    _clear_pending(context)

    total_items = reload_processor(context.application.bot_data)

    await status_msg.edit_text(
        f"✅ Прайс *{entry.name}* (`{entry.id}`) {action_text}.\n"
        f"Позиций в прайсе: *{entry.items_count}*\n"
        f"Всего позиций в поиске: *{total_items}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    return True
