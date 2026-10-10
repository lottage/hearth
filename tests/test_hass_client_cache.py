"""Offline tests for the short-lived state-list cache in hass_client (one voice order used to read all states four times)."""

import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from hearth import hass_client  # noqa: E402

STATES = [{"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}, "last_updated": "t"},
          {"entity_id": "switch.tv", "state": "off", "attributes": {"friendly_name": "TV Plug"}, "last_updated": "t"},
          {"entity_id": "light.lamp", "state": "off", "attributes": {"friendly_name": "Lamp"}, "last_updated": "t"}]


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestStatesCache(unittest.TestCase):
    def setUp(self):
        self.client = hass_client.HomeAssistantClient("http://ha", "tok")
        self.reads = []
        self.now = 100.0
        self.data = json.dumps(STATES).encode()

    def fake_urlopen(self, req, timeout=0):
        url = req.full_url
        self.reads.append(url)
        if "/api/services/" in url:
            return Resp(b"[]")
        return Resp(self.data)

    def run_with(self, fn):
        with mock.patch.object(hass_client.urllib.request, "urlopen", self.fake_urlopen), \
                mock.patch.object(hass_client.time, "monotonic", lambda: self.now):
            return fn()

    def state_reads(self):
        return [u for u in self.reads if u.endswith("/api/states")]

    def test_the_three_reads_of_one_order_are_one_fetch(self):
        def order():
            return [self.client.get_states(d)["count"] for d in ("light", "switch", "fan", "light")]
        self.assertEqual(self.run_with(order), [2, 1, 0, 2])          # each domain still filters correctly
        self.assertEqual(len(self.state_reads()), 1)

    def test_an_old_read_is_fetched_again(self):
        def two_reads():
            self.client.get_states("light")
            self.now += hass_client.STATES_TTL_S + 0.1
            return self.client.get_states("light")
        self.run_with(two_reads)
        self.assertEqual(len(self.state_reads()), 2)

    def test_a_service_call_drops_the_cache(self):
        def flow():
            self.client.get_states("light")
            self.client.call_service("light", "turn_off", {"entity_id": "light.lamp"})
            return self.client.get_states("light")
        self.run_with(flow)
        self.assertEqual(len(self.state_reads()), 2)                   # the read after the command is fresh, not cached

    def test_a_failed_read_is_not_cached(self):
        calls = []

        def flaky(req, timeout=0):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("down")
            return Resp(self.data)
        with mock.patch.object(hass_client.urllib.request, "urlopen", flaky), \
                mock.patch.object(hass_client.time, "monotonic", lambda: self.now):
            first = self.client.get_states("light")
            second = self.client.get_states("light")
        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(calls), 2)

    def test_no_token_reads_nothing(self):
        r = hass_client.HomeAssistantClient("http://ha", "").get_states("light")
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
