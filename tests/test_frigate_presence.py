"""Offline tests for Frigate presence: identity mapping, in-view vs ended events, filtering, and the merge
with the presence hub. Event dicts mirror real Frigate 0.18 payloads (REST /api/events and ws 'after')."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from hearth.frigate_presence import FrigatePresence, merge_locations  # noqa: E402
from stonesage.courage.prompt import build_presence_card  # noqa: E402
from household_fixture import TEST_HOUSEHOLD  # noqa: E402

NOW = 1790276200.0


def ev(eid, label, start, end=None, score=0.88, **kw):
    return {"id": eid, "camera": "kitchen_living_room", "label": label, "sub_label": kw.pop("sub_label", None),
            "start_time": start, "end_time": end, "has_snapshot": True, "false_positive": kw.pop("false_positive", None),
            "data": {"score": score, "top_score": score}, **kw}


def fp():
    return FrigatePresence("http://frigate:5000", {"cat": "bella", "dog": "rex"}, min_score=0.7, clock=lambda: NOW)


class TestIdentity(unittest.TestCase):
    def test_label_mapping_and_unknown_person(self):
        p = fp()
        self.assertEqual(p.identify(ev("1", "dog", 1)), "rex")
        self.assertEqual(p.identify(ev("2", "cat", 1)), "bella")
        self.assertEqual(p.identify(ev("3", "person", 1)), "someone")
        self.assertIsNone(p.identify(ev("4", "car", 1)))

    def test_face_sub_label_wins_in_both_shapes(self):
        p = fp()
        self.assertEqual(p.identify(ev("5", "person", 1, sub_label="Alex")), "alex")
        self.assertEqual(p.identify(ev("6", "person", 1, sub_label=["Sam", 0.91])), "sam")


class TestIngest(unittest.TestCase):
    def test_in_view_then_ended(self):
        p = fp()
        p.ingest(ev("a", "dog", NOW - 30))                       # new: no end_time
        loc = p.locations()["rex"]
        self.assertEqual(loc["minutes_ago"], 0.0)
        self.assertEqual(loc["doing"], "in view now")
        p.ingest(ev("a", "dog", NOW - 30, end=NOW - 120))         # end
        loc = p.locations()["rex"]
        self.assertEqual(loc["minutes_ago"], 2.0)
        self.assertEqual(loc["camera"], "kitchen living room")
        self.assertTrue(loc["snapshot_url"].endswith("/api/events/a/snapshot.jpg"))

    def test_low_score_and_false_positive_are_ignored(self):
        p = fp()
        self.assertIsNone(p.ingest(ev("b", "dog", NOW, end=NOW, score=0.5)))
        self.assertIsNone(p.ingest(ev("c", "dog", NOW, end=NOW, false_positive=True)))
        self.assertEqual(p.locations(), {})

    def test_older_event_does_not_overwrite_newer(self):
        p = fp()
        p.ingest(ev("new", "cat", NOW - 60, end=NOW - 60))
        p.ingest(ev("old", "cat", NOW - 900, end=NOW - 900))     # replayed out of order
        self.assertEqual(p.locations()["bella"]["event_id"], "new")

    def test_reject_falls_back_to_the_previous_sighting_for_good(self):
        p = fp()
        p.ingest(ev("old", "dog", NOW - 900, end=NOW - 900))
        p.ingest(ev("new", "dog", NOW - 60, end=NOW - 60))
        p.apply_correction("new", None)                           # the owner: that was not Rex
        self.assertEqual(p.locations()["rex"]["event_id"], "old")
        p.ingest(ev("new", "dog", NOW - 60, end=NOW - 60))        # replayed by a reconnect: stays rejected
        self.assertEqual(p.locations()["rex"]["event_id"], "old")
        p.apply_correction("old", None)
        self.assertNotIn("rex", p.locations())

    def test_relabel_moves_the_sighting_and_the_old_identity_falls_back(self):
        p = fp()
        p.ingest(ev("k1", "dog", NOW - 900, end=NOW - 900))
        p.ingest(ev("k2", "dog", NOW - 60, end=NOW - 60))
        p.apply_correction("k2", "Bella")                          # the "dog" was the cat
        self.assertEqual(p.locations()["bella"]["event_id"], "k2")
        self.assertEqual(p.locations()["rex"]["event_id"], "k1")

    def test_websocket_frame_with_string_payload(self):
        p = fp()
        frame = json.dumps({"topic": "events", "payload": json.dumps({"type": "new", "after": ev("w", "dog", NOW)})})
        self.assertEqual(p.handle_message(frame), "rex")
        self.assertIsNone(p.handle_message(json.dumps({"topic": "stats", "payload": "{}"})))


class TestCameraLook(unittest.TestCase):
    def _fake_urlopen(self, events):
        import io
        from unittest import mock
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(json.dumps(events).encode())
        return mock.patch("hearth.frigate_presence.urllib.request.urlopen", return_value=resp)

    def test_in_view_now_filters_ended_low_and_false_positive(self):
        events = [ev("live", "dog", NOW - 40), ev("done", "cat", NOW - 90, end=NOW - 60),
                  ev("weak", "person", NOW - 5, score=0.4), ev("fp", "person", NOW - 5, false_positive=True),
                  ev("anon", "person", NOW - 5)]
        with self._fake_urlopen(events):
            objs = fp().in_view_now("kitchen_living_room")
        self.assertEqual([(o["name"], o["for_s"]) for o in objs], [("rex", 40), ("someone", 5)])

    def test_describe_in_view_names_known_and_hedges_unknown(self):
        from hearth.frigate_presence import describe_in_view
        text = describe_in_view([{"label": "dog", "name": "rex", "for_s": 40},
                                 {"label": "person", "name": "someone", "for_s": 5}])
        self.assertEqual(text, "Rex (dog, in view 40 s), a person (not identified, in view 5 s)")


class TestLiveCameras(unittest.TestCase):
    def test_prefers_sub_stream_and_skips_cameras_without_one(self):
        import io
        from unittest import mock
        from hearth.frigate_presence import live_cameras
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(json.dumps({"kitchen_living_room": {}, "kitchen_living_room_sub": {},
                                                             "garage": {}}).encode())
        with mock.patch("hearth.frigate_presence.urllib.request.urlopen", return_value=resp):
            cams = live_cameras("http://go2rtc:1984", ["kitchen_living_room", "garage", "attic"])
        self.assertEqual(cams, [{"id": "kitchen_living_room", "name": "kitchen living room", "stream": "kitchen_living_room_sub"},
                                {"id": "garage", "name": "garage", "stream": "garage"}])

    def test_live_cameras_uses_the_callers_stream_list(self):
        from unittest import mock
        from hearth.frigate_presence import live_cameras
        with mock.patch("hearth.frigate_presence.urllib.request.urlopen", side_effect=AssertionError("no fetch")):
            cams = live_cameras("http://go2rtc:1984", ["garage"], {"garage", "garage_sub"})
        self.assertEqual(cams[0]["stream"], "garage_sub")

    def test_event_id_pattern_rejects_paths(self):
        from hearth.frigate_presence import EVENT_ID
        self.assertTrue(EVENT_ID.match("1790276100.088879-gen6ep"))
        self.assertFalse(EVENT_ID.match("../../api/config"))
        self.assertFalse(EVENT_ID.match("1790276100.088879-gen6ep/../x"))


class TestMerge(unittest.TestCase):
    def test_newest_source_wins_per_identity(self):
        hub = {"alex": {"minutes_ago": 12.0, "camera": "Kitchen/Living"}, "rex": {"minutes_ago": 30.0}}
        frig = {"rex": {"minutes_ago": 0.0, "source": "frigate"}, "bella": {"minutes_ago": 5.0, "source": "frigate"}}
        out = merge_locations(hub, frig)
        self.assertEqual(out["rex"]["source"], "frigate")
        self.assertEqual(out["alex"]["minutes_ago"], 12.0)   # hub still names people Frigate can't
        self.assertIn("bella", out)

    def test_card_leads_with_gps_home_status(self):
        card = build_presence_card({
            "locations": {"sam": {"minutes_ago": 180.0, "camera": "Kitchen/Living"}},
            "home_status": {"sam": {"state": "not_home", "source_label": "Life360", "since_min": 126.0},
                            "alex": {"state": "home", "source_label": "Life360", "since_min": 12.0}}},
            who=TEST_HOUSEHOLD.who)
        self.assertIn("- Sam: away (Life360, 2.1 h); camera: seen 3.0 h ago on Kitchen/Living", card)
        self.assertIn("- Alex: home (Life360, 12 min); camera: no recent sighting", card)
        self.assertIn("- Bella: no recent sighting", card)          # pets have no GPS line

    def test_presence_now_trusts_gps_away_and_skips_the_camera(self):
        from stonesage.courage import CourageDeps, CourageTools
        looks = []
        state = {"locations": {"sam": {"minutes_ago": 300.0, "camera": "Kitchen/Living"}},
                 "home_status": {"sam": {"state": "not_home", "source_label": "Life360", "since_min": 40.0}}}
        tools = CourageTools(CourageDeps(ha_states=None, ha_call=None, presence=lambda: state,
                                         camera_look=lambda e, n, **kw: looks.append(e) or "empty room", camera_scan=None,
                                         household=TEST_HOUSEHOLD))
        out = tools._presence_now("sam")
        self.assertEqual(out["gps"], "away (Life360, 40 min)")
        self.assertEqual(looks, [])                                  # no pointless camera look
        self.assertIn("sam", tools._presence_now()["gps"])

    def test_card_shows_in_view_and_unidentified_person(self):
        card = build_presence_card({"locations": {"rex": {"minutes_ago": 0.0, "camera": "kitchen living room"},
                                                  "someone": {"minutes_ago": 3.0, "camera": "kitchen living room"}}},
                                   who=TEST_HOUSEHOLD.who)
        self.assertIn("- Rex: in view now on kitchen living room", card)
        self.assertIn("A person, not identified: seen 3 min ago", card)


if __name__ == "__main__":
    unittest.main()
