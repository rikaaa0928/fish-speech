from __future__ import annotations

import unittest

from fish_worker.api_client import classify_api_error


class ApiErrorCodeTests(unittest.TestCase):
    def test_oom_is_explicit_and_not_retryable(self) -> None:
        self.assertEqual(
            classify_api_error(507, "tts_out_of_memory"),
            ("tts_out_of_memory", False),
        )

    def test_generic_server_failure_remains_retryable(self) -> None:
        self.assertEqual(
            classify_api_error(500, None),
            ("sglang_unavailable", True),
        )


if __name__ == "__main__":
    unittest.main()
