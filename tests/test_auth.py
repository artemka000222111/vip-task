import hashlib
import hmac
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from auth import verify_telegram_init_data


TOKEN = "test-bot-token"


def make_init_data(user=None, auth_date=1_800_000_000):
    values = {
        "auth_date": str(auth_date),
        "query_id": "test-query",
        "user": json.dumps(user or {"id": 123456, "first_name": "Test"}),
    }
    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret = hmac.new(
        b"WebAppData", TOKEN.encode(), hashlib.sha256
    ).digest()
    signature = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()
    values["hash"] = signature
    return urlencode(values)


class TelegramAuthTests(unittest.TestCase):
    def test_accepts_valid_signed_init_data(self):
        with patch.dict("os.environ", {"BOT_TOKEN": TOKEN}), patch(
            "auth.time.time", return_value=1_800_000_100
        ):
            user = verify_telegram_init_data(make_init_data())
        self.assertEqual(user["id"], 123456)

    def test_rejects_tampered_user_data(self):
        init_data = make_init_data().replace("first_name", "last_name")
        with patch.dict("os.environ", {"BOT_TOKEN": TOKEN}), patch(
            "auth.time.time", return_value=1_800_000_100
        ):
            with self.assertRaisesRegex(ValueError, "(?i)подпись"):
                verify_telegram_init_data(init_data)

    def test_rejects_expired_init_data(self):
        with patch.dict("os.environ", {"BOT_TOKEN": TOKEN}), patch(
            "auth.time.time", return_value=1_900_000_000
        ):
            with self.assertRaisesRegex(ValueError, "устарела"):
                verify_telegram_init_data(make_init_data(auth_date=1_800_000_000))

    def test_rejects_future_auth_date(self):
        with patch.dict("os.environ", {"BOT_TOKEN": TOKEN}), patch(
            "auth.time.time", return_value=1_800_000_000
        ):
            with self.assertRaisesRegex(ValueError, "время"):
                verify_telegram_init_data(make_init_data(auth_date=1_800_000_100))

    def test_rejects_duplicate_parameters(self):
        init_data = make_init_data() + "&auth_date=1800000000"
        with patch.dict("os.environ", {"BOT_TOKEN": TOKEN}):
            with self.assertRaisesRegex(ValueError, "Некорректные"):
                verify_telegram_init_data(init_data)

    def test_rejects_missing_signature(self):
        init_data = "auth_date=1800000000&user=%7B%22id%22%3A123%7D"
        with patch.dict("os.environ", {"BOT_TOKEN": TOKEN}):
            with self.assertRaisesRegex(ValueError, "подпись"):
                verify_telegram_init_data(init_data)


if __name__ == "__main__":
    unittest.main()
