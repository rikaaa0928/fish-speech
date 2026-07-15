from __future__ import annotations

import asyncio
import contextlib
import signal

from fish_worker.config import Config
from fish_worker.worker import Worker


def install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop_event.set)


async def async_main() -> None:
    config = Config.from_env()
    worker = Worker(config)
    install_signal_handlers(worker)
    try:
        await worker.run()
    finally:
        if worker.api_server_process and worker.api_server_process.returncode is None:
            worker.api_server_process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(worker.api_server_process.wait(), timeout=20)


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
