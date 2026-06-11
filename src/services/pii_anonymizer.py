from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AnonymizationSession:
    """Сессия обезличивания для одного AI-запроса."""

    token_map: dict[str, str] = field(default_factory=dict)
    replacements: int = 0

    def register(self, original: str, category: str) -> str:
        normalized = original.strip()
        if not normalized:
            return original

        for token, value in self.token_map.items():
            if value == normalized:
                return token

        index = sum(1 for key in self.token_map if key.startswith(f"[{category}_"))
        token = f"[{category}_{index + 1}]"
        self.token_map[token] = normalized
        self.replacements += 1
        return token


class PIIAnonymizer:
    """Обезличивание текста перед отправкой во внешние AI-сервисы."""

    _PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        (
            "URL",
            re.compile(
                r"https?://[^\s<>\"']+|"
                r"www\.[^\s<>\"']+",
                re.IGNORECASE,
            ),
        ),
        (
            "EMAIL",
            re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
        ),
        (
            "SNILS",
            re.compile(r"\b\d{3}[-\s]?\d{3}[-\s]?\d{3}[-\s]?\d{2}\b"),
        ),
        (
            "INN",
            re.compile(
                r"\b(?:ИНН|INN)[\s:]*\d{10}(?:\d{2})?\b",
                re.IGNORECASE,
            ),
        ),
        (
            "KPP",
            re.compile(r"\b(?:КПП|KPP)[\s:]*\d{9}\b", re.IGNORECASE),
        ),
        (
            "OGRN",
            re.compile(
                r"\b(?:ОГРН|OGRN)[\s:]*\d{13}(?:\d{2})?\b",
                re.IGNORECASE,
            ),
        ),
        (
            "PASSPORT",
            re.compile(r"\b\d{4}\s+\d{6}\b"),
        ),
        (
            "PHONE",
            re.compile(
                r"(?:\+7|8|7)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}\b|"
                r"\b\d{3}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}\b"
            ),
        ),
        (
            "CARD",
            re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"),
        ),
        (
            "BANK_ACCOUNT",
            re.compile(r"\b\d{20}\b"),
        ),
        (
            "BIK",
            re.compile(r"\b(?:БИК|BIK)[\s:]*\d{9}\b", re.IGNORECASE),
        ),
        (
            "USERNAME",
            re.compile(r"@[A-Za-z0-9_]{4,32}\b"),
        ),
        (
            "IP",
            re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
        ),
        (
            "ADDRESS",
            re.compile(
                r"(?:г\.|город|ул\.|улица|пр\.|проспект|пер\.|переулок|"
                r"ш\.|шоссе|д\.|дом|кв\.|квартира|стр\.|строение|"
                r"обл\.|область|м\.р-н|район)[^\n,.;]{0,80}",
                re.IGNORECASE,
            ),
        ),
        (
            "FIO",
            re.compile(
                r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\s+"
                r"[А-ЯЁ][а-яё]+(?:ович|евич|овна|евна|ична|инич)\b"
            ),
        ),
    ]

    _SENSITIVE_JSON_KEYS = {
        "link",
        "url",
        "email",
        "phone",
        "fio",
        "contact",
        "address",
        "passport",
        "snils",
    }

    def __init__(
        self,
        enabled: bool = True,
        redact_org_data: bool = True,
        org_terms: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.redact_org_data = redact_org_data
        self.org_terms = sorted(
            {term.strip() for term in (org_terms or []) if term and len(term.strip()) > 3},
            key=len,
            reverse=True,
        )

    def new_session(self) -> AnonymizationSession:
        return AnonymizationSession()

    def anonymize_text(self, text: str, session: AnonymizationSession) -> str:
        if not self.enabled or not text:
            return text

        result = text
        for category, pattern in self._PATTERNS:
            result = self._replace_matches(result, pattern, category, session)

        if self.redact_org_data:
            for term in self.org_terms:
                if term in result:
                    token = session.register(term, "ORG")
                    result = result.replace(term, token)

        return result

    def anonymize_json(self, data: Any, session: AnonymizationSession) -> Any:
        if not self.enabled:
            return data

        if isinstance(data, dict):
            sanitized: dict[str, Any] = {}
            for key, value in data.items():
                if key.lower() in self._SENSITIVE_JSON_KEYS:
                    continue
                sanitized[key] = self.anonymize_json(value, session)
            return sanitized

        if isinstance(data, list):
            return [self.anonymize_json(item, session) for item in data]

        if isinstance(data, str):
            return self.anonymize_text(data, session)

        return data

    def anonymize_prompt_payload(
        self,
        tz_name: str,
        tz_unit: str,
        tz_quantity: float,
        tz_specs: str,
        catalog_candidates: list[dict],
        price_candidates: list[dict],
        registry_candidates: list[dict],
    ) -> tuple[str, AnonymizationSession]:
        session = self.new_session()

        safe_tz = {
            "name": self.anonymize_text(tz_name, session),
            "quantity": tz_quantity,
            "unit": tz_unit,
            "specifications": self.anonymize_text(tz_specs, session),
        }
        safe_catalog = self.anonymize_json(catalog_candidates, session)
        safe_registry = self.anonymize_json(registry_candidates, session)
        safe_prices = self.anonymize_json(price_candidates, session)

        prompt = (
            "Ты — эксперт по подбору учебного оборудования для коммерческих предложений.\n\n"
            "Задача: найти лучшее соответствие позиции из ТЗ заказчика в каталогах и прайсах.\n"
            "Данные обезличены: плейсхолдеры вида [EMAIL_1], [PHONE_1] не являются названиями товаров.\n\n"
            f"Позиция ТЗ:\n{json.dumps(safe_tz, ensure_ascii=False, indent=2)}\n\n"
            f"Кандидаты из каталога (JSON):\n"
            f"{json.dumps(safe_catalog, ensure_ascii=False, indent=2)}\n\n"
            f"Кандидаты из реестра остатков (JSON):\n"
            f"{json.dumps(safe_registry, ensure_ascii=False, indent=2)}\n\n"
            f"Кандидаты из прайсов поставщиков (JSON):\n"
            f"{json.dumps(safe_prices, ensure_ascii=False, indent=2)}"
        )
        return prompt, session

    def anonymize_estimate_payload(
        self,
        tz_name: str,
        tz_specs: str,
    ) -> tuple[str, AnonymizationSession]:
        session = self.new_session()
        safe = {
            "name": self.anonymize_text(tz_name, session),
            "specifications": self.anonymize_text(tz_specs, session),
        }
        prompt = (
            "Оцени ориентировочную закупочную себестоимость (в рублях, без наценки) для позиции.\n"
            "Данные обезличены: игнорируй плейсхолдеры [EMAIL_1], [PHONE_1] и т.п.\n\n"
            f"{json.dumps(safe, ensure_ascii=False, indent=2)}"
        )
        return prompt, session

    def deanonymize_response(self, payload: dict, session: AnonymizationSession) -> dict:
        if not self.enabled or not session.token_map:
            return payload

        result = dict(payload)
        for key in ("matched_name", "notes"):
            value = result.get(key)
            if isinstance(value, str):
                result[key] = self.deanonymize_text(value, session)

        alternatives = result.get("alternatives")
        if isinstance(alternatives, list):
            result["alternatives"] = [
                self.deanonymize_text(item, session) if isinstance(item, str) else item
                for item in alternatives
            ]

        return result

    def deanonymize_text(self, text: str, session: AnonymizationSession) -> str:
        if not text or not session.token_map:
            return text

        result = text
        for token, original in session.token_map.items():
            result = result.replace(token, original)
        return result

    def log_session(self, session: AnonymizationSession, context: str) -> None:
        if not self.enabled:
            return
        if session.replacements:
            logger.info(
                "PII anonymization (%s): %s replacements applied",
                context,
                session.replacements,
            )
        else:
            logger.debug("PII anonymization (%s): no sensitive data detected", context)

    @staticmethod
    def _replace_matches(
        text: str,
        pattern: re.Pattern[str],
        category: str,
        session: AnonymizationSession,
    ) -> str:
        def replacer(match: re.Match[str]) -> str:
            return session.register(match.group(0), category)

        return pattern.sub(replacer, text)


def build_org_terms_from_config() -> list[str]:
    from src.config import (
        COMPANY_ADDRESS,
        COMPANY_INN,
        COMPANY_KPP,
        COMPANY_NAME,
        COMPANY_OGRN,
        PII_EXTRA_TERMS,
    )

    terms = [COMPANY_NAME, COMPANY_ADDRESS, *PII_EXTRA_TERMS]
    if COMPANY_INN:
        terms.append(COMPANY_INN)
        terms.append(f"ИНН {COMPANY_INN}")
    if COMPANY_KPP:
        terms.append(COMPANY_KPP)
        terms.append(f"КПП {COMPANY_KPP}")
    if COMPANY_OGRN:
        terms.append(COMPANY_OGRN)
        terms.append(f"ОГРН {COMPANY_OGRN}")
    return terms
