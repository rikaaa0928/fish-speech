from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fish_worker.config import Config


class ConfigDefaultsTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"MANAGER_URL": "ws://manager.test", "WORKER_TOKEN": "test-token"},
        clear=True,
    )
    def test_long_text_defaults(self) -> None:
        config = Config.from_env()

        self.assertEqual(config.api_server_llama_max_seq_len, 8192)
        self.assertEqual(config.api_server_tts_max_new_tokens, 4096)

    @patch.dict(
        os.environ,
        {
            "MANAGER_URL": "ws://manager.test",
            "WORKER_TOKEN": "test-token",
            "API_SERVER_LLAMA_MAX_SEQ_LEN": "",
            "API_SERVER_TTS_MAX_NEW_TOKENS": "",
        },
        clear=True,
    )
    def test_empty_values_disable_worker_overrides(self) -> None:
        config = Config.from_env()

        self.assertIsNone(config.api_server_llama_max_seq_len)
        self.assertIsNone(config.api_server_tts_max_new_tokens)


if __name__ == "__main__":
    unittest.main()
