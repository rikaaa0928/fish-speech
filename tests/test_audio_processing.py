import unittest

import numpy as np

from fish_speech.audio_processing import adjust_speed


class AdjustSpeedTest(unittest.TestCase):
    def setUp(self):
        sample_rate = 44100
        samples = np.arange(sample_rate, dtype=np.float32)
        self.audio = np.sin(2 * np.pi * 440 * samples / sample_rate).astype(
            np.float32
        )

    def test_default_speed_is_noop(self):
        self.assertIs(adjust_speed(self.audio, 1.0), self.audio)

    def test_librosa_faster_audio_is_shorter(self):
        output = adjust_speed(self.audio, 1.5, method="librosa")
        self.assertEqual(len(output), round(len(self.audio) / 1.5))
        self.assertEqual(output.dtype, self.audio.dtype)

    def test_librosa_slower_audio_is_longer(self):
        output = adjust_speed(self.audio, 0.5, method="librosa")
        self.assertEqual(len(output), round(len(self.audio) / 0.5))
        self.assertEqual(output.dtype, self.audio.dtype)

    def test_linear_faster_audio_is_shorter(self):
        output = adjust_speed(self.audio, 1.5, method="linear")
        self.assertEqual(len(output), round(len(self.audio) / 1.5))
        self.assertEqual(output.dtype, self.audio.dtype)

    def test_linear_matches_sglang_interpolation(self):
        audio = np.array([0.0, 1.0, 0.0, -1.0], dtype=np.float32)
        output = adjust_speed(audio, 1.5, method="linear")
        expected = np.interp(
            np.linspace(0.0, len(audio) - 1, round(len(audio) / 1.5)),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)
        np.testing.assert_array_equal(output, expected)

    def test_linear_slower_audio_is_longer(self):
        output = adjust_speed(self.audio, 0.5, method="linear")
        self.assertEqual(len(output), round(len(self.audio) / 0.5))
        self.assertEqual(output.dtype, self.audio.dtype)

    def test_unknown_method_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported speed method"):
            adjust_speed(self.audio, 1.5, method="unknown")


if __name__ == "__main__":
    unittest.main()
