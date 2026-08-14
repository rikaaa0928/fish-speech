import torch
from loguru import logger

from fish_speech.audio_processing import SpeedMethod
from fish_speech.inference_engine import TTSInferenceEngine
from fish_speech.models.dac.inference import load_model as load_decoder_model
from fish_speech.models.text2semantic.inference import launch_thread_safe_queue
from fish_speech.utils.schema import ServeTTSRequest
from tools.server.inference import inference_wrapper as inference


def log_gpu_memory(stage: str) -> None:
    """Reusable startup GPU memory snapshot for the API server lifecycle.

    Records both allocated/reserved and the running peaks. Call
    torch.cuda.synchronize() first for accurate numbers; peaks reflect the
    highest value seen since the last reset_peak_memory_stats() call.
    """
    if not torch.cuda.is_available():
        logger.info(f"[gpu-mem] {stage}: CUDA not available, skipped")
        return
    torch.cuda.synchronize()
    alloc = torch.cuda.memory_allocated() / 2**30
    reserved = torch.cuda.memory_reserved() / 2**30
    peak_alloc = torch.cuda.max_memory_allocated() / 2**30
    peak_reserved = torch.cuda.max_memory_reserved() / 2**30
    logger.info(
        f"[gpu-mem] {stage}: allocated={alloc:.2f}GiB reserved={reserved:.2f}GiB "
        f"peak_allocated={peak_alloc:.2f}GiB peak_reserved={peak_reserved:.2f}GiB"
    )


class ModelManager:
    def __init__(
        self,
        mode: str,
        device: str,
        half: bool,
        compile: bool,
        llama_checkpoint_path: str,
        decoder_checkpoint_path: str,
        decoder_config_name: str,
        speed_method: SpeedMethod = "librosa",
        llama_max_seq_len: int | None = None,
    ) -> None:

        self.mode = mode
        self.device = device
        self.half = half
        self.compile = compile
        self.llama_max_seq_len = llama_max_seq_len

        self.precision = torch.half if half else torch.bfloat16

        # Check if MPS or CUDA is available
        if torch.backends.mps.is_available():
            self.device = "mps"
            logger.info("mps is available, running on mps.")
        elif not torch.cuda.is_available():
            self.device = "cpu"
            logger.info("CUDA is not available, running on CPU.")

        # Load the TTS models
        log_gpu_memory("before model load")
        self.load_llama_model(
            llama_checkpoint_path, self.device, self.precision, self.compile, self.mode
        )
        log_gpu_memory("after LLAMA load/cache")
        self.load_decoder_model(
            decoder_config_name, decoder_checkpoint_path, self.device
        )
        log_gpu_memory("after DAC load")
        self.tts_inference_engine = TTSInferenceEngine(
            llama_queue=self.llama_queue,
            decoder_model=self.decoder_model,
            precision=self.precision,
            compile=self.compile,
            speed_method=speed_method,
        )

        # Warm up the models
        if self.mode == "tts":
            self.warm_up(self.tts_inference_engine)
        log_gpu_memory("after warmup")

    def load_llama_model(
        self, checkpoint_path, device, precision, compile, mode
    ) -> None:

        if mode == "tts":
            self.llama_queue = launch_thread_safe_queue(
                checkpoint_path=checkpoint_path,
                device=device,
                precision=precision,
                compile=compile,
                max_seq_len=self.llama_max_seq_len,
            )
        else:
            raise ValueError(f"Invalid mode: {mode}")

        logger.info("LLAMA model loaded.")

    def load_decoder_model(self, config_name, checkpoint_path, device) -> None:
        self.decoder_model = load_decoder_model(
            config_name=config_name,
            checkpoint_path=checkpoint_path,
            device=device,
        )
        logger.info("Decoder model loaded.")

    def warm_up(self, tts_inference_engine) -> None:
        request = ServeTTSRequest(
            text="Hello world.",
            references=[],
            reference_id=None,
            max_new_tokens=1024,
            chunk_length=200,
            top_p=0.7,
            repetition_penalty=1.2,
            temperature=0.7,
            format="wav",
        )
        list(inference(request, tts_inference_engine))
        logger.info("Models warmed up.")
