
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl


def verify_telegram_init_data(init_data: str) -> dict:
    bot_token = os.getenv("BOT_TOKEN")

    if not bot_token:
        raise ValueError("BOT_TOKEN не настроен")

    if not init_data:
        raise ValueError("Отсутствуют данные Telegram")

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
        )
        parsed = dict(pairs)
        if len(parsed) != len(pairs):
            raise ValueError("Дублирующиеся параметры")
    except (TypeError, ValueError):
        raise ValueError("Некорректные данные Telegram")

    received_hash = parsed.pop("hash", None)

    if not received_hash:
        raise ValueError("Отсутствует подпись Telegram")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(parsed.items())
    )

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Подпись Telegram недействительна")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        raise ValueError("Некорректная дата авторизации")

    current_time = int(time.time())

    if auth_date <= 0 or auth_date > current_time + 60:
        raise ValueError("Некорректное время авторизации")

    if current_time - auth_date > 86400:
        raise ValueError("Сессия Telegram устарела")

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        raise ValueError("Некорректные данные пользователя")

    if not isinstance(user, dict) or not user.get("id"):
        raise ValueError("Не удалось определить пользователя Telegram")

    return user
