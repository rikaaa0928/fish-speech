import unittest

from fish_speech.models.text2semantic.context import (
    ContextLengthExceededError,
    resolve_generation_budget,
)


class ContextBudgetTests(unittest.TestCase):
    def test_omitted_limit_uses_remaining_context(self) -> None:
        self.assertEqual(resolve_generation_budget(2560, 731, None), 1829)

    def test_explicit_limit_is_preserved(self) -> None:
        self.assertEqual(resolve_generation_budget(2560, 731, 1000), 1000)

    def test_explicit_oversized_limit_is_rejected(self) -> None:
        with self.assertRaises(ContextLengthExceededError):
            resolve_generation_budget(2560, 731, 1830)

    def test_prompt_that_fills_context_is_rejected(self) -> None:
        with self.assertRaises(ContextLengthExceededError):
            resolve_generation_budget(2560, 2560, None)


if __name__ == "__main__":
    unittest.main()
