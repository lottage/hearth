"""Offline tests for sighting boxes: parsing the vision model's grounding answer, how a profile is described to it,
the face-training crop, and the cache (a failed look is retried after an hour)."""

import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from PIL import Image  # noqa: E402

from hearth import sighting_boxes as sb  # noqa: E402


class TestParse(unittest.TestCase):
    def test_qwen_grounding_answer_to_fractions(self):
        text = '```json\n[\n\t{"bbox_2d": [408, 215, 459, 258], "label": "dog"}\n]\n```'   # real answer, 640x360 frame
        self.assertEqual(sb.parse_box(text, 640, 360), [0.6375, 0.5972, 0.0797, 0.1194])

    def test_clamped_swapped_and_degenerate(self):
        self.assertEqual(sb.parse_box("[700, 400, 600, 300]", 640, 360), [0.9375, 0.8333, 0.0625, 0.1667])
        self.assertIsNone(sb.parse_box("[10, 10, 12, 12]", 640, 360))       # too small to be a subject
        self.assertIsNone(sb.parse_box("I cannot see a dog.", 640, 360))


class TestDescribe(unittest.TestCase):
    def test_pets_people_and_unknowns(self):
        self.assertEqual(sb.describe({"species": "dog", "traits": "Long-haired dachshund, black and tan coat."}, "Rex"),
                         "the dog (Long-haired dachshund, black and tan coat)")
        self.assertEqual(sb.describe({"role": "Resident", "traits": ""}, "Alex"), "the person")
        self.assertEqual(sb.describe(None, "someone"), "the person")


class TestCropAndCache(unittest.TestCase):
    def test_crop_keeps_room_around_the_subject(self):
        buf = io.BytesIO()
        Image.new("RGB", (1280, 720)).save(buf, format="JPEG")
        out = Image.open(io.BytesIO(sb.crop(buf.getvalue(), [0.5, 0.5, 0.1, 0.2])))
        self.assertGreater(out.width, 128)                                   # wider than the box itself
        self.assertLessEqual(out.width, 1280 - 640)                          # clipped at the right edge

    def test_cache_persists_and_retries_misses(self):
        now = [1000.0]
        path = os.path.join(tempfile.mkdtemp(), "boxes.json")
        c = sb.BoxCache(path, clock=lambda: now[0])
        c.put("sentry|a.jpg|rex", [0.1, 0.1, 0.2, 0.2])
        c.put("sentry|b.jpg|rex", None)
        c2 = sb.BoxCache(path, clock=lambda: now[0])
        self.assertEqual(c2.get("sentry|a.jpg|rex"), (True, [0.1, 0.1, 0.2, 0.2]))
        self.assertEqual(c2.get("sentry|b.jpg|rex"), (True, None))         # a miss is remembered...
        now[0] += sb.RETRY_S + 1
        self.assertEqual(c2.get("sentry|b.jpg|rex"), (False, None))        # ...for an hour


class TestFaceCropHook(unittest.TestCase):
    def test_registered_face_goes_through_prepare_face(self):
        from hearth.presence_corrections import PresenceCorrections
        sent = []
        pc = PresenceCorrections("h", "/x", "http://frigate:5000",
                                 http=lambda m, path, body=None, ctype=None: sent.append(body) or {"success": True},
                                 prepare_face=lambda jpeg, name: b"CROPPED-" + name.encode())
        self.assertTrue(pc.register_face_image(b"FULLFRAME", "Alex"))
        self.assertIn(b"CROPPED-Alex", sent[0])
        self.assertNotIn(b"FULLFRAME", sent[0])


if __name__ == "__main__":
    unittest.main()
