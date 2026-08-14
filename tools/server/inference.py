from dataclasses import dataclass
from http import HTTPStatus

import numpy as np
import torch
from kui.asgi import HTTPException

from fish_speech.inference_engine import TTSInferenceEngine
from fish_speech.models.text2semantic.inference import ContextLengthExceededError
from fish_speech.utils.schema import ServeTTSRequest

AMPLITUDE = 32768  # Needs an explaination
TTS_OOM_ERROR_CODE = "tts_out_of_memory"
TTS_TRUNCATED_ERROR_CODE = "tts_output_truncated"


@dataclass(frozen=True)
class FinalAudio:
    audio: np.ndarray
    finish_reason: str
    generated_tokens: int
    max_new_tokens: int
    input_characters: int


def inference_wrapper(req: ServeTTSRequest, engine: TTSInferenceEngine):
    """
    Wrapper for the inference function.
    Used in the API server.
    """
    count = 0
    for result in engine.inference(req):
        match result.code:
            case "header":
                if isinstance(result.audio, tuple):
                    header = result.audio[1]
                    yield (
                        header.tobytes()
                        if isinstance(header, np.ndarray)
                        else header
                    )

            case "error":
                if isinstance(result.error, torch.OutOfMemoryError):
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    raise HTTPException(
                        HTTPStatus.INSUFFICIENT_STORAGE,
                        content="TTS generation ran out of GPU memory",
                        headers={"X-TTS-Error-Code": TTS_OOM_ERROR_CODE},
                    )
                status = (
                    HTTPStatus.UNPROCESSABLE_ENTITY
                    if isinstance(result.error, ContextLengthExceededError)
                    else HTTPStatus.INTERNAL_SERVER_ERROR
                )
                raise HTTPException(
                    status,
                    content=str(result.error),
                )

            case "segment":
                count += 1
                if isinstance(result.audio, tuple):
                    yield (result.audio[1] * AMPLITUDE).astype(np.int16).tobytes()

            case "final":
                count += 1
                if isinstance(result.audio, tuple):
                    yield FinalAudio(
                        audio=result.audio[1],
                        finish_reason=result.finish_reason or "stop",
                        generated_tokens=result.generated_tokens or 0,
                        max_new_tokens=result.max_new_tokens or req.max_new_tokens,
                        input_characters=result.input_characters or len(req.text),
                    )
                return None  # Stop the generator

    if count == 0:
        raise HTTPException(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            content="No audio generated, please check the input text.",
        )
