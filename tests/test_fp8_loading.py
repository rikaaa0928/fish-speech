import tempfile
import unittest
from pathlib import Path

import torch
from safetensors.torch import save_file
from torch import nn

from fish_speech.models.text2semantic.llama import (
    FP8Linear,
    _assign_tensor,
    _fp8_weight_names,
    _replace_fp8_linears,
)


class TinyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.projection = nn.Linear(3, 2, bias=True, device="meta")


class FP8LoadingTests(unittest.TestCase):
    def test_detect_replace_and_restore_scaled_fp8_linear(self):
        qweight = torch.tensor(
            [[1.0, -2.0, 0.5], [0.25, 1.0, -1.0]],
            dtype=torch.float8_e4m3fn,
        )
        scale = torch.tensor([0.5, 2.0], dtype=torch.float32)
        bias = torch.tensor([0.25, -0.5], dtype=torch.bfloat16)

        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "model.safetensors"
            save_file(
                {
                    "projection.weight": qweight,
                    "projection.weight.scale": scale,
                    "projection.bias": bias,
                },
                checkpoint,
            )
            fp8_weights = _fp8_weight_names(checkpoint)

        self.assertEqual(fp8_weights, {"projection.weight"})
        model = TinyModel()
        self.assertEqual(_replace_fp8_linears(model, fp8_weights), 1)
        self.assertIsInstance(model.projection, FP8Linear)

        _assign_tensor(model, "projection.qweight", qweight)
        _assign_tensor(model, "projection.scale", scale[:, None])
        _assign_tensor(model, "projection.bias", bias)

        inputs = torch.tensor([[2.0, -1.0, 4.0]], dtype=torch.bfloat16)
        expected_weight = qweight.to(torch.bfloat16) * scale[:, None].to(
            torch.bfloat16
        )
        expected = torch.nn.functional.linear(inputs, expected_weight, bias)
        actual = model.projection(inputs)

        torch.testing.assert_close(actual, expected)
        self.assertEqual(model.projection.qweight.dtype, torch.float8_e4m3fn)
        self.assertEqual(model.projection.scale.dtype, torch.float32)

    def test_rejects_scale_without_e4m3_weight(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "model.safetensors"
            save_file(
                {
                    "projection.weight": torch.ones(2, 3, dtype=torch.bfloat16),
                    "projection.weight.scale": torch.ones(2, dtype=torch.float32),
                },
                checkpoint,
            )
            self.assertEqual(_fp8_weight_names(checkpoint), set())


if __name__ == "__main__":
    unittest.main()
