"""A fresh install that configured only Home Assistant makes no voice, ssh or Valkey calls at all (no noise in the log)."""

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from hearth import services  # noqa: E402
from harness.data_fabric import valkey_amem as vmod  # noqa: E402

ENDPOINT_ENV = ("VOICE_URL", "COORDINATOR_URL", "VALKEY_URL", "VALKEY_HOST", "VALKEY_PORT")


class _Env(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in ENDPOINT_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestTtsWarmSkipsWithoutVoice(_Env):
    def test_no_voice_section_means_no_voice_server_calls(self):
        synth = mock.Mock(return_value=b"RIFF")
        with mock.patch.object(services, "config", {"homeassistant": {"url": "http://ha:8123"}}), \
                mock.patch("harness.connectors.voice_connector.voice_connector.synthesize_speech", synth), \
                mock.patch("hearth.reflex.candidates") as cands:
            services._tts_warm()
        synth.assert_not_called()
        cands.assert_not_called()

    def test_with_a_voice_server_the_confirmations_are_made(self):
        with mock.patch.object(services, "config", {"voice": {"stt_url": "http://voice.test:8210"}}), \
                mock.patch("hearth.reflex.candidates", return_value=[]), \
                mock.patch("hearth.tts_cache.warm", return_value=0) as warm,                 mock.patch.object(services, "_tts_cache_dir", return_value="unused"):
            services._tts_warm()
        warm.assert_called_once()


class TestSentrySnapshotSkipsWithoutEngineHost(_Env):
    def test_no_engine_host_means_no_ssh(self):
        with mock.patch.object(services, "config", {"homeassistant": {"url": "http://ha:8123"}}), \
                mock.patch.object(services.subprocess, "run") as run:
            self.assertIsNone(services._sentry_snapshot_bytes("cat_20260101_000000.jpg"))
        run.assert_not_called()

    def test_with_an_engine_host_the_snapshot_is_fetched_over_ssh(self):
        done = types.SimpleNamespace(returncode=0, stdout=b"\xff\xd8jpeg")
        with mock.patch.object(services, "config", {"cluster": {"coordinator_url": "http://engine.test:8001/v1"}}), \
                mock.patch.object(services.subprocess, "run", return_value=done) as run:
            self.assertEqual(services._sentry_snapshot_bytes("cat_20260101_000000.jpg"), b"\xff\xd8jpeg")
        run.assert_called_once()


class TestValkeyOptional(_Env):
    def test_unconfigured_valkey_is_not_contacted_and_said_once(self):
        redis = mock.Mock()
        with mock.patch.object(vmod.fleet_config, "valkey_configured", False), mock.patch.object(vmod, "redis", redis), \
                mock.patch.object(vmod, "_unconfigured_logged", False), mock.patch.object(vmod, "logger") as log:
            first = vmod.ValkeyAMEM()
            second = vmod.ValkeyAMEM()
        redis.Redis.assert_not_called()
        self.assertIsNone(first.r)
        self.assertIsNone(second.r)
        self.assertEqual(log.info.call_count, 1)
        log.warning.assert_not_called()

    def test_configured_valkey_still_connects(self):
        redis = mock.Mock()
        redis.Redis.return_value.ping.return_value = True
        with mock.patch.object(vmod.fleet_config, "valkey_configured", True), mock.patch.object(vmod, "redis", redis):
            amem = vmod.ValkeyAMEM()
        redis.Redis.assert_called_once()
        self.assertIsNotNone(amem.r)


if __name__ == "__main__":
    unittest.main()
