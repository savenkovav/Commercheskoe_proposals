from __future__ import annotations

import json
import logging
import re
from typing import Optional

from openai import OpenAI

from src.config import (
    OPENAI_MODEL,
    PII_ANONYMIZATION_ENABLED,
    PII_REDACT_ORG_DATA,
    PROXYAPI_API_KEY,
    PROXYAPI_BASE_URL,
)
from src.services.models import MatchSource, MatchStatus, TZItem
from src.services.data_loader import normalize_name
from src.services.pii_anonymizer import PIIAnonymizer, build_org_terms_from_config

logger = logging.getLogger(__name__)


class AIAgent:
    def __init__(self) -> None:
        self.enabled = bool(PROXYAPI_API_KEY)
        self.client: Optional[OpenAI] = None
        self.anonymizer = PIIAnonymizer(
            enabled=PII_ANONYMIZATION_ENABLED,
            redact_org_data=PII_REDACT_ORG_DATA,
            org_terms=build_org_terms_from_config() if PII_REDACT_ORG_DATA else [],
        )
        self._validation_cache: dict[tuple[str, str], bool] = {}
        if self.enabled:
            self.client = OpenAI(
                api_key=PROXYAPI_API_KEY,
                base_url=PROXYAPI_BASE_URL,
                timeout=20.0,
                max_retries=1,
            )

    def match_item(
        self,
        tz_item: TZItem,
        catalog_candidates: list[dict],
        price_candidates: list[dict],
        registry_candidates: list[dict],
    ) -> dict:
        if not self.enabled or not self.client:
            return self._fallback_response()

        prompt_body, session = self.anonymizer.anonymize_prompt_payload(
            tz_name=tz_item.name,
            tz_unit=tz_item.unit,
            tz_quantity=tz_item.quantity,
            tz_specs=tz_item.specifications[:1500],
            catalog_candidates=catalog_candidates[:8],
            price_candidates=price_candidates[:8],
            registry_candidates=registry_candidates[:5],
        )
        self.anonymizer.log_session(session, "match_item")

        prompt = f"""{prompt_body}

Верни JSON строго в формате:
{{
  "status": "exact|similar|not_found",
  "source": "catalog|registry|price_list|web|none",
  "matched_name": "название найденной позиции или пустая строка",
  "unit_cost": число или null,
  "match_score": число от 0 до 100,
  "price_source": "название источника и товара (без URL)",
  "product_url": "прямая ссылка https:// на карточку товара или null (не выдумывай)",
  "notes": "краткое пояснение на русском",
  "alternatives": ["вариант1", "вариант2"]
}}

Правила:
- Сопоставляй позицию ТЗ по паре «наименование + характеристики» вместе, а не только по общему бренду/модели
- Тип товара должен совпадать: парта ≠ колонка ≠ аудиосистема ≠ микрофон ≠ микроскоп ≠ мольберт и т.д.
- Микрофон и микроскоп — разные товары, даже если названия похожи
- Если в ТЗ «колонка Smarty Blue», а в каталоге «парта Smarty Blue» — это not_found (разные товары)
- exact: полное соответствие наименованию и ключевым характеристикам (score >= 90)
- similar: частичное соответствие, требует проверки менеджером (score 70-89)
- not_found: нет подходящей позиции в каталогах/прайсах
- Если в каталогах нет, но можно оценить рыночную себестоимость — source=web, status=similar
- unit_cost: себестоимость за единицу в рублях (без наценки)
- price_source: откуда взята цена (каталог, прайс, реестр, маркетплейс)
- product_url: только реальная ссылка из кандидатов или null — не придумывай URL
- Не используй обезличенные плейсхолдеры в matched_name
- Отвечай только JSON, без markdown"""

        try:
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты возвращаешь только валидный JSON без пояснений. "
                            "Не запрашивай и не используй персональные данные."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
            return self.anonymizer.deanonymize_response(result, session)
        except Exception as exc:
            logger.exception("AI match failed: %s", exc)
            return self._fallback_response()

    def validate_tz_candidate(self, tz_item: TZItem, candidate_name: str) -> dict:
        """Проверяет, что кандидат — тот же тип товара, что в ТЗ (наименование + характеристики)."""
        from src.config import LOCAL_MATCH_THRESHOLD
        from src.services.fuzzy_scoring import name_match_score
        from src.services.tz_search import product_type_conflict, tz_match_query

        candidate = (candidate_name or "").strip()
        if not candidate:
            return {"accept": False, "reason": "пустой кандидат"}

        name_score = name_match_score(
            normalize_name(tz_item.name),
            normalize_name(candidate),
        )
        if name_score >= LOCAL_MATCH_THRESHOLD:
            return {"accept": True, "reason": "точное совпадение наименования"}

        if product_type_conflict(tz_item, candidate):
            return {
                "accept": False,
                "reason": "разный тип товара (например, парта и колонка, микрофон и микроскоп)",
            }

        cache_key = (normalize_name(tz_match_query(tz_item)), normalize_name(candidate))
        if cache_key in self._validation_cache:
            accepted = self._validation_cache[cache_key]
            return {"accept": accepted, "reason": "кэш"}

        if not self.enabled or not self.client:
            return {"accept": True, "reason": "AI недоступен — правила по типу товара"}

        specs = tz_item.specifications[:1200]
        prompt = f"""Позиция из технического задания:
Наименование: {tz_item.name}
Характеристики: {specs or "—"}

Кандидат из каталога/прайса: {candidate}

Это один и тот же товар с учётом наименования И характеристик?
Парта, колонка, аудиосистема, микрофон, микроскоп, мольберт — разные категории.
Микрофон и микроскоп — разные товары (похожие слова, но это не одно и то же).
Общий бренд или модель (например Smarty Blue) НЕ делает товары одинаковыми, если категория разная.

Верни JSON: {{"accept": true/false, "reason": "кратко на русском"}}"""

        try:
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты эксперт по сопоставлению позиций закупки с каталогом. "
                            "Возвращай только JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
            accept = bool(result.get("accept"))
            self._validation_cache[cache_key] = accept
            return {
                "accept": accept,
                "reason": str(result.get("reason") or "").strip(),
            }
        except Exception as exc:
            logger.warning("AI validate_tz_candidate failed: %s", exc)
            return {"accept": True, "reason": "ошибка AI — оставлено правило по типу"}

    def estimate_web_price(self, tz_item: TZItem) -> dict:
        if not self.enabled or not self.client:
            return {
                "status": "not_found",
                "source": "none",
                "matched_name": "",
                "unit_cost": None,
                "match_score": 0,
                "notes": "AI недоступен — требуется ручной подбор",
                "alternatives": [],
            }

        prompt_body, session = self.anonymizer.anonymize_estimate_payload(
            tz_name=tz_item.name,
            tz_specs=tz_item.specifications[:1200],
        )
        self.anonymizer.log_session(session, "estimate_web_price")

        prompt = f"""{prompt_body}

Верни JSON:
{{
  "status": "similar",
  "source": "web",
  "matched_name": "ориентировочное рыночное наименование",
  "unit_cost": число,
  "match_score": число 50-75,
  "price_source": "название товара и площадки-ориентира (без URL)",
  "product_url": null,
  "notes": "оценка по открытым источникам, требует проверки; прямую ссылку не указывай",
  "alternatives": []
}}

Не выдумывай product_url — у тебя нет доступа к интернету, всегда product_url: null."""

        try:
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты эксперт по ценам учебного оборудования в России. "
                            "Возвращай только JSON. Не используй персональные данные. "
                            "Поле price_source: площадка или тип источника. "
                            "product_url всегда null — нет доступа к интернету."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
            return self.anonymizer.deanonymize_response(result, session)
        except Exception as exc:
            logger.exception("AI web estimate failed: %s", exc)
            return {
                "status": "not_found",
                "source": "none",
                "matched_name": "",
                "unit_cost": None,
                "match_score": 0,
                "notes": "Ошибка AI-оценки",
                "alternatives": [],
            }

    def search_competitors(self, tz_item: TZItem, limit: int = 3) -> list[dict]:
        if not self.enabled or not self.client:
            return []

        prompt_body, session = self.anonymizer.anonymize_estimate_payload(
            tz_name=tz_item.name,
            tz_specs=tz_item.specifications[:1200],
        )
        self.anonymizer.log_session(session, "search_competitors")

        prompt = f"""{prompt_body}

Верни JSON с оценкой цен ближайших конкурентов на российском рынке:
{{
  "competitors": [
    {{
      "name": "наименование товара у конкурента",
      "platform": "Ozon|Яндекс.Маркет|другой магазин",
      "price": число,
      "match_score": число 40-80,
      "url": "ссылка на поиск товара на площадке (ozon.ru/search, market.yandex.ru/search и т.п.)",
      "notes": "кратко"
    }}
  ]
}}

Верни ровно {limit} позиции-конкурента с разными площадками если возможно.
В поле url укажи только ссылку на страницу поиска площадки с названием товара, не выдумывай карточки товаров."""

        try:
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты эксперт по ценам учебного и лабораторного оборудования в России. "
                            "Возвращай только JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
            cleaned = self.anonymizer.deanonymize_response(result, session)
            items = cleaned.get("competitors") or []
            if not isinstance(items, list):
                return []
            return items[:limit]
        except Exception as exc:
            logger.exception("AI competitor search failed: %s", exc)
            return []

    def interpret_assistant_message(
        self,
        user_message: str,
        items_summary: list[dict],
        preferences: dict,
        chat_history: list[dict],
        markup_percent: float,
        *,
        task_mode: str,
        stage: str,
        search_completed: bool,
        rag_context: str = "",
    ) -> dict:
        if not self.enabled or not self.client:
            from src.services.assistant_intent import detect_assistant_intent

            patch = detect_assistant_intent(
                user_message,
                has_items=bool(items_summary),
                search_completed=search_completed,
            )
            patch.update(self._extract_refinement_fields(patch, user_message))
            return patch

        history_text = "\n".join(
            f"{turn.get('role', 'user')}: {turn.get('text', '')}"
            for turn in chat_history[-10:]
        )
        items_text = json.dumps(items_summary[:30], ensure_ascii=False)
        prefs_text = json.dumps(preferences, ensure_ascii=False)

        prompt = f"""Ты — КП-Ассистент (версия 2.0). Веди диалог с менеджером по коммерческим предложениям.

ВАЖНО: не запускай поиск в каталогах, прайсах, складе и интернете, пока пользователь явно не попросил.
Сначала уточни задачу, задай вопросы при неполных данных, предложи кнопки действий.

Текущий режим задачи: {task_mode}
Стадия сессии: {stage}
Поиск уже выполнялся: {"да" if search_completed else "нет"}
Наценка: {markup_percent}%
Настройки: {prefs_text}
RAG-контекст ТЗ (релевантные фрагменты документа):
{rag_context or "—"}
Позиции: {items_text}
История:
{history_text or "—"}

Сообщение пользователя:
{user_message}

Верни JSON:
{{
  "reply": "ответ на русском — что понял и что предлагает сделать дальше",
  "task_mode": "task1|task1_task2|null",
  "run_local_search": false,
  "run_web_search": false,
  "generate_excel": false,
  "save_rule": "текст правила или null",
  "markup_percent": число или null,
  "reprocess_items": [],
  "reprocess_all": false,
  "excluded_platforms_add": [],
  "excluded_platforms_remove": [],
  "disabled_sources_add": [],
  "disabled_sources_remove": [],
  "search_kit_component_links": null,
  "force_kit_component_pricing": null
}}

Правила:
- run_local_search=true ТОЛЬКО если пользователь явно просит найти/обработать/подобрать в каталогах, прайсах, складе
- run_web_search=true ТОЛЬКО для задачи 1+2 и явной просьбы про конкурентов/интернет/рынок
- generate_excel=true только если поиск уже был и пользователь просит сформировать/выгрузить КП
- task1 = только поиск и себестоимость; task1_task2 = + конкуренты и рекомендация цены
- save_rule — если пользователь пишет «запомни как правило»
- Если позиций нет — предложи загрузить ТЗ, run_local_search=false
- Если характеристики неполные — задай уточняющий вопрос в reply, run_local_search=false
- null = не менять настройку
- Отвечай только JSON"""

        try:
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты оркестратор КП-Ассистента. "
                            "Не запускай поиск без явной команды пользователя. "
                            "Возвращай только валидный JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)
        except Exception as exc:
            logger.exception("AI assistant message failed: %s", exc)
            from src.services.assistant_intent import detect_assistant_intent

            patch = detect_assistant_intent(
                user_message,
                has_items=bool(items_summary),
                search_completed=search_completed,
            )
            patch.update(self._extract_refinement_fields(patch, user_message))
            return patch

    @staticmethod
    def _extract_refinement_fields(patch: dict, user_message: str) -> dict:
        fallback = AIAgent._fallback_kp_refinement(user_message)
        for key in (
            "excluded_platforms_add",
            "excluded_platforms_remove",
            "disabled_sources_add",
            "disabled_sources_remove",
            "search_kit_component_links",
            "force_kit_component_pricing",
            "markup_percent",
            "reprocess_items",
            "reprocess_all",
        ):
            if not patch.get(key) and fallback.get(key):
                patch[key] = fallback[key]
        return patch

    def interpret_kp_refinement(
        self,
        user_message: str,
        items_summary: list[dict],
        preferences: dict,
        chat_history: list[dict],
        markup_percent: float,
    ) -> dict:
        if not self.enabled or not self.client:
            return self._fallback_kp_refinement(user_message)

        history_text = "\n".join(
            f"{turn.get('role', 'user')}: {turn.get('text', '')}"
            for turn in chat_history[-8:]
        )
        items_text = json.dumps(items_summary[:30], ensure_ascii=False)
        prefs_text = json.dumps(preferences, ensure_ascii=False)

        prompt = f"""Пользователь формирует коммерческое предложение (КП) и просит скорректировать результат.

Текущая наценка: {markup_percent}%
Текущие настройки: {prefs_text}

Позиции КП (кратко):
{items_text}

История чата:
{history_text or "—"}

Новое сообщение пользователя:
{user_message}

Верни JSON:
{{
  "reply": "понятный ответ на русском — что сделано",
  "excluded_platforms_add": ["Ozon"],
  "excluded_platforms_remove": [],
  "disabled_sources_add": ["web"],
  "disabled_sources_remove": [],
  "search_kit_component_links": true или false или null,
  "force_kit_component_pricing": true или false или null,
  "markup_percent": число или null,
  "reprocess_items": [номера позиций ТЗ] или [],
  "reprocess_all": true или false
}}

Правила:
- excluded_platforms_add/remove: площадки конкурентов (Ozon, Яндекс.Маркет, Wildberries и т.п.)
- disabled_sources_add: catalog, price_list, registry, web — отключить источник
- search_kit_component_links=true — искать ссылки по составляющим комплекта (медленно)
- force_kit_component_pricing=true — пересчитать цену комплекта по сумме составляющих
- reprocess_items — номера позиций из ТЗ для повторного подбора
- reprocess_all=true — пересчитать все позиции
- markup_percent — новая наценка в %, если пользователь просит изменить
- Если просят «не используй Ozon» — excluded_platforms_add: ["Ozon"], reprocess_all: true
- Если просят «считай по составляющим» — force_kit_component_pricing: true, reprocess_items: номера комплектов
- null в полях = не менять настройку
- Отвечай только JSON"""

        try:
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты помощник менеджера по коммерческим предложениям. "
                            "Возвращай только валидный JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)
        except Exception as exc:
            logger.exception("AI KP refinement failed: %s", exc)
            return self._fallback_kp_refinement(user_message)

    @staticmethod
    def _fallback_kp_refinement(user_message: str) -> dict:
        text = user_message.lower()
        patch: dict = {
            "reply": "Применены базовые правила (AI недоступен).",
            "excluded_platforms_add": [],
            "excluded_platforms_remove": [],
            "disabled_sources_add": [],
            "disabled_sources_remove": [],
            "search_kit_component_links": None,
            "force_kit_component_pricing": None,
            "markup_percent": None,
            "reprocess_items": [],
            "reprocess_all": False,
        }

        for platform in ("ozon", "wildberries", "яндекс", "маркет", "авито"):
            if platform in text and ("не использ" in text or "исключ" in text or "убери" in text):
                label = {
                    "ozon": "Ozon",
                    "wildberries": "Wildberries",
                    "яндекс": "Яндекс.Маркет",
                    "маркет": "Яндекс.Маркет",
                    "авито": "Авито",
                }[platform]
                patch["excluded_platforms_add"].append(label)
                patch["reprocess_all"] = True

        if "составляющ" in text or "по составу" in text:
            patch["force_kit_component_pricing"] = True
            patch["reprocess_all"] = True
            patch["reply"] = "Пересчитываю комплекты по составляющим."

        if "ссылк" in text and "состав" in text:
            patch["search_kit_component_links"] = True
            patch["reprocess_all"] = True

        if "интернет" in text and ("не использ" in text or "отключ" in text):
            patch["disabled_sources_add"].append("web")
            patch["reprocess_all"] = True

        markup_match = re.search(r"наценк\w*\s*(\d+(?:[.,]\d+)?)\s*%?", text)
        if markup_match:
            patch["markup_percent"] = float(markup_match.group(1).replace(",", "."))
            patch["reply"] = f"Наценка изменена на {patch['markup_percent']}%."

        numbers = [int(n) for n in re.findall(r"(?:позици\w*|№|#)\s*(\d+)", text)]
        if numbers:
            patch["reprocess_items"] = numbers
            patch["reply"] = f"Пересчитываю позиции: {', '.join(map(str, numbers))}."

        if "пересчит" in text and "все" in text:
            patch["reprocess_all"] = True

        return patch

    @staticmethod
    def _fallback_response() -> dict:
        return {
            "status": "not_found",
            "source": "none",
            "matched_name": "",
            "unit_cost": None,
            "match_score": 0,
            "notes": "AI недоступен",
            "alternatives": [],
        }

    @staticmethod
    def parse_status(value: str) -> MatchStatus:
        mapping = {
            "exact": MatchStatus.EXACT,
            "similar": MatchStatus.SIMILAR,
            "not_found": MatchStatus.NOT_FOUND,
        }
        return mapping.get(value, MatchStatus.NOT_FOUND)

    @staticmethod
    def parse_source(value: str) -> MatchSource:
        mapping = {
            "catalog": MatchSource.CATALOG,
            "registry": MatchSource.REGISTRY,
            "price_list": MatchSource.PRICE_LIST,
            "web": MatchSource.WEB,
            "ai": MatchSource.AI,
        }
        return mapping.get(value, MatchSource.NONE)
