#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO = Path(os.getenv("FISH_REPO", "/root/src/fish-speech"))
WORKER_VENV = REPO / "fish-worker" / ".venv"
PYTHON = WORKER_VENV / "bin" / "python"
SGL_OMNI = WORKER_VENV / "bin" / "sgl-omni"
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/autodl-fs/data/models/s2-pro"))
RUN_DIR = Path(os.getenv("TTS_BENCH_RUN_DIR", "/root/autodl-tmp/tts_direct_bench"))
REF_DIR = RUN_DIR / "refs"
AUDIO_DIR = RUN_DIR / "audio"
LOG_DIR = RUN_DIR / "logs"
CONFIG_DIR = RUN_DIR / "configs"
RESULTS = RUN_DIR / "results.jsonl"
DEFAULT_MANAGER_BASE_URL = "https://manager.example.com"


@dataclass(frozen=True)
class Sample:
    name: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Result:
    service: str
    label: str
    sample: str
    chars: int
    ok: bool
    status_code: int | None
    latency_s: float
    audio_bytes: int
    audio_duration_s: float | None
    chars_per_s: float | None
    audio_path: str | None
    error: str | None
    extra: dict[str, Any]


class ManagedProcess:
    def __init__(self, cmd: list[str], log_path: Path, cwd: Path, env: dict[str, str]) -> None:
        self.cmd = cmd
        self.log_path = log_path
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_file = None

    def __enter__(self) -> "ManagedProcess":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print("started", " ".join(self.cmd), "pid", self.proc.pid, "log", self.log_path, flush=True)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=30)
        if self.log_file:
            self.log_file.close()


def request_json(url: str, payload: dict[str, Any], timeout: float | None = None) -> tuple[int | None, bytes, dict[str, str]]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, error.read(), {k.lower(): v for k, v in error.headers.items()}
    except Exception as error:
        return None, repr(error).encode("utf-8"), {}


