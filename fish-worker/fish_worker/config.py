from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from fish_worker.priority import PRIORITIES, Priority


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_optional_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None:
        return default
    if value == "":
        return None
    return int(value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value not in {"0", "false", "False", "no", "NO"}


def normalized_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    omp_threads = env.get("OMP_NUM_THREADS", "")
    if not omp_threads.isdecimal() or int(omp_threads) < 1:
        env["OMP_NUM_THREADS"] = "1"
    env.setdefault("FISH_API_SERVER_ACCESS_LOG", "0")
    return env


def priority_queue_limits(v3_default: int) -> dict[Priority, int]:
    v3_limit = max(0, env_int("API_SERVER_MAX_QUEUED_REQUESTS_V3", v3_default))
    limits: dict[Priority, int] = {}
    for priority in PRIORITIES:
        env_name = f"API_SERVER_MAX_QUEUED_REQUESTS_{priority.value.upper()}"
        fallback = v3_limit
        if priority is Priority.V3:
            fallback = v3_limit
        limits[priority] = max(0, env_int(env_name, fallback))
    return limits


@dataclass(frozen=True)
class Config:
    manager_url: str
    worker_token: str
    worker_id: str
    model_id: str
    model_dir: Path
    cache_dir: Path
    api_server_host: str
    api_server_port: int
    api_server_url_override: str | None
    api_server_max_running_requests: int
    api_server_max_queued_requests: int
    api_server_max_queued_requests_by_priority: dict[Priority, int]
    api_server_tts_max_new_tokens: int | None
    api_server_llama_max_seq_len: int | None
    api_server_decoder_checkpoint_path: Path
    api_server_decoder_config_name: str
    api_server_decoder_dtype: str
    api_server_compile: bool
    api_server_half: bool
    api_server_workers: int
    api_server_max_text_length: int
    api_server_speed_method: str
    api_server_references_dir: Path
    heartbeat_interval_seconds: float
    manage_api_server: bool
    require_gpu: bool
    cuda_probe_interval_seconds: float
    cuda_probe_timeout_seconds: float
    gpu_watchdog_failures_before_exit: int
    api_server_watchdog_failures_before_restart: int
    api_server_restart_cooldown_seconds: float

    @property
    def api_server_url(self) -> str:
        if self.api_server_url_override:
            return self.api_server_url_override.rstrip("/")
        host = "127.0.0.1" if self.api_server_host in {"0.0.0.0", "::"} else self.api_server_host
        return f"http://{host}:{self.api_server_port}"

    @classmethod
    def from_env(cls) -> "Config":
        manager_url = os.environ["MANAGER_URL"]
        worker_token = os.environ["WORKER_TOKEN"]
        worker_id = os.getenv("WORKER_ID") or f"worker-{uuid.uuid4().hex[:12]}"
        v3_queue_limit = max(0, env_int("API_SERVER_MAX_QUEUED_REQUESTS", 1))
        api_server_speed_method = os.getenv(
            "API_SERVER_SPEED_METHOD", "librosa"
        ).strip()
        if api_server_speed_method not in {"librosa", "linear"}:
            raise ValueError(
                "API_SERVER_SPEED_METHOD must be either 'librosa' or 'linear'"
            )
        api_server_decoder_dtype = os.getenv(
            "API_SERVER_DECODER_DTYPE", "float32"
        ).strip().lower()
        if api_server_decoder_dtype not in {"float32", "bfloat16"}:
            raise ValueError(
                "API_SERVER_DECODER_DTYPE must be either 'float32' or 'bfloat16'"
            )

        return cls(
            manager_url=manager_url,
            worker_token=worker_token,
            worker_id=worker_id,
            model_id=os.getenv("MODEL_ID", "fishaudio/s2-pro"),
            model_dir=Path(os.getenv("MODEL_DIR", "/models/s2-pro")),
            cache_dir=Path(os.getenv("CACHE_DIR", "/cache")),
            api_server_host=os.getenv("API_SERVER_HOST", "127.0.0.1"),
            api_server_port=env_int("API_SERVER_PORT", 8000),
            api_server_url_override=os.getenv("API_SERVER_URL") or None,
            api_server_max_running_requests=max(1, env_int("API_SERVER_MAX_RUNNING_REQUESTS", 1)),
            api_server_max_queued_requests=v3_queue_limit,
            api_server_max_queued_requests_by_priority=priority_queue_limits(v3_queue_limit),
            api_server_tts_max_new_tokens=env_optional_int(
                "API_SERVER_TTS_MAX_NEW_TOKENS", 4096
            ),
            api_server_llama_max_seq_len=env_optional_int(
                "API_SERVER_LLAMA_MAX_SEQ_LEN", 8192
            ),
            api_server_decoder_checkpoint_path=Path(
                os.getenv("API_SERVER_DECODER_CHECKPOINT_PATH")
                or os.getenv("DECODER_CHECKPOINT_PATH", "/models/s2-pro/codec.pth")
            ),
            api_server_decoder_config_name=os.getenv("API_SERVER_DECODER_CONFIG_NAME", "modded_dac_vq"),
            api_server_decoder_dtype=api_server_decoder_dtype,
            api_server_compile=env_bool("API_SERVER_COMPILE", True),
            api_server_half=env_bool("API_SERVER_HALF", False),
            api_server_workers=max(1, env_int("API_SERVER_WORKERS", 1)),
            api_server_max_text_length=max(0, env_int("API_SERVER_MAX_TEXT_LENGTH", 0)),
            api_server_speed_method=api_server_speed_method,
            api_server_references_dir=Path(os.getenv("API_SERVER_REFERENCES_DIR", "references")),
            heartbeat_interval_seconds=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "5")),
            manage_api_server=env_bool("WORKER_MANAGE_API_SERVER", True),
            require_gpu=env_bool("WORKER_REQUIRE_GPU", True),
            cuda_probe_interval_seconds=float(os.getenv("CUDA_PROBE_INTERVAL_SECONDS", "30")),
            cuda_probe_timeout_seconds=float(os.getenv("CUDA_PROBE_TIMEOUT_SECONDS", "10")),
            gpu_watchdog_failures_before_exit=max(1, env_int("GPU_WATCHDOG_FAILURES_BEFORE_EXIT", 2)),
            api_server_watchdog_failures_before_restart=max(
                1, env_int("API_SERVER_WATCHDOG_FAILURES_BEFORE_RESTART", 3)
            ),
            api_server_restart_cooldown_seconds=float(os.getenv("API_SERVER_RESTART_COOLDOWN_SECONDS", "30")),
        )
