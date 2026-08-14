import io
import re
from contextlib import nullcontext

import librosa
import torch
import torchaudio
from cachetools import LRUCache, cached

from fish_speech.models.dac.modded_dac import DAC

CACHE_MAXSIZE = 10000
MICRO_BATCH_SIZE = 8
ASR_SAMPLE_RATE = 16000
HUGE_GAP_THRESHOLD = 4000


def model_autocast(model):
    """Match autocast to reduced-precision codec weights when configured.

    The historical float32 codec path keeps its CUDA float16 autocast. A
    resident bfloat16 codec instead uses bfloat16 so VQGAN endpoints do not mix
    float16 activations with bfloat16 weights.
    """
    device_type = torch.device(model.device).type
    if device_type != "cuda":
        return nullcontext()

    model_dtype = next(model.parameters()).dtype
    autocast_dtype = (
        model_dtype
        if model_dtype in (torch.float16, torch.bfloat16)
        else torch.float16
    )
    return torch.autocast(device_type="cuda", dtype=autocast_dtype)


@torch.no_grad()
def batch_encode(model, audios_list: list[bytes]):
    # Get sample rate from model
    if hasattr(model, "spec_transform"):
        sample_rate = model.spec_transform.sample_rate
    else:
        sample_rate = model.sample_rate

    audios: list[torch.Tensor] = [
        (
            torch.from_numpy(librosa.load(io.BytesIO(audio), sr=sample_rate)[0])[None]
            if isinstance(audio, bytes)
            else audio
        )
        for audio in audios_list
    ]

    lengths = torch.tensor([audio.shape[-1] for audio in audios], device=model.device)
    max_length = lengths.max().item()

    print(f"Encode max length: {max_length / sample_rate:.2f}s")

    padded = torch.stack(
        [
            torch.nn.functional.pad(audio, (0, int(max_length - audio.shape[-1])))
            for audio in audios
        ]
    ).to(model.device)

    with model_autocast(model):
        features, feature_lengths = model.encode(padded, audio_lengths=lengths)
    features, feature_lengths = features.cpu(), feature_lengths.cpu()

    return [feature[..., :length] for feature, length in zip(features, feature_lengths)]


@cached(
    cache=LRUCache(maxsize=CACHE_MAXSIZE),
    key=lambda model, audios: (model.device, tuple(audios)),
)
def cached_vqgan_batch_encode(model, audios: list[bytes]):
    return batch_encode(model, audios)


@torch.no_grad()
def batch_vqgan_decode(model, features):
    lengths = torch.tensor(
        [feature.shape[-1] for feature in features], device=model.device
    )
    max_length = lengths.max().item()
    padded = torch.stack(
        [
            torch.nn.functional.pad(feature, (0, max_length - feature.shape[-1]))
            for feature in features
        ]
    ).to(model.device)

    # If bs too large, we do micro batch decode
    audios, audio_lengths = [], []
    for i in range(0, padded.shape[0], MICRO_BATCH_SIZE):
        feature_batch = padded[i : i + MICRO_BATCH_SIZE]
        length_batch = lengths[i : i + MICRO_BATCH_SIZE]
        with model_autocast(model):
            if isinstance(model, DAC):
                audio = model.from_indices(feature_batch.long())
                audio_length = length_batch * model.frame_length
            else:
                audio, audio_length = model.decode(
                    feature_batch,
                    feature_lengths=length_batch,
                )
        audios.append(audio)
        audio_lengths.append(audio_length)
    audios = torch.cat(audios, dim=0)
    audio_lengths = torch.cat(audio_lengths, dim=0)
    audios, audio_lengths = audios.cpu(), audio_lengths.cpu()

    return [audio[..., :length].numpy() for audio, length in zip(audios, audio_lengths)]
