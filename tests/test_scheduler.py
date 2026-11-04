import time
import unittest

from copilot_cost_management.scheduler import DailySnapshotScheduler


class DailySnapshotSchedulerTests(unittest.TestCase):
    def test_schedule_captures_immediately_and_can_be_stopped(self):
        scheduler = DailySnapshotScheduler()

        async def capture():
            return {
                "status": "completed",
                "snapshot_id": "snapshot-1",
                "completed_at": "2026-10-28T00:00:00+00:00",
            }

        scheduler.start(capture)
        deadline = time.monotonic() + 2
        while scheduler.status()["last_snapshot_id"] is None and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(scheduler.status()["last_snapshot_id"], "snapshot-1")
        self.assertEqual(scheduler.status()["last_completed_at"], "2026-10-28T00:00:00+00:00")
        scheduler.stop()
        scheduler._thread.join(timeout=1)
        self.assertEqual(scheduler.status()["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
