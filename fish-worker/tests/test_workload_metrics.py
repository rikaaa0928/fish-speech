from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from fish_worker.priority import PRIORITIES, Priority
from fish_worker.worker import Worker


def workload_worker(max_running: int = 1) -> Worker:
    worker = Worker.__new__(Worker)
    worker.config = SimpleNamespace(
        api_server_max_running_requests=max_running,
        api_server_max_queued_requests=10,
        api_server_max_queued_requests_by_priority={},
    )
    worker.local_inflight = 0
    worker.active_requests = {}
    worker.priority_queues = {priority: [] for priority in PRIORITIES}
    worker.capacity_condition = asyncio.Condition()
    worker.api_server_reject_count = 0
    return worker


class WorkloadMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracks_active_request_counts_and_chars_by_priority(self) -> None:
        worker = workload_worker(max_running=2)

        self.assertTrue(await worker.acquire_request_slot("first", Priority.V2, 24))
        self.assertTrue(await worker.acquire_request_slot("second", Priority.V3, 81))

        self.assertEqual(worker.inflight_by_priority()[Priority.V2], 1)
        self.assertEqual(worker.inflight_by_priority()[Priority.V3], 1)
        self.assertEqual(worker.inflight_chars_by_priority()[Priority.V2], 24)
        self.assertEqual(worker.inflight_chars_by_priority()[Priority.V3], 81)

        await worker.release_request_slot("first")
        await worker.release_request_slot("second")

    async def test_moves_character_workload_from_queue_to_inflight(self) -> None:
        worker = workload_worker()
        self.assertTrue(await worker.acquire_request_slot("active", Priority.V3, 40))

        waiting = asyncio.create_task(
            worker.acquire_request_slot("queued", Priority.V2, 70)
        )
        await asyncio.sleep(0)

        self.assertEqual(worker.queued_by_priority()[Priority.V2], 1)
        self.assertEqual(worker.queued_chars_by_priority()[Priority.V2], 70)

        await worker.release_request_slot("active")
        self.assertTrue(await waiting)
        self.assertEqual(worker.queued_chars_by_priority()[Priority.V2], 0)
        self.assertEqual(worker.inflight_chars_by_priority()[Priority.V2], 70)

        await worker.release_request_slot("queued")


if __name__ == "__main__":
    unittest.main()
