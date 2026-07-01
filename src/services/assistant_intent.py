from __future__ import annotations

import re


LOCAL_SEARCH_TRIGGERS = (
    "найди",
    "найти",
    "ищи",
    "искать",
    "поиск",
    "обработай",
    "подбери",
    "сопостав",
    "рассчитай себест",
    "начни поиск",
    "запусти поиск",
    "ищи в каталог",
    "ищи в прайс",
)

WEB_SEARCH_TRIGGERS = (
    "конкурент",
    "интернет",
    "маркетплейс",
    "ozon",
    "wildberries",
    "яндекс",
    "рынок",
    "задача 2",
    "задача 1+2",
    "1+2",
    "1 и 2",
)

EXCEL_TRIGGERS = (
    "сформируй excel",
    "сформируй кп",
    "выгрузи excel",
    "скачать excel",
    "готовый файл",
)

RULE_TRIGGERS = (
    "запомни как правило",
    "запомни правило",
    "сохрани правило",
)


def detect_assistant_intent(message: str, *, has_items: bool, search_completed: bool) -> dict:
    text = message.strip().lower()

    intent: dict = {
        "reply": "",
        "task_mode": None,
        "run_local_search": False,
        "run_web_search": False,
        "generate_excel": False,
        "save_rule": None,
        "markup_percent": None,
        "reprocess_items": [],
        "reprocess_all": False,
        "excluded_platforms_add": [],
        "excluded_platforms_remove": [],
        "disabled_sources_add": [],
        "disabled_sources_remove": [],
        "search_kit_component_links": None,
        "force_kit_component_pricing": None,
    }

    if "задача 1+2" in text or "задача 1 и 2" in text or "1+2" in text:
        intent["task_mode"] = "task1_task2"
    elif re.search(r"задач\w*\s*1\b", text) and "2" not in text:
        intent["task_mode"] = "task1"

    if any(token in text for token in RULE_TRIGGERS):
        rule_text = message.split(":", 1)[-1].strip() if ":" in message else message
        intent["save_rule"] = rule_text
        intent["reply"] = "Правило сохранено для этой сессии."

    markup_match = re.search(r"наценк\w*\s*(\d+(?:[.,]\d+)?)\s*%?", text)
    if markup_match:
        intent["markup_percent"] = float(markup_match.group(1).replace(",", "."))
        intent["reply"] = f"Наценка установлена: {intent['markup_percent']}%."

    if "только поиск" in text or "задача 1" in text:
        intent["task_mode"] = intent["task_mode"] or "task1"
        intent["run_local_search"] = has_items

    if intent["task_mode"] == "task1_task2" or any(token in text for token in WEB_SEARCH_TRIGGERS):
        if has_items and any(token in text for token in LOCAL_SEARCH_TRIGGERS + WEB_SEARCH_TRIGGERS):
            intent["run_local_search"] = True
            intent["run_web_search"] = True
            intent["task_mode"] = "task1_task2"

    if any(token in text for token in LOCAL_SEARCH_TRIGGERS):
        intent["run_local_search"] = has_items

    if any(token in text for token in EXCEL_TRIGGERS):
        intent["generate_excel"] = search_completed

    numbers = [int(n) for n in re.findall(r"(?:позици\w*|№|#)\s*(\d+)", text)]
    if numbers and ("пересчит" in text or "найди" in text or "ищи" in text):
        intent["reprocess_items"] = numbers
        intent["run_local_search"] = has_items

    if "пересчит" in text and "все" in text:
        intent["reprocess_all"] = True
        intent["run_local_search"] = has_items

    if "составляющ" in text or "по составу" in text:
        intent["force_kit_component_pricing"] = True

    if "интернет" in text and ("не использ" in text or "отключ" in text or "без" in text):
        intent["disabled_sources_add"].append("web")

    for platform, label in (
        ("ozon", "Ozon"),
        ("wildberries", "Wildberries"),
        ("яндекс", "Яндекс.Маркет"),
        ("маркет", "Яндекс.Маркет"),
    ):
        if platform in text and ("не использ" in text or "исключ" in text or "убери" in text):
            intent["excluded_platforms_add"].append(label)

    if not intent["reply"]:
        if intent["run_local_search"] and intent["run_web_search"]:
            intent["reply"] = "Запускаю поиск по внутренним источникам и анализ конкурентных цен."
        elif intent["run_local_search"]:
            intent["reply"] = "Запускаю поиск по складу, закупкам и прайсам."
        elif intent["generate_excel"] and not search_completed:
            intent["reply"] = "Сначала выполните поиск — нажмите «Только поиск» или напишите «найди в каталогах»."
        elif not has_items:
            intent["reply"] = (
                "Напишите название товара — найду в каталоге, прайсах и реестре. "
                "Для расчёта КП по ТЗ загрузите файл или опишите список позиций."
            )
        else:
            intent["reply"] = (
                "Позиции загружены. Выберите задачу и нажмите быструю кнопку "
                "или напишите, например: «найди в каталогах»."
            )

    return intent
