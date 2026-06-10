from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.bot.app_state import get_processor, reload_processor
from src.bot.admin_handlers import (
    PENDING_UPLOAD_KEY,
    handle_price_upload,
    is_admin,
)
from src.config import ALLOWED_USER_IDS
from src.services.product_lookup import (
    ProductLookupService,
    format_lookup_response,
    is_lookup_message,
    parse_lookup_query,
)
from src.services.tz_parser import SUPPORTED_TZ_EXTENSIONS, SUPPORTED_TZ_LABEL

logger = logging.getLogger(__name__)


def _is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    text = (
        "👋 *AI-агент коммерческих предложений*\n\n"
        "Отправьте файл *ТЗ* (.docx, .pdf, .xlsx, .xls) от заказчика — бот:\n"
        "1. Распознает позиции из ТЗ\n"
        "2. Найдёт их в каталоге, реестре остатков и прайсах\n"
        "3. При необходимости оценит цену через AI\n"
        "4. Рассчитает себестоимость и наценку 30%\n"
        "5. Сформирует Excel КП по образцу\n\n"
        "*Команды:*\n"
        "/start — это сообщение\n"
        "/status — статус загруженных данных\n"
        "/demo — обработать демо-ТЗ из проекта\n"
        "/find — поиск позиции по названию\n"
        "/help — справка\n\n"
        "*Примеры запроса:*\n"
        "`/find термометр лабораторный | цена, остаток`\n"
        "`сколько стоит мольберт и какой остаток?`"
    )

    if is_admin(update.effective_user.id):
        text += (
            "\n\n*Админ:* /admin — управление прайсами"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    processor = get_processor()
    price_entries = processor.price_manager.list_entries()

    lines = [
        "📦 *Статус данных:*\n",
        f"Каталог: *{len(processor.catalog)}* позиций",
        f"Реестр остатков: *{len(processor.registry)}* позиций",
        f"Прайсы: *{len(processor.price_lists)}* позиций в *{len(price_entries)}* файлах",
        f"AI: *{'включён' if processor.ai.enabled else 'выключен (нет PROXYAPI_API_KEY)'}*",
        f"Защита ПДн: *{'включена' if processor.ai.anonymizer.enabled else 'выключена'}*",
    ]

    if price_entries:
        lines.append("\n*Подключённые прайсы:*")
        for entry in price_entries:
            lines.append(f"• `{entry.id}` — {entry.name} ({entry.items_count} поз.)")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def demo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    from src.config import PROJECT_ROOT

    demo_path = PROJECT_ROOT / "data" / "sample_tz.docx"
    if not demo_path.exists():
        await update.message.reply_text("❌ Демо-файл не найден в data/sample_tz.docx")
        return

    await _process_tz(update, demo_path)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.document:
        return

    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    if context.user_data.get(PENDING_UPLOAD_KEY):
        handled = await handle_price_upload(update, context)
        if handled:
            return

    doc = update.message.document
    filename = doc.file_name or "tz.docx"

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_TZ_EXTENSIONS:
        if is_admin(update.effective_user.id):
            await update.message.reply_text(
                f"⚠️ Обычный режим: отправьте ТЗ ({SUPPORTED_TZ_LABEL}).\n"
                "Для загрузки прайса сначала выполните /price\\_add или /price\\_replace.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"⚠️ Отправьте файл ТЗ: {SUPPORTED_TZ_LABEL}",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    status_msg = await update.message.reply_text("⏳ Загружаю файл...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tz_path = Path(tmpdir) / filename
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(str(tz_path))
        await _process_tz(update, tz_path, status_msg=status_msg)


async def _process_tz(
    update: Update,
    tz_path: Path,
    status_msg=None,
) -> None:
    message = update.message
    if not message:
        return

    if status_msg is None:
        status_msg = await message.reply_text("⏳ Обрабатываю ТЗ...")

    try:
        await status_msg.edit_text(
            "🔍 Ищу позиции в каталоге, реестре и прайсах...\n"
            "Это может занять 30–60 секунд."
        )

        processor = get_processor()
        use_ai = processor.ai.enabled
        output_path, summary, _, _ = processor.process_tz_file(
            tz_path,
            use_ai=use_ai,
        )

        summary_text = processor.format_summary_text(summary)
        await status_msg.edit_text(summary_text, parse_mode=ParseMode.MARKDOWN)

        with open(output_path, "rb") as f:
            await message.reply_document(
                document=f,
                filename=output_path.name,
                caption="📄 Коммерческое предложение (Excel)",
            )
    except Exception as exc:
        logger.exception("Processing failed")
        await status_msg.edit_text(f"❌ Ошибка обработки: {exc}")


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    if not context.args:
        await update.message.reply_text(
            "*Поиск позиции по названию*\n\n"
            "Формат:\n"
            "`/find название | поля`\n\n"
            "Примеры:\n"
            "`/find термометр лабораторный | цена, остаток`\n"
            "`/find мольберт | себестоимость, цена`\n"
            "`/find палочка стеклянная`\n\n"
            "Доступные поля: цена, себестоимость, остаток, количество, "
            "единица, код, поставщик, цена прайса",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    query_text = " ".join(context.args)
    await _handle_product_lookup(update, f"/find {query_text}")


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return

    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    text = update.message.text.strip()
    if not is_lookup_message(text):
        await update.message.reply_text(
            f"Отправьте файл ТЗ ({SUPPORTED_TZ_LABEL}), используйте /demo или запросите позицию:\n"
            "`/find термометр | цена, остаток`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _handle_product_lookup(update, text)


async def _handle_product_lookup(update: Update, text: str) -> None:
    message = update.message
    if not message:
        return

    parsed = parse_lookup_query(text)
    if not parsed:
        await message.reply_text(
            "Не удалось распознать запрос. Пример:\n"
            "`/find термометр лабораторный | цена, остаток`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await message.reply_text(
        f"🔍 Ищу: *{parsed.product_name}*...",
        parse_mode=ParseMode.MARKDOWN,
    )

    processor = get_processor()
    lookup = ProductLookupService(processor.matcher, processor.ai)
    result = lookup.lookup(parsed.product_name, parsed.requested_fields)
    response = format_lookup_response(result)

    await status_msg.edit_text(response, parse_mode=ParseMode.MARKDOWN)
