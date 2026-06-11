#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_SOURCE_TEXT = Path(__file__).resolve().parents[1] / "鲁迅作品选_清理版.txt"
DEFAULT_AUDIO_DIR = Path(__file__).resolve().parent / "run" / "audio"
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "run"
DEFAULT_REMOTE_REPO = "/root/src/fish-speech"
DEFAULT_REMOTE_REF = "rev-agent"


@dataclass(frozen=True)
class Sample:
    name: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class TtsResult:
    ok: bool
    sample: str
    chars: int
    status_code: int | None
    latency_s: float
    audio_bytes: int
    audio_duration_s: float | None
    audio_path: str | None
    error: str | None

    @property
    def chars_per_s(self) -> float:
        return self.chars / self.latency_s if self.latency_s > 0 else 0.0

    @property
    def audio_realtime_factor(self) -> float | None:
        if self.audio_duration_s is None or self.audio_duration_s <= 0:
            return None
        return self.latency_s / self.audio_duration_s


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def first_api_key() -> str | None:
    if key := os.getenv("OPENAI_API_KEY"):
        return key
    keys = os.getenv("OPENAI_API_KEYS", "")
    return next((key.strip() for key in keys.split(",") if key.strip()), None)


def worker_ws_url_from_base(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/internal/workers/ws", "", "", ""))


def resolve_config(args: argparse.Namespace) -> None:
    args.base_url = (
        args.base_url or os.getenv("FISH_MANAGER_BASE_URL") or DEFAULT_BASE_URL
    ).rstrip("/")
    args.voice_id = (
        args.voice_id
        or os.getenv("FISH_MANAGER_BENCH_VOICE_ID")
        or os.getenv("FISH_MANAGER_CHECK_VOICE_ID")
        or "leijun"
    )
    args.ssh_host = args.ssh_host or os.getenv("FISH_MANAGER_BENCH_SSH_HOST")
    args.ssh_port = int(
        args.ssh_port or os.getenv("FISH_MANAGER_BENCH_SSH_PORT") or "22"
    )
    args.ssh_key = args.ssh_key or os.getenv("FISH_MANAGER_BENCH_SSH_KEY")
    args.remote_repo = (
        args.remote_repo or os.getenv("FISH_MANAGER_BENCH_REMOTE_REPO") or DEFAULT_REMOTE_REPO
    )
    args.remote_ref = (
        args.remote_ref or os.getenv("FISH_MANAGER_BENCH_REMOTE_REF") or DEFAULT_REMOTE_REF
    )
    args.manager_ws_url = (
        args.manager_ws_url
        or os.getenv("FISH_MANAGER_WORKER_WS_URL")
        or os.getenv("MANAGER_URL")
        or worker_ws_url_from_base(args.base_url)
    )
    args.worker_token = args.worker_token or os.getenv("WORKER_TOKEN", "")


def normalize_text(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    return value.strip()


def load_samples(path: Path, targets: list[int]) -> list[Sample]:
    lines = path.read_text(encoding="utf-8").splitlines()
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = normalize_text(line)
        if not stripped:
            if current:
                paragraphs.append("".join(current))
                current = []
            continue
        if len(stripped) <= 8 and not re.search(r"[，。！？；]", stripped):
            if current:
                paragraphs.append("".join(current))
                current = []
            continue
        current.append(stripped)
    if current:
        paragraphs.append("".join(current))

    corpus = "".join(paragraphs)
    samples: list[Sample] = []
    offset = 0
    for target in targets:
        if not corpus:
            raise ValueError("source text is empty")
        if offset + target > len(corpus):
            offset = 0
        end = min(len(corpus), offset + target)
        cut = corpus[offset:end]
        if end < len(corpus):
            punctuation = max(cut.rfind(mark) for mark in "。！？；")
            if punctuation >= max(12, target // 2):
                cut = cut[: punctuation + 1]
        samples.append(Sample(name=f"chars-{len(cut)}", text=cut))
        offset = min(len(corpus), offset + max(target, len(cut)))
    return samples


def wav_duration(path: Path) -> float | None:
    with contextlib.suppress(Exception):
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if rate > 0:
                return frames / rate
    return None


def audio_extension(content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "audio/wav": "wav",
        "audio/wave": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/flac": "flac",
        "audio/ogg": "ogg",
    }.get(content_type, "bin")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, round((len(values) - 1) * p)))
    return values[index]


def summarize(results: list[TtsResult]) -> dict[str, Any]:
    ok = [result for result in results if result.ok]
    latencies = [result.latency_s for result in ok]
    chars_per_s = [result.chars_per_s for result in ok]
    rtfs = [value for result in ok if (value := result.audio_realtime_factor) is not None]
    return {
        "requests": len(results),
        "ok": len(ok),
        "failed": len(results) - len(ok),
        "total_chars": sum(result.chars for result in ok),
        "total_latency_s": sum(latencies),
        "avg_latency_s": statistics.mean(latencies) if latencies else None,
        "p50_latency_s": percentile(latencies, 0.50),
        "p90_latency_s": percentile(latencies, 0.90),
        "avg_chars_per_s": statistics.mean(chars_per_s) if chars_per_s else None,
        "avg_audio_rtf": statistics.mean(rtfs) if rtfs else None,
    }


async def wait_for_worker(base_url: str, api_key: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    headers = {"Authorization": f"Bearer {api_key}"}
    last_payload: dict[str, Any] = {}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get("/v1/workers")
                response.raise_for_status()
                payload = response.json()
                last_payload = payload
                workers = payload.get("data") or []
                ready = [worker for worker in workers if worker.get("ready") and worker.get("sglang_healthy")]
                if ready:
                    return ready[0]
            except Exception as exc:  # noqa: BLE001
                last_payload = {"error": str(exc)}
            await asyncio.sleep(5)
    raise TimeoutError(f"worker did not become ready in {timeout_s}s: {last_payload}")


async def call_tts(
    client: httpx.AsyncClient,
    sample: Sample,
    voice_id: str,
    audio_dir: Path | None,
    max_new_tokens: int | None,
    run_label: str,
) -> TtsResult:
    payload: dict[str, Any] = {
        "input": sample.text,
        "voice_id": voice_id,
        "response_format": "wav",
        "stream": False,
    }
    if max_new_tokens is not None:
        payload["max_new_tokens"] = max_new_tokens

    started = time.monotonic()
    try:
        response = await client.post("/v1/audio/speech", json=payload)
        latency_s = time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return TtsResult(False, sample.name, sample.chars, None, time.monotonic() - started, 0, None, None, str(exc))

    if response.status_code != 200:
        return TtsResult(
            False,
            sample.name,
            sample.chars,
            response.status_code,
            latency_s,
            len(response.content),
            None,
            None,
            response.text[:1000],
        )

    audio_path: Path | None = None
    duration_s: float | None = None
    if audio_dir is not None:
        audio_dir.mkdir(parents=True, exist_ok=True)
        ext = audio_extension(response.headers.get("content-type", ""))
        filename = f"{int(time.time())}-{run_label}-{sample.name}.{ext}"
        audio_path = audio_dir / filename
        audio_path.write_bytes(response.content)
        duration_s = wav_duration(audio_path)

    return TtsResult(
        True,
        sample.name,
        sample.chars,
        response.status_code,
        latency_s,
        len(response.content),
        duration_s,
        str(audio_path) if audio_path else None,
        None,
    )


async def run_bench(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(Path(args.env_file))
    resolve_config(args)
    api_key = args.api_key or first_api_key()
    if not api_key:
        raise SystemExit("missing API key; set OPENAI_API_KEY or pass --api-key")

    targets = [int(part) for part in args.targets.split(",") if part.strip()]
    samples = load_samples(Path(args.source_text), targets)
    requests = [sample for _ in range(args.repeat) for sample in samples]
    audio_dir = None if args.no_audio else Path(args.audio_dir)
    results_path = Path(args.results_path) if args.results_path else None
    if results_path:
        results_path.parent.mkdir(parents=True, exist_ok=True)

    headers = {"Authorization": f"Bearer {api_key}"}
    limits = httpx.Limits(max_connections=max(args.concurrency, 1), max_keepalive_connections=max(args.concurrency, 1))
    timeout = httpx.Timeout(args.timeout, connect=20)
    semaphore = asyncio.Semaphore(args.concurrency)
    results: list[TtsResult] = []

    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=timeout, limits=limits) as client:
        async def one(index: int, sample: Sample) -> TtsResult:
            async with semaphore:
                result = await call_tts(client, sample, args.voice_id, audio_dir, args.max_new_tokens, args.run_label)
                print(json.dumps({"index": index, **result.__dict__, "chars_per_s": result.chars_per_s, "audio_rtf": result.audio_realtime_factor}, ensure_ascii=False), flush=True)
                return result

        tasks = [asyncio.create_task(one(index, sample)) for index, sample in enumerate(requests, start=1)]
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            if results_path:
                with results_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(result.__dict__, ensure_ascii=False) + "\n")

    payload = {
        "config": {
            "base_url": args.base_url,
            "voice_id": args.voice_id,
            "max_new_tokens": args.max_new_tokens,
            "concurrency": args.concurrency,
            "repeat": args.repeat,
            "targets": targets,
        },
        "samples": [{"name": sample.name, "chars": sample.chars, "text": sample.text} for sample in samples],
        "summary": summarize(results),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return payload


def ssh_command(args: argparse.Namespace, remote_command: str) -> list[str]:
    command = [
        "ssh",
        "-p",
        str(args.ssh_port),
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if args.ssh_key:
        command.extend(["-i", str(args.ssh_key)])
    command.extend([args.ssh_host, remote_command])
    return command


def run_ssh(args: argparse.Namespace, remote_command: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ssh_command(args, remote_command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"remote SSH command timed out after {timeout}s") from exc


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def start_remote_worker(args: argparse.Namespace, mem_fraction: str, max_new_tokens: int) -> None:
    env = {
        "MANAGER_URL": args.manager_ws_url,
        "WORKER_TOKEN": args.worker_token,
        "SGLANG_TTS_MEM_FRACTION_STATIC": mem_fraction,
        "SGLANG_TTS_MAX_RUNNING_REQUESTS": "1",
        "SGLANG_MAX_RUNNING_REQUESTS": "1",
        "SGLANG_MAX_QUEUED_REQUESTS": "0",
        "SGLANG_TTS_MAX_NEW_TOKENS": str(max_new_tokens),
        "SGLANG_TTS_TORCH_COMPILE": "0",
        "SGLANG_TTS_CUDA_GRAPH": "0",
    }
    exports = " ".join(f"{key}={shell_quote(value)}" for key, value in env.items())
    remote = (
        "set -e; "
        f"cd {shell_quote(args.remote_repo)}; "
        f"git fetch origin {shell_quote(args.remote_ref)} >/tmp/fish-worker-git-fetch.log 2>&1 || true; "
        f"git checkout {shell_quote(args.remote_ref)} >/tmp/fish-worker-git-checkout.log 2>&1 || true; "
        f"git pull --ff-only origin {shell_quote(args.remote_ref)} >/tmp/fish-worker-git-pull.log 2>&1 || true; "
        "pkill -TERM -f '[u]v run fish-worker' >/dev/null 2>&1 || true; "
        "pkill -TERM -f '/[f]ish-worker/.venv/bin/fish-worker' >/dev/null 2>&1 || true; "
        "pkill -TERM -f '/[f]ish-worker/.venv/bin/python' >/dev/null 2>&1 || true; "
        "pkill -TERM -f '[s]gl-omni' >/dev/null 2>&1 || true; "
        "sleep 5; "
        "pkill -KILL -f '[u]v run fish-worker' >/dev/null 2>&1 || true; "
        "pkill -KILL -f '/[f]ish-worker/.venv/bin/fish-worker' >/dev/null 2>&1 || true; "
        "pkill -KILL -f '/[f]ish-worker/.venv/bin/python' >/dev/null 2>&1 || true; "
        "pkill -KILL -f '[s]gl-omni' >/dev/null 2>&1 || true; "
        "sleep 3; "
        "rm -f fish-worker/.generated/s2pro_tts.yaml; "
        "mkdir -p fish-worker/run; "
        f"nohup env {exports} bash scripts/autodl_start_worker.sh > fish-worker/run/bench-worker.log 2>&1 & "
        "echo $! > fish-worker/run/bench-worker.pid; "
        "sleep 1; "
        "cat fish-worker/run/bench-worker.pid"
    )
    result = run_ssh(args, remote, timeout=args.ssh_start_timeout)
    print(result.stdout, flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"failed to start remote worker: {result.stdout}")


def remote_tail(args: argparse.Namespace, lines: int = 80) -> str:
    result = run_ssh(args, f"cd {shell_quote(args.remote_repo)}; tail -n {lines} fish-worker/run/bench-worker.log 2>/dev/null || true", timeout=60)
    return result.stdout


async def run_tune(args: argparse.Namespace) -> None:
    load_dotenv(Path(args.env_file))
    resolve_config(args)
    api_key = args.api_key or first_api_key()
    if not api_key:
        raise SystemExit("missing API key; set OPENAI_API_KEY or pass --api-key")
    if not args.worker_token:
        raise SystemExit("missing worker token; pass --worker-token or set WORKER_TOKEN")
    if not args.ssh_host:
        raise SystemExit(
            "missing SSH host; pass --ssh-host or set FISH_MANAGER_BENCH_SSH_HOST"
        )

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    candidates = [part.strip() for part in args.mem_fractions.split(",") if part.strip()]
    max_new_tokens_values = [int(part) for part in args.max_new_tokens_values.split(",") if part.strip()]
    all_results: list[dict[str, Any]] = []

    for mem_fraction in candidates:
        for max_new_tokens in max_new_tokens_values:
            label = f"mem{mem_fraction}-tokens{max_new_tokens}".replace(".", "p")
            print(f"starting remote worker {label}", flush=True)
            start_remote_worker(args, mem_fraction, max_new_tokens)
            try:
                worker = await wait_for_worker(args.base_url, api_key, args.ready_timeout)
            except Exception as exc:  # noqa: BLE001
                log_tail = remote_tail(args)
                record = {"label": label, "mem_fraction": mem_fraction, "max_new_tokens": max_new_tokens, "ready": False, "error": str(exc), "worker_log_tail": log_tail}
                all_results.append(record)
                print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
                continue

            bench_args = argparse.Namespace(**vars(args))
            bench_args.max_new_tokens = max_new_tokens
            bench_args.concurrency = 1
            bench_args.repeat = args.tune_repeat
            bench_args.targets = args.tune_targets
            bench_args.run_label = label
            bench_args.audio_dir = str(results_dir / "audio" / label)
            bench_args.results_path = str(results_dir / f"{label}.jsonl")
            bench_args.no_audio = args.no_audio
            print(f"benchmarking {label}", flush=True)
            bench = await run_bench(bench_args)
            record = {
                "label": label,
                "mem_fraction": mem_fraction,
                "max_new_tokens": max_new_tokens,
                "ready": True,
                "worker": worker,
                "bench": bench["summary"],
            }
            all_results.append(record)
            (results_dir / "tune-summary.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(all_results, ensure_ascii=False, indent=2), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark manager TTS through uv.")
    parser.add_argument("command", choices=["bench", "tune-remote"])
    parser.add_argument(
        "--base-url",
        default=None,
        help="manager public base URL; defaults to FISH_MANAGER_BASE_URL or localhost",
    )
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--source-text", default=str(DEFAULT_SOURCE_TEXT))
    parser.add_argument(
        "--voice-id",
        default=None,
        help="stored voice id; defaults to FISH_MANAGER_BENCH_VOICE_ID or leijun",
    )
    parser.add_argument("--targets", default="40,80,160,320,640")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR))
    parser.add_argument("--results-path", default=None)
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--run-label", default="bench")
    parser.add_argument("--no-audio", action="store_true")

    parser.add_argument(
        "--ssh-host",
        default=None,
        help="remote worker SSH host; defaults to FISH_MANAGER_BENCH_SSH_HOST",
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=None,
        help="remote worker SSH port; defaults to FISH_MANAGER_BENCH_SSH_PORT or 22",
    )
    parser.add_argument(
        "--ssh-key",
        default=None,
        help="remote worker SSH key path; defaults to FISH_MANAGER_BENCH_SSH_KEY",
    )
    parser.add_argument("--ssh-start-timeout", type=int, default=600)
    parser.add_argument(
        "--remote-repo",
        default=None,
        help="remote fish-speech repo path; defaults to FISH_MANAGER_BENCH_REMOTE_REPO",
    )
    parser.add_argument(
        "--remote-ref",
        default=None,
        help="remote git ref to checkout; defaults to FISH_MANAGER_BENCH_REMOTE_REF",
    )
    parser.add_argument(
        "--manager-ws-url",
        default=None,
        help="worker WebSocket URL; defaults to FISH_MANAGER_WORKER_WS_URL or MANAGER_URL",
    )
    parser.add_argument("--worker-token", default=None)
    parser.add_argument("--mem-fractions", default="0.45,0.50,0.55,0.60")
    parser.add_argument("--max-new-tokens-values", default="512,768,1024")
    parser.add_argument("--ready-timeout", type=float, default=900)
    parser.add_argument("--tune-repeat", type=int, default=1)
    parser.add_argument("--tune-targets", default="80,240,480")
    return parser


async def async_main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "bench":
        await run_bench(args)
    elif args.command == "tune-remote":
        await run_tune(args)
    else:
        raise AssertionError(args.command)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
