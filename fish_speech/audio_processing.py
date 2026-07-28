from typing import Literal

import numpy as np


SpeedMethod = Literal["librosa", "linear"]


def adjust_speed(
    segment: np.ndarray,
    speed: float,
    method: SpeedMethod = "librosa",
) -> np.ndarray:
    """Change speech speed with the configured audio processing method."""
    if speed == 1.0 or segment.size < 2:
        return segment

    if method == "librosa":
        # Librosa is imported lazily so the default speed has no startup cost.
        import librosa

        adjusted = librosa.effects.time_stretch(
            segment.astype(np.float32, copy=False),
            rate=speed,
        )
    elif method == "linear":
        # Match SGLang-Omni's lightweight np.interp implementation.
        new_length = max(int(round(segment.size / speed)), 1)
        old_indices = np.arange(segment.size, dtype=np.float64)
        new_indices = np.linspace(
            0.0,
            segment.size - 1,
            num=new_length,
            dtype=np.float64,
        )
        adjusted = np.interp(new_indices, old_indices, segment).astype(np.float32)
    else:
        raise ValueError(f"Unsupported speed method: {method}")

    return adjusted.astype(segment.dtype, copy=False)
