import json
import time
import unittest
from types import SimpleNamespace

from custody_runtime import CustodyRuntime


class ManualScanCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_refresh_does_not_bypass_the_shared_scan_schedule(self):
        service = object.__new__(CustodyRuntime)
        service.uid = lambda request: 123
        service.admit = lambda uid: None
        service.discovery = SimpleNamespace(
            cooldown_remaining=lambda: 0,
            scan_cooldown_remaining=lambda: 0,
            status_report=lambda: {"retry_after_seconds": 0},
        )
        service.discovery_started = time.time() - 300
        service.discovery_due = time.time() + 240
        service.discovery_task = None
        service.poll_discovery = lambda: None

        response = await service.scan_candidates({}, None)
        result = json.loads(response.text)

        self.assertGreater(service.discovery_due, time.time())
        self.assertEqual(result["scan_state"], "current")
        self.assertFalse(result["scan_running"])
        self.assertIn("next scan is scheduled", result["message"])
        self.assertIn("No extra provider request", result["message"])

    async def test_manual_refresh_reports_running_scan_for_status_polling(self):
        class RunningTask:
            def done(self):
                return False

        service = object.__new__(CustodyRuntime)
        service.uid = lambda request: 123
        service.admit = lambda uid: None
        service.discovery = SimpleNamespace(
            cooldown_remaining=lambda: 0,
            scan_cooldown_remaining=lambda: 0,
            status_report=lambda: {"entry_paused": True, "message": "Candidate checks expired."},
        )
        service.discovery_due = time.time() + 240
        service.discovery_task = RunningTask()
        service.poll_discovery = lambda: None

        response = await service.scan_candidates({}, None)
        result = json.loads(response.text)

        self.assertTrue(result["scan_running"])
        self.assertEqual(result["scan_state"], "running")
        self.assertIn("running", result["message"])
        self.assertIn("No trade", result["message"])