def http_get_ok(url: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def wait_ready(urls: list[str], proc: ManagedProcess, timeout_s: int) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.proc and proc.proc.poll() is not None:
            raise RuntimeError(f"process exited early with {proc.proc.returncode}; log={proc.log_path}")
        if any(http_get_ok(url, timeout=5) for url in urls):
            return
        time.sleep(3)
    raise TimeoutError(f"service did not become ready in {timeout_s}s; log={proc.log_path}")


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wav_duration(path: Path) -> float | None:
    with contextlib.suppress(Exception):
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.getnframes()
            if rate > 0:
                return frames / rate
    return None


def normalize_text(text: str) -> str:
    return "".join(text.split())


def load_corpus() -> str:
    candidates = sorted((REPO / "fish-manager").glob("*.txt"))
    for path in candidates:
        text = normalize_text(path.read_text(encoding="utf-8", errors="ignore"))
        if len(text) > 1000:
            return text
    return ("This is a fallback benchmark sentence for text to speech generation. " * 200).strip()


def build_samples(lengths: list[int]) -> list[Sample]:
    corpus = load_corpus()
    samples: list[Sample] = []
    offset = 0
    for target in lengths:
        if offset + target > len(corpus):
            offset = 0
        text = corpus[offset : offset + target]
        offset += target
        samples.append(Sample(f"chars-{len(text)}", text))
    return samples


def fetch_reference(args: argparse.Namespace) -> tuple[Path, str | None]:
    if not args.worker_token:
        raise ValueError("--worker-token or WORKER_TOKEN is required to fetch stored voice audio")

    REF_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = REF_DIR / f"{args.voice_id}.wav"
    meta_path = REF_DIR / f"{args.voice_id}.json"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        checksum = None
        with contextlib.suppress(Exception):
            checksum = json.loads(meta_path.read_text()).get("checksum")
        return audio_path, checksum

    url = f"{args.manager_base_url.rstrip('/')}/internal/voices/{args.voice_id}/audio"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {args.worker_token}"})
    with urllib.request.urlopen(req, timeout=60) as response:
        body = response.read()
        checksum = response.headers.get("x-voice-checksum")
        audio_path.write_bytes(body)
        meta_path.write_text(json.dumps({"checksum": checksum, "bytes": len(body)}, indent=2), encoding="utf-8")
        return audio_path, checksum


def base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{WORKER_VENV / 'bin'}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"/root/src/sglang-omni:{REPO}:{env.get('PYTHONPATH', '')}"
    env.setdefault("OMP_NUM_THREADS", "1")
    return env


def write_sglang_config(label: str, compile_on: bool, cuda_graph_on: bool, mem_fraction: float, server_max_new_tokens: int) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / f"sglang-{label}.yaml"
    text = f"""config_cls: S2ProPipelineConfig
model_path: {MODEL_DIR}
relay_backend: shm
runtime_overrides:
  tts_engine:
    max_new_tokens: {server_max_new_tokens}
    server_args_overrides:
      mem_fraction_static: {mem_fraction:.2f}
      max_running_requests: 1
      enable_torch_compile: {str(compile_on).lower()}
      disable_cuda_graph: {str(not cuda_graph_on).lower()}
"""
    path.write_text(text, encoding="utf-8")
    return path


def save_result(result: Result) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as output:
        output.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
    print(json.dumps(asdict(result), ensure_ascii=False), flush=True)


def call_sglang(
    base_url: str,
    label: str,
    sample: Sample,
    ref_audio: Path,
    ref_text: str,
    max_new_tokens: int,
    request_timeout: float | None,
) -> Result:
    payload = {
        "input": sample.text,
        "voice": "leijun",
        "response_format": "wav",
        "stream": False,
        "max_new_tokens": max_new_tokens,
        "temperature": 0.8,
        "top_p": 0.8,
        "top_k": 30,
        "repetition_penalty": 1.1,
        "references": [{"audio_path": str(ref_audio), "text": ref_text}],
    }
    started = time.monotonic()
    status, body, headers = request_json(f"{base_url}/v1/audio/speech", payload, timeout=request_timeout)
    latency = time.monotonic() - started
    return build_result("sglang", label, sample, status, body, headers, latency)


def call_api(
    base_url: str,
    label: str,
    sample: Sample,
    ref_audio_b64: str,
    ref_text: str,
    max_new_tokens: int,
    request_timeout: float | None,
) -> Result:
    payload = {
        "text": sample.text,
        "format": "wav",
        "streaming": False,
        "max_new_tokens": max_new_tokens,
        "chunk_length": 300,
        "top_p": 0.8,
        "repetition_penalty": 1.1,
        "temperature": 0.8,
        "references": [{"audio": ref_audio_b64, "text": ref_text}],
    }
    started = time.monotonic()
    status, body, headers = request_json(f"{base_url}/v1/tts", payload, timeout=request_timeout)
    latency = time.monotonic() - started
    return build_result("api", label, sample, status, body, headers, latency)


def build_result(service: str, label: str, sample: Sample, status: int | None, body: bytes, headers: dict[str, str], latency: float) -> Result:
    ok = status == 200 and body.startswith(b"RIFF")
    audio_path: Path | None = None
    duration: float | None = None
    error: str | None = None
    if ok:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        audio_path = AUDIO_DIR / f"{service}-{label}-{sample.name}-{int(time.time() * 1000)}.wav"
        audio_path.write_bytes(body)
        duration = wav_duration(audio_path)
    else:
        error = body[:1000].decode("utf-8", errors="replace")
    return Result(
        service=service,
        label=label,
        sample=sample.name,
        chars=sample.chars,
        ok=ok,
        status_code=status,
        latency_s=latency,
        audio_bytes=len(body),
        audio_duration_s=duration,
        chars_per_s=(sample.chars / latency if latency > 0 else None),
        audio_path=str(audio_path) if audio_path else None,
        error=error,
        extra={"content_type": headers.get("content-type")},
    )


def run_sglang(args: argparse.Namespace, label: str, compile_on: bool, cuda_graph_on: bool) -> None:
    ref_audio, checksum = fetch_reference(args)
    samples = build_samples(args.lengths)
    config = write_sglang_config(label, compile_on, cuda_graph_on, args.mem_fraction, args.server_max_new_tokens)
    log_path = LOG_DIR / f"sglang-{label}.log"
    port = find_free_port(args.host) if args.port == 0 else args.port
    cmd = [
        str(SGL_OMNI),
        "serve",
        "--config",
        str(config),
        "--host",
        args.host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    with ManagedProcess(cmd, log_path, REPO, base_env()) as proc:
        wait_ready([f"http://{args.host}:{port}/health", f"http://{args.host}:{port}/v1/models"], proc, args.startup_timeout)
        print("ready", label, "port", port, "checksum", checksum, flush=True)
        for _ in range(args.repeats):
            for sample in samples:
                result = call_sglang(
                    f"http://{args.host}:{port}",
                    label,
                    sample,
                    ref_audio,
                    args.ref_text,
                    args.request_max_new_tokens,
                    args.request_timeout,
                )
                save_result(result)
                if args.stop_on_fail and not result.ok:
                    return


def run_api(args: argparse.Namespace) -> None:
    ref_audio, checksum = fetch_reference(args)
    ref_audio_b64 = base64.b64encode(ref_audio.read_bytes()).decode("ascii")
    samples = build_samples(args.lengths)
    label = "compile"
    log_path = LOG_DIR / "api-compile.log"
    cmd = [
        str(PYTHON),
        "tools/api_server.py",
        "--listen",
        f"{args.host}:{args.api_port}",
        "--llama-checkpoint-path",
        str(MODEL_DIR),
        "--decoder-checkpoint-path",
        str(MODEL_DIR / "codec.pth"),
        "--decoder-config-name",
        "modded_dac_vq",
        "--compile",
        "--max-text-length",
        "0",
        "--workers",
        "1",
    ]
    if args.api_half:
        cmd.append("--half")
    with ManagedProcess(cmd, log_path, REPO, base_env()) as proc:
        wait_ready([f"http://{args.host}:{args.api_port}/v1/health"], proc, args.startup_timeout)
        print("ready", label, "checksum", checksum, flush=True)
        for _ in range(args.repeats):
            for sample in samples:
                result = call_api(
                    f"http://{args.host}:{args.api_port}",
                    label,
                    sample,
                    ref_audio_b64,
                    args.ref_text,
                    args.request_max_new_tokens,
                    args.request_timeout,
                )
                save_result(result)
                if args.stop_on_fail and not result.ok:
                    return


def summarize_results() -> None:
    if not RESULTS.exists():
        print("no results", file=sys.stderr)
        return
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        groups.setdefault((row["service"], row["label"]), []).append(row)
    for (service, label), rows in sorted(groups.items()):
        ok = [row for row in rows if row["ok"]]
        print(f"\n{service} {label}: {len(ok)}/{len(rows)} ok")
        if not ok:
            for row in rows[-3:]:
                print("  fail", row["sample"], row["status_code"], row["error"])
            continue
        avg_cps = sum(row["chars_per_s"] for row in ok if row["chars_per_s"] is not None) / len(ok)
        print(f"  avg chars/s: {avg_cps:.2f}")
        prev: dict[str, Any] | None = None
        for row in sorted(ok, key=lambda item: item["chars"]):
            dur = row["audio_duration_s"]
            growth = None if prev is None or dur is None or prev["audio_duration_s"] is None else dur - prev["audio_duration_s"]
            print(
                f"  {row['sample']}: latency={row['latency_s']:.2f}s duration={dur} growth={growth} cps={row['chars_per_s']:.2f}"
            )
            prev = row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["sglang", "sglang-matrix", "api", "summary"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-port", type=int, default=8080)
    parser.add_argument("--startup-timeout", type=int, default=900)
    parser.add_argument("--lengths", type=lambda value: [int(part) for part in value.split(",") if part], default=[80, 160, 320])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--manager-base-url", default=os.getenv("FISH_MANAGER_BASE_URL", DEFAULT_MANAGER_BASE_URL))
    parser.add_argument("--worker-token", default=os.getenv("WORKER_TOKEN"))
    parser.add_argument("--voice-id", default="leijun")
    parser.add_argument("--ref-text", default="")
    parser.add_argument("--mem-fraction", type=float, default=0.50)
    parser.add_argument("--server-max-new-tokens", type=int, default=2048)
    parser.add_argument("--request-max-new-tokens", type=int, default=2048)
    parser.add_argument("--request-timeout", type=float, default=600)
    parser.add_argument("--stop-on-fail", action="store_true")
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--cuda-graph", action="store_true")
    parser.add_argument("--label", default=None)
    parser.add_argument("--api-half", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if args.command == "summary":
        summarize_results()
        return
    if args.command == "api":
        run_api(args)
        return
    if args.command == "sglang":
        label = args.label or f"compile{int(args.compile)}-cudagraph{int(args.cuda_graph)}"
        run_sglang(args, label, args.compile, args.cuda_graph)
        return
    for label, compile_on, cuda_graph_on in [
        ("none", False, False),
        ("compile", True, False),
        ("cudagraph", False, True),
        ("compile-cudagraph", True, True),
    ]:
        try:
            run_sglang(args, label, compile_on, cuda_graph_on)
        except Exception as exc:
            save_result(
                Result(
                    service="sglang",
                    label=label,
                    sample="startup",
                    chars=0,
                    ok=False,
                    status_code=None,
                    latency_s=0.0,
                    audio_bytes=0,
                    audio_duration_s=None,
                    chars_per_s=None,
                    audio_path=None,
                    error=repr(exc),
                    extra={},
                )
            )


if __name__ == "__main__":
    main()
