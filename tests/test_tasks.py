import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tasks import get_action_price, validate_url, WORK_HOURS


class TaskInputValidationTests(unittest.TestCase):
    def test_supported_task_prices_are_read_in_kopecks(self):
        with patch.dict("os.environ", {
            "TASK_PRICE_SURVEY_KOPECKS": "2500",
            "TASK_PRICE_FEEDBACK_KOPECKS": "3000",
            "TASK_PRICE_USABILITY_KOPECKS": "4500",
        }):
            self.assertEqual(get_action_price("survey"), 2500)
            self.assertEqual(get_action_price("content_feedback"), 3000)
            self.assertEqual(get_action_price("usability_test"), 4500)

    def test_unknown_task_type_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            get_action_price("likes")
        self.assertEqual(error.exception.status_code, 400)

    def test_missing_task_price_is_reported_as_configuration_error(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(HTTPException) as error:
                get_action_price("survey")
        self.assertEqual(error.exception.status_code, 503)

    def test_non_integer_and_non_positive_prices_are_rejected(self):
        for value in ("25.5", "abc", "0", "-100"):
            with self.subTest(value=value), patch.dict(
                "os.environ", {"TASK_PRICE_SURVEY_KOPECKS": value}
            ):
                with self.assertRaises(HTTPException):
                    get_action_price("survey")

    def test_only_http_and_https_target_urls_are_accepted(self):
        self.assertEqual(
            validate_url("https://example.com/task"),
            "https://example.com/task",
        )
        self.assertEqual(
            validate_url("http://example.com/task"),
            "http://example.com/task",
        )
        for value in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "example.com/task",
            "https://",
            "ftp://example.com/task",
        ):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                validate_url(value)

    def test_target_url_length_is_limited(self):
        with self.assertRaises(HTTPException):
            validate_url("https://example.com/" + "a" * 1000)

    def test_work_and_revision_deadline_is_one_hour(self):
        self.assertEqual(WORK_HOURS, 1)


if __name__ == "__main__":
    unittest.main()
