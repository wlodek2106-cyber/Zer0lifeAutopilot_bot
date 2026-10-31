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
            status_report=lambda: {"retry_after_seconds": 0},
        )
        service.discovery_started = time.time() - 300
        service.discovery_due = time.time() + 240
        service.discovery_task = None
        service.poll_discovery = lambda: None

        response = await service.scan_candidates({}, None)
        result = json.loads(response.text)

        self.assertGreater(service.discovery_due, time.time())
        self.assertIn("shared schedule", result["message"])
        self.assertIn("No extra provider request", result["message"])
