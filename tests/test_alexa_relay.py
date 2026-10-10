"""Alexa relay: sealing, the home-side listener, the skill's phrase table, and the Lambda itself run in node against it (no network)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "deploy", "alexa-skill"))

from hearth import alexa_relay as ar  # noqa: E402
import build_skill  # noqa: E402

KEY = "k" * 40
NODE = shutil.which("node")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Resp:
    """What urlopen returns: iterable of lines, a context manager."""
    def __init__(self, lines=(), status=200):
        self.lines, self.status = list(lines), status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.lines)


class Net:
    def __init__(self, stream=(), fail_post=False):
        self.posts, self.urls, self.stream, self.fail_post = [], [], list(stream), fail_post

    def __call__(self, req, timeout=0):
        self.urls.append(req.full_url)
        if req.get_method() == "POST":
            if self.fail_post:
                raise OSError("relay down")
            self.posts.append((req.full_url.rsplit("/", 1)[1], req.data.decode()))
            return Resp()
        return Resp(self.stream)


def make(run_turn=None, clock=None, **kw):
    clock = clock or Clock()
    net = kw.pop("net", None) or Net()
    said = []
    relay = ar.AlexaRelay(KEY, run_turn or (lambda h, s: ("Done.", False)), announce=lambda t, r: said.append((t, r)),
                          clock=clock, opener=net, **kw)
    return relay, net, said, clock


def request(relay, clock, text="turn off the porch light", rid="r1", sid="s1", **extra):
    body = {"v": 1, "id": rid, "ts": int(clock()), "sid": sid, "text": text, "dl": 6.5, "uid": "u1"}
    body.update(extra)
    return ar.seal(relay.keys, "req", body)


class TestSealing(unittest.TestCase):
    def setUp(self):
        self.k = ar.derive(KEY)

    def test_round_trip(self):
        for obj in ({"a": 1}, {"text": "turn off the porch light", "n": [1, 2, 3]}, {"long": "x" * 3000}):
            self.assertEqual(ar.open_sealed(self.k, "req", ar.seal(self.k, "req", obj)), obj)

    def test_every_seal_is_different(self):
        self.assertNotEqual(ar.seal(self.k, "req", {"a": 1}), ar.seal(self.k, "req", {"a": 1}))

    def test_tampering_is_caught_anywhere(self):
        s = ar.seal(self.k, "req", {"text": "unlock the front door"})
        for i in (0, 10, 25, len(s) - 3):
            bad = s[:i] + ("A" if s[i] != "A" else "B") + s[i + 1:]
            with self.assertRaises(ValueError, msg=f"char {i}"):
                ar.open_sealed(self.k, "req", bad)

    def test_a_request_is_not_an_answer(self):
        with self.assertRaises(ValueError):
            ar.open_sealed(self.k, "res", ar.seal(self.k, "req", {"a": 1}))

    def test_another_key_cannot_open_it(self):
        with self.assertRaises(ValueError):
            ar.open_sealed(ar.derive("z" * 40), "req", ar.seal(self.k, "req", {"a": 1}))

    def test_garbage_is_a_value_error_not_a_crash(self):
        for junk in ("", "abc", "!!!!", "A" * 200, "short"):
            with self.assertRaises(ValueError):
                ar.open_sealed(self.k, "req", junk)

    def test_topics_are_secret_distinct_and_stable(self):
        a, b = ar.derive(KEY), ar.derive("z" * 40)
        self.assertNotEqual(a["req"], a["res"])
        self.assertNotEqual(a["req"], b["req"])
        self.assertEqual(a["req"], ar.derive(KEY)["req"])
        self.assertTrue(a["req"].startswith("ssr-") and len(a["req"]) == 36)

    def test_a_weak_key_is_refused(self):
        with self.assertRaises(ValueError):
            ar.AlexaRelay("short", lambda h, s: ("", False))


class TestProcess(unittest.TestCase):
    def test_answers_on_the_response_topic_with_the_request_id(self):
        relay, net, said, clock = make()
        self.assertEqual(relay.process(request(relay, clock)), "answered")
        topic, body = net.posts[0]
        self.assertEqual(topic, relay.keys["res"])
        ans = ar.open_sealed(relay.keys, "res", body)
        self.assertEqual((ans["re"], ans["say"], ans["more"]), ("r1", "Done.", False))
        self.assertEqual(said, [])

    def test_a_pending_approval_keeps_the_session_open(self):
        relay, net, _s, clock = make(lambda h, s: ("Shall I unlock it? Just say yes.", True))
        relay.process(request(relay, clock))
        self.assertTrue(ar.open_sealed(relay.keys, "res", net.posts[0][1])["more"])

    def test_dropped_without_an_answer(self):
        relay, net, _s, clock = make()
        other = ar.derive("z" * 40)
        cases = {
            "not ours": ar.seal(other, "req", {"v": 1, "id": "x", "ts": int(clock()), "text": "hi"}),
            "malformed": ar.seal(relay.keys, "req", {"v": 1, "id": "x", "ts": int(clock()), "text": ""}),
            "stale": request(relay, clock, rid="old", ts=int(clock()) - 500),
            "future": request(relay, clock, rid="new", ts=int(clock()) + 500),
        }
        for why, text in cases.items():
            self.assertEqual(relay.process(text), "rejected:" + why.replace("future", "stale"), why)
        self.assertEqual(net.posts, [])

    def test_a_replayed_request_is_ignored(self):
        calls = []
        relay, net, _s, clock = make(lambda h, s: (calls.append(1) or "Done.", False))
        text = request(relay, clock)
        self.assertEqual(relay.process(text), "answered")
        self.assertEqual(relay.process(text), "rejected:replay")
        self.assertEqual(len(calls), 1)

    def test_only_allowed_users_when_a_filter_is_set(self):
        relay, net, _s, clock = make(allowed_users=["u1"])
        self.assertEqual(relay.process(request(relay, clock, rid="a")), "answered")
        self.assertEqual(relay.process(request(relay, clock, rid="b", uid="stranger")), "rejected:user not allowed")
        self.assertIn("stranger", relay.users_seen)             # shown in status so the owner can pin their own id

    def test_rate_limit_stops_a_flood(self):
        relay, net, _s, clock = make(max_per_minute=3)
        results = [relay.process(request(relay, clock, rid=f"r{i}")) for i in range(5)]
        self.assertEqual(results, ["answered"] * 3 + ["rejected:rate limit"] * 2)
        clock.t += 61
        self.assertEqual(relay.process(request(relay, clock, rid="later")), "answered")

    def test_a_slow_answer_is_announced_on_the_echo_not_posted(self):
        clock = Clock()

        def slow(h, s):
            clock.t += 6.0                                    # past the deadline minus the return trip
            return "The porch camera sees a cat.", False
        relay, net, said, _c = make(slow, clock=clock)
        self.assertEqual(relay.process(request(relay, clock)), "announced")
        self.assertEqual(net.posts, [])
        self.assertEqual(said, [("The porch camera sees a cat.", "kitchen")])

    def test_an_answer_just_inside_the_deadline_is_posted(self):
        clock = Clock()

        def ok(h, s):
            clock.t += 4.0
            return "Done.", False
        relay, net, said, _c = make(ok, clock=clock)
        self.assertEqual(relay.process(request(relay, clock)), "answered")

    def test_relay_down_falls_back_to_the_echo(self):
        relay, net, said, clock = make(net=Net(fail_post=True))
        self.assertEqual(relay.process(request(relay, clock)), "announced")
        self.assertEqual(said[0][0], "Done.")
        self.assertIn("publish failed", relay.last_error)

    def test_a_crashing_turn_gets_an_honest_answer(self):
        def boom(h, s):
            raise RuntimeError("x")
        relay, net, _s, clock = make(boom)
        self.assertEqual(relay.process(request(relay, clock)), "answered")
        self.assertIn("broke", ar.open_sealed(relay.keys, "res", net.posts[0][1])["say"])

    def test_one_alexa_session_is_one_conversation(self):
        seen = []

        def turn(history, sid):
            seen.append((list(history), sid))
            return f"answer {len(seen)}", False
        relay, net, _s, clock = make(turn)
        relay.process(request(relay, clock, text="where is rex", rid="1", sid="A"))
        relay.process(request(relay, clock, text="and bella", rid="2", sid="A"))
        relay.process(request(relay, clock, text="hello", rid="3", sid="B"))
        self.assertEqual([m["content"] for m in seen[1][0]], ["where is rex", "answer 1", "and bella"])
        self.assertEqual([m["content"] for m in seen[2][0]], ["hello"])
        self.assertEqual(seen[0][1], seen[1][1])
        self.assertNotEqual(seen[0][1], seen[2][1])
        self.assertTrue(seen[0][1].startswith("alexa:"))      # approvals and the voice policy key off this prefix

    def test_history_is_trimmed_and_sessions_expire(self):
        relay, net, _s, clock = make()
        for i in range(10):
            relay.process(request(relay, clock, rid=f"r{i}", sid="A"))
        self.assertLessEqual(len(next(iter(relay._sessions.values()))["history"]), ar.HISTORY_MAX)
        clock.t += ar.SESSION_TTL_S + 1
        relay.process(request(relay, clock, rid="late", sid="B"))
        self.assertEqual(len(relay._sessions), 1)

    def test_a_long_answer_is_cut_to_what_the_relay_can_carry(self):
        relay, net, _s, clock = make(lambda h, s: ("word " * 1000, False))
        relay.process(request(relay, clock))
        say = ar.open_sealed(relay.keys, "res", net.posts[0][1])["say"]
        self.assertLessEqual(len(say), ar.MAX_SAY_CHARS)
        self.assertLess(len(net.posts[0][1]), 4096)

    def test_status_reports_counts(self):
        relay, net, _s, clock = make()
        relay.process(request(relay, clock))
        relay.process("garbage")
        st = relay.status()
        self.assertEqual((st["stats"]["requests"], st["stats"]["answered"], st["stats"]["rejected"]), (1, 1, 1))


class TestListening(unittest.TestCase):
    def test_a_stream_message_is_handled_and_the_next_connection_resumes_after_it(self):
        relay, net0, _s, clock = make()
        got, done = [], threading.Event()
        relay.process = lambda text: (got.append(text), done.set())
        sealed = request(relay, clock)
        stream = [json.dumps({"id": "k1", "event": "open"}).encode(),
                  json.dumps({"id": "k2", "event": "keepalive"}).encode(),
                  b"not json",
                  json.dumps({"id": "m1", "event": "message", "message": sealed}).encode()]
        net = Net(stream=stream)
        relay._open = net
        relay.listen_once()
        self.assertTrue(done.wait(2))
        self.assertEqual(got, [sealed])
        self.assertIn(relay.keys["req"] + "/json?since=", net.urls[0])
        relay.listen_once()
        self.assertTrue(net.urls[1].endswith("since=m1"))      # nothing is read twice, nothing is missed

    def test_it_never_replays_what_was_said_before_it_started(self):
        relay, net, _s, clock = make()
        self.assertEqual(relay._since, str(int(clock())))

    def test_it_only_makes_outbound_requests(self):
        # no server socket anywhere in the module
        src = open(ar.__file__, encoding="utf-8").read()
        for word in ("bind(", "listen(", "HTTPServer", "socketserver"):
            self.assertNotIn(word, src)


class TestSkillBuild(unittest.TestCase):
    def test_the_committed_template_holds_no_key(self):
        t = open(os.path.join(ROOT, "deploy", "alexa-skill", "index.template.js"), encoding="utf-8").read()
        self.assertIn("__RELAY_KEY__", t)
        rendered = build_skill.render_lambda(t, KEY)
        self.assertIn(KEY, rendered)
        for ph in ("__RELAY_KEY__", "__CARRIERS__", "__SIMPLE__"):
            self.assertNotIn(ph, rendered)

    def test_the_model_is_well_formed(self):
        m = build_skill.model("computer")["interactionModel"]["languageModel"]
        names = [i["name"] for i in m["intents"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(m["invocationName"], "computer")
        for needed in ("AMAZON.StopIntent", "AMAZON.HelpIntent", "AMAZON.YesIntent", "AMAZON.NoIntent", "AMAZON.FallbackIntent"):
            self.assertIn(needed, names)
        for i in m["intents"]:
            if i["name"].startswith("C_"):                    # a free-text slot needs a carrier word in front of it
                self.assertEqual(i["samples"], [build_skill.carriers()[i["name"]] + " {query}"])
                self.assertEqual(i["slots"], [{"name": "query", "type": "AMAZON.SearchQuery"}])
        self.assertEqual(set(build_skill.carriers()) | set(build_skill.simple()),
                         {n for n in names if n[:2] in ("C_", "S_")})

    def test_the_openers_cover_what_the_house_is_asked(self):
        words = set(build_skill.carriers().values())
        for w in ("turn", "is", "where", "what", "who", "unlock", "show", "tell"):
            self.assertIn(w, words)
        self.assertNotIn("stop", words)                       # Amazon's own StopIntent owns that word


@unittest.skipUnless(NODE, "node is not installed")
class TestLambdaInNode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        t = open(os.path.join(ROOT, "deploy", "alexa-skill", "index.template.js"), encoding="utf-8").read()
        open(os.path.join(cls.dir, "index.js"), "w", encoding="utf-8").write(build_skill.render_lambda(t, KEY))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def node(self, script, *args):
        p = subprocess.run([NODE, "-e", script, *args], cwd=self.dir, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_node_and_python_seal_the_same_way(self):
        keys = ar.derive(KEY)
        out = self.node("const t=require('./index.js')._test;const k=t.derive(process.argv[1]);"
                        "const mine=t.seal(k,'req',{id:'n1',text:'turn off the lamp'});"
                        "const theirs=t.open(k,'res',process.argv[2]);"
                        "console.log(JSON.stringify({mine,theirs,req:k.req,res:k.res}))",
                        KEY, ar.seal(keys, "res", {"re": "p1", "say": "Done.", "more": False}))
        self.assertEqual(ar.open_sealed(keys, "req", out["mine"]), {"id": "n1", "text": "turn off the lamp"})
        self.assertEqual(out["theirs"], {"re": "p1", "say": "Done.", "more": False})
        self.assertEqual((out["req"], out["res"]), (keys["req"], keys["res"]))      # same secret topics on both sides

    def test_node_refuses_what_python_would(self):
        out = self.node("const t=require('./index.js')._test;const k=t.derive(process.argv[1]);let r=[];"
                        "for(const [label,s] of [['res',process.argv[2]],['req',process.argv[3]]]){"
                        "try{t.open(k,label,s);r.push('opened')}catch(e){r.push(e.message)}}"
                        "console.log(JSON.stringify(r))",
                        KEY, ar.seal(ar.derive(KEY), "req", {"a": 1}), ar.seal(ar.derive("z" * 40), "req", {"a": 1}))
        self.assertEqual(out, ["bad tag", "bad tag"])

    def run_handler(self, event, answer):
        script = ("const m=require('./index.js');const t=m._test;const k=t.derive(process.argv[1]);const ev=JSON.parse(process.argv[2]);"
                  "const ans=JSON.parse(process.argv[3]);const posted=[];"
                  "t.relay.publish=async(topic,body)=>{if(ans.publishFails)throw new Error('down');posted.push({topic,msg:t.open(k,'req',body)})};"
                  "t.relay.waitFor=async(keys,id)=>ans.reply?Object.assign({re:id},ans.reply):null;"
                  "m.handler(ev).then(r=>console.log(JSON.stringify({r,posted,topicOk:posted.every(p=>p.topic===k.req)})))")
        return self.node(script, KEY, json.dumps(event), json.dumps(answer))

    @staticmethod
    def intent(name, query=None, session="sess-1"):
        slots = {"query": {"name": "query", "value": query}} if query is not None else {}
        return {"session": {"sessionId": session},
                "context": {"System": {"user": {"userId": "amzn1.ask.account.X"}, "device": {"deviceId": "amzn1.ask.device.Y"}}},
                "request": {"type": "IntentRequest", "intent": {"name": name, "slots": slots}}}

    def test_a_spoken_sentence_is_rebuilt_and_sent_sealed(self):
        out = self.run_handler(self.intent("C_turn", "off the porch light"), {"reply": {"say": "Porch light off.", "more": False}})
        self.assertEqual(out["posted"][0]["msg"]["text"], "turn off the porch light")
        self.assertEqual(out["posted"][0]["msg"]["sid"], "sess-1")
        self.assertTrue(out["topicOk"])
        self.assertEqual(out["r"]["response"]["outputSpeech"]["text"], "Porch light off.")
        self.assertTrue(out["r"]["response"]["shouldEndSession"])
        self.assertNotIn("amzn1", json.dumps(out["posted"]))     # the Alexa ids leave only as hashes

    def test_questions_keep_their_opener(self):
        out = self.run_handler(self.intent("C_where", "is rex"), {"reply": {"say": "In the kitchen.", "more": False}})
        self.assertEqual(out["posted"][0]["msg"]["text"], "where is rex")

    def test_a_pending_yes_no_keeps_the_session_open(self):
        out = self.run_handler(self.intent("C_unlock", "the front door"), {"reply": {"say": "Shall I? Just say yes.", "more": True}})
        self.assertFalse(out["r"]["response"]["shouldEndSession"])
        self.assertIn("reprompt", out["r"]["response"])
        yes = self.run_handler(self.intent("AMAZON.YesIntent"), {"reply": {"say": "Done.", "more": False}})
        self.assertEqual(yes["posted"][0]["msg"]["text"], "yes")

    def test_no_answer_in_time_says_so_and_ends(self):
        out = self.run_handler(self.intent("C_check", "the driveway camera"), {})
        self.assertIn("taking a while", out["r"]["response"]["outputSpeech"]["text"])
        self.assertTrue(out["r"]["response"]["shouldEndSession"])

    def test_an_unreachable_relay_is_reported(self):
        out = self.run_handler(self.intent("C_turn", "on the lamp"), {"publishFails": True})
        self.assertIn("can't reach", out["r"]["response"]["outputSpeech"]["text"])

    def test_launch_stop_help_and_unknown_never_reach_the_house(self):
        for ev, speech, ends in (({"request": {"type": "LaunchRequest"}}, "What do you need", False),
                                 ({"request": {"type": "IntentRequest", "intent": {"name": "AMAZON.StopIntent"}}}, "Right", True),
                                 ({"request": {"type": "IntentRequest", "intent": {"name": "AMAZON.HelpIntent"}}}, "Ask me", False),
                                 (self.intent("SomethingElse"), "did not catch", False)):
            out = self.run_handler(ev, {"reply": {"say": "SHOULD NOT BE SENT", "more": False}})
            self.assertEqual(out["posted"], [])
            self.assertIn(speech, out["r"]["response"]["outputSpeech"]["text"])
            self.assertEqual(out["r"]["response"]["shouldEndSession"], ends)

    def test_small_talk_sentences(self):
        out = self.run_handler(self.intent("S_thanks"), {"reply": {"say": "Anytime.", "more": False}})
        self.assertEqual(out["posted"][0]["msg"]["text"], "thank you")


if __name__ == "__main__":
    unittest.main()
