from __future__ import annotations

import json
import logging
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
        if self.enabled:
            self.client = OpenAI(
                api_key=PROXYAPI_API_KEY,
                base_url=PROXYAPI_BASE_URL,
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
