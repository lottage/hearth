"""Offline tests for presence corrections: wildlife_admin.py (in a temp dir), the StoneSage router with fake
SSH/Frigate, and FrigatePresence.apply_correction."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "deploy", "cluster-bridge"))

import wildlife_admin as wa  # noqa: E402
from hearth.frigate_presence import FrigatePresence, norm_name  # noqa: E402
from hearth.presence_corrections import PresenceCorrections  # noqa: E402

ENTITIES = {"people": [{"name": "Alex"}, {"name": "Sam"}], "pets": [{"name": "Bella", "species": "cat"}]}


class TestWildlifeAdmin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        wa.BASE_DIR = self.dir
        os.makedirs(os.path.join(self.dir, "snapshots"))
        with open(os.path.join(self.dir, "known_entities.json"), "w") as f:
            json.dump(ENTITIES, f)
        open(os.path.join(self.dir, "snapshots", "alex_20260924_153217.jpg"), "wb").write(b"jpg")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def call(self, cmd, args):
        out = io.StringIO()
        sys.stdout, old = out, sys.stdout
        try:
            wa.main(["wildlife_admin.py", cmd], stdin=io.StringIO(json.dumps(args)))
        finally:
            sys.stdout = old
        return json.loads(out.getvalue())

    def test_relabel_keeps_timestamp_and_logs(self):
        res = self.call("relabel", {"file": "alex_20260924_153217.jpg", "name": "sam"})
        self.assertEqual(res, {"ok": True, "file": "sam_20260924_153217.jpg"})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "snapshots", "sam_20260924_153217.jpg")))
        log = open(os.path.join(self.dir, "corrections.jsonl")).read()
        self.assertIn('"action": "relabel"', log)

    def test_relabel_into_a_taken_name_gets_a_suffix(self):
        # the sentry saved Alex and Bella from one frame in the same second
        open(os.path.join(self.dir, "snapshots", "bella_20260924_153217.jpg"), "wb").write(b"jpg")
        res = self.call("relabel", {"file": "bella_20260924_153217.jpg", "name": "Alex"})
        self.assertEqual(res, {"ok": True, "file": "alex_20260924_153217-2.jpg"})
        again = self.call("relabel", {"file": "alex_20260924_153217-2.jpg", "name": "Alex"})
        self.assertTrue(again.get("unchanged"))
        self.assertEqual(self.call("relabel", {"file": "alex_20260924_153217-2.jpg", "name": "Bella"})["file"],
                         "bella_20260924_153217.jpg")

    def test_reject_moves_aside(self):
        self.call("reject", {"file": "alex_20260924_153217.jpg"})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "rejected", "alex_20260924_153217.jpg")))

    def test_path_tricks_and_unknown_names_refused(self):
        self.assertFalse(self.call("reject", {"file": "../known_entities.json"})["ok"])
        self.assertFalse(self.call("relabel", {"file": "alex_20260924_153217.jpg", "name": "Nobody"})["ok"])

    def test_profile_add_with_backup_and_duplicate_check(self):
        res = self.call("profile-add", {"name": "Aunt May", "kind": "person", "role": "Visitor"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["profile"]["id"], "person-aunt-may")
        self.assertTrue(any(f.startswith("known_entities.json.bak-") for f in os.listdir(self.dir)))
        self.assertFalse(self.call("profile-add", {"name": "aunt may", "kind": "person"})["ok"])
        self.assertFalse(self.call("profile-add", {"name": "Bad;Name", "kind": "pet"})["ok"])
        # relabel to the new profile gives the prefix the hub and frontend expect
        res = self.call("relabel", {"file": "alex_20260924_153217.jpg", "name": "Aunt May"})
        self.assertEqual(res["file"], "aunt-may_20260924_153217.jpg")
        self.assertEqual(wa.prefix("Aunt May"), norm_name("Aunt May"))


class FakeRun:
    """Stands in for ssh -> wildlife_admin.py; records commands, answers from a script."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, input=None, **kw):
        cmd = argv[-1].split()[-1]
        self.calls.append((cmd, json.loads(input)))
        out = self.answers.get(cmd, {"ok": True})
        return type("R", (), {"stdout": json.dumps(out), "stderr": ""})()


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.http_calls = []

    def http(self, method, path, body=None, ctype="application/json"):
        self.http_calls.append((method, path, body))
        if path == "/api/faces":
            return {"train": ["1790278172.66522-sduwop-1.2-unknown-0.6.webp", "999.1-other-1.0.webp"]}
        return {"success": True}

    def pc(self, answers=None):
        run = FakeRun({"profiles": {"ok": True, "entities": ENTITIES}, **(answers or {})})
        return PresenceCorrections("u@h", "/opt/x.py", "http://frigate:5000", run=run, http=self.http), run

    def test_frigate_person_relabel_sets_sub_label_and_trains_this_events_faces(self):
        pc, _ = self.pc()
        res = pc.correct("frigate", "1790278172.66522-sduwop", "relabel", "sam")
        self.assertEqual((res["ok"], res["name"], res["faces_trained"]), (True, "Sam", 1))
        self.assertIn(("POST", "/api/events/1790278172.66522-sduwop/sub_label",
                       json.dumps({"subLabel": "Sam", "subLabelScore": 1.0}).encode()), self.http_calls)
        classify = [c for c in self.http_calls if c[1].endswith("/classify")]
        self.assertEqual(len(classify), 1)

    def test_frigate_reject_marks_false_positive(self):
        pc, _ = self.pc()
        pc.correct("frigate", "1790278172.66522-sduwop", "reject")
        self.assertEqual(self.http_calls[-1][:2], ("PUT", "/api/events/1790278172.66522-sduwop/false_positive"))

    def test_pet_relabel_does_not_touch_faces(self):
        pc, _ = self.pc()
        pc.correct("frigate", "1790278172.66522-sduwop", "relabel", "Bella")
        self.assertFalse([c for c in self.http_calls if "/faces" in c[1]])

    def test_sentry_relabel_to_person_registers_the_face(self):
        pc, run = self.pc({"relabel": {"ok": True, "file": "sam_20260924_153217.jpg"},
                           "snapshot": {"ok": True, "jpeg_b64": "anBn"}})
        res = pc.correct("sentry", "alex_20260924_153217.jpg", "relabel", "Sam")
        self.assertTrue(res["face_registered"])
        self.assertIn(("relabel", {"file": "alex_20260924_153217.jpg", "name": "Sam"}), run.calls)
        self.assertTrue(any(c[1] == "/api/faces/Sam/register" for c in self.http_calls))

    def test_unknown_profile_and_bad_ids_refused(self):
        pc, _ = self.pc()
        self.assertFalse(pc.correct("frigate", "1790278172.66522-sduwop", "relabel", "Nobody")["ok"])
        self.assertFalse(pc.correct("frigate", "../../config", "reject")["ok"])
        self.assertFalse(pc.correct("elsewhere", "x", "reject")["ok"])


class TestApplyCorrection(unittest.TestCase):
    def test_relabel_and_reject_in_memory(self):
        fp = FrigatePresence("http://f", {"dog": "rex"}, clock=lambda: 1000.0)
        fp.ingest({"id": "1.1-a", "camera": "kitchen", "label": "person", "start_time": 900, "end_time": 950,
                   "data": {"top_score": 0.9}})
        self.assertIn("someone", fp.locations())
        fp.apply_correction("1.1-a", "Sam")
        self.assertEqual(set(fp.locations()), {"sam"})
        fp.apply_correction("1.1-a", None)
        self.assertEqual(fp.locations(), {})


if __name__ == "__main__":
    unittest.main()
