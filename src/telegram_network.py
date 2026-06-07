from __future__ import annotations

import logging
import socket
import subprocess
import time

import httpx

logger = logging.getLogger(__name__)

COMMON_SOCKS_PORTS = (7890, 7891, 10808, 1080, 2080, 9090)


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _telegram_reachable(token: str, proxy: str | None = None, timeout: float = 15.0) -> bool:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def _open_vpn_app(app_path: str) -> None:
    if not app_path:
        return
    try:
        subprocess.run(["open", "-a", app_path], check=False, capture_output=True)
    except OSError as exc:
        logger.warning("Не удалось открыть VPN-приложение: %s", exc)


def _wait_for_stable_access(
    token: str,
    proxy: str | None,
    *,
    stable_checks: int = 2,
    interval: float = 1.5,
    vpn_app_path: str = "",
    wait_seconds: int = 60,
) -> None:
    """Ждёт несколько подряд успешных проверок — VPN часто нестабилен при подключении."""
    _open_vpn_app(vpn_app_path)
    deadline = time.time() + wait_seconds
    consecutive = 0
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        if _telegram_reachable(token, proxy):
            consecutive += 1
            if consecutive >= stable_checks:
                logger.info(
                    "Telegram API стабилен (%s проверок подряд)",
                    stable_checks,
                )
                return
        else:
            consecutive = 0
            if attempt == 1:
                logger.info("Ожидание стабильного доступа к Telegram API...")
        time.sleep(interval)

    raise RuntimeError(
        "Не удалось получить стабильный доступ к api.telegram.org. "
        "Подключите VPN в приложении «ВПН» или укажите рабочий TELEGRAM_PROXY_URL."
    )


def resolve_telegram_proxy(
    token: str,
    configured_proxy: str,
    auto: bool = True,
    vpn_app_path: str = "",
    wait_seconds: int = 120,
) -> str | None:
    """Возвращает рабочий proxy URL или None для прямого доступа через VPN."""
    stable_wait = min(60, wait_seconds)

    if configured_proxy and _telegram_reachable(token, configured_proxy):
        logger.info("Telegram доступен через прокси из .env")
        _wait_for_stable_access(
            token,
            configured_proxy,
            vpn_app_path="",
            wait_seconds=stable_wait,
        )
        return configured_proxy

    if configured_proxy:
        logger.warning(
            "Прокси из .env недоступен (%s).",
            configured_proxy.split("@")[-1],
        )
        if not auto:
            raise RuntimeError(
                "Прокси из TELEGRAM_PROXY_URL не может подключиться к api.telegram.org. "
                "Проверьте логин/пароль и что провайдер прокси разрешает доступ к Telegram."
            )

    if not auto:
        _wait_for_stable_access(
            token,
            None,
            vpn_app_path=vpn_app_path,
            wait_seconds=wait_seconds,
        )
        logger.info("Telegram доступен напрямую")
        return None

    _open_vpn_app(vpn_app_path)
    deadline = time.time() + wait_seconds
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())

        if _telegram_reachable(token):
            logger.info("Telegram доступен напрямую (попытка %s)", attempt)
            _wait_for_stable_access(
                token,
                None,
                vpn_app_path=vpn_app_path,
                wait_seconds=max(30, remaining),
            )
            return None

        for port in COMMON_SOCKS_PORTS:
            if not _port_open("127.0.0.1", port):
                continue
            proxy = f"socks5://127.0.0.1:{port}"
            if _telegram_reachable(token, proxy):
                logger.info("Telegram доступен через SOCKS %s", proxy)
                _wait_for_stable_access(
                    token,
                    proxy,
                    vpn_app_path=vpn_app_path,
                    wait_seconds=max(30, remaining),
                )
                return proxy

        if attempt == 1:
            logger.info("Ожидание VPN и доступа к Telegram API...")
        time.sleep(3)

    raise RuntimeError(
        "Не удалось подключиться к api.telegram.org. "
        "Подключите VPN в приложении «ВПН» или укажите рабочий TELEGRAM_PROXY_URL."
    )
