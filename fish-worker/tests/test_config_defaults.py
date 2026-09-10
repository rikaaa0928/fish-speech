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

    @patch.dict(
        os.environ,
        {
            "MANAGER_URL": "ws://manager.test",
            "WORKER_TOKEN": "test-token",
            "API_SERVER_LLAMA_MAX_SEQ_LEN": "",
        },
        clear=True,
    )
    def test_empty_values_disable_worker_overrides(self) -> None:
        config = Config.from_env()

        self.assertIsNone(config.api_server_llama_max_seq_len)


if __name__ == "__main__":
    unittest.main()
