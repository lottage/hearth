"""
Reflex: plain on/off orders ("turn off the tv lights", "string lights on") skip the LLM.

Deterministic and conservative: it fires only when the sentence is a bare on/off order and the name
matches exactly one light, lamp, plug or fan. Anything else (questions, remarks, ambiguous names,
camera and config switches, server plugs) returns None and Courage's tool loop handles it.
Replaces the old harness System-1 path, whose prototypes named entities that don't exist and whose
vector match could not tell "turn on" from "turn off".
"""

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

ORDER_FIRST = re.compile(r"^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:turn|switch|flip)\s+(on|off)\s+(.+?)"
                         r"(?:\s+please)?\s*[.!?]?$")
NAME_FIRST = re.compile(r"^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:(?:turn|switch|flip)\s+)?(.+?)\s+(on|off)"
                        r"(?:\s+please)?\s*[.!?]?$")
COMPOUND = re.compile(r"\b(and|then|in|for|after|until|at|when|if|unless)\b|\d")
QUESTION_START =re.compile(r"^(is|are|was|were|do|does|did|what|which|who|where|when|why|how)\b")
# switches that are devices you'd switch by voice, not camera or firmware settings
SWITCH_DEVICE = re.compile(r"\b(lights?|lamps?|plugs?|fans?)\b")
SWITCH_SETTING = re.compile(r"\b(led|enabled|detection|alarm|privacy|auto|update|record|sync|lens|indicator|weight|mode)\b")
DROP = {"the", "my", "our", "a", "please"}


def _words(text: str) -> List[str]:
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", text.lower()).split() if w not in DROP]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]  # "lights" == "light"


def parse(text: str) -> Optional[Dict[str, str]]:
    """'turn off the tv lights' -> {'state': 'off', 'name': 'tv lights'}; None if not a bare on/off order."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not t or QUESTION_START.match(t) or len(t) > 60:
        return None
    m = ORDER_FIRST.match(t)
    order = {"state": m.group(1), "name": m.group(2)} if m else None
    if not order:
        m = NAME_FIRST.match(t)
        order = {"state": m.group(2), "name": m.group(1)} if m else None
    # timers, conditions and compound orders ("... in ten minutes and lock up") are the LLM's job
    if order and (len(order["name"].split()) > 5 or COMPOUND.search(order["name"])):
        return None
    return order


def candidates(ha_states: Callable[[str], Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for domain in ("light", "switch", "fan"):
        try:
            res = ha_states(domain) or {}
        except Exception:
            continue
        for e in res.get("entities") or []:
            name = (e.get("friendly_name") or "").lower()
            if domain == "switch" and (not SWITCH_DEVICE.search(name) or SWITCH_SETTING.search(name)):
                continue
            out.append(dict(e, domain=domain))
    return out


def match(order: Dict[str, str], ents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The one entity whose name contains every word the user said, or None (no match or ambiguous)."""
    want = set(_words(order["name"]))
    if not want:
        return None
    exact = [e for e in ents if set(_words(e.get("friendly_name") or "")) == want]
    if len(exact) == 1:
        return exact[0]
    # entity ids carry names too: "String Lights" is switch.front_porch_..._lights, so "porch lights" finds it
    hits = [e for e in ents if want <= set(_words(f"{e.get('friendly_name') or ''} {e['entity_id'].split('.', 1)[-1]}"))]
    return hits[0] if len(hits) == 1 else None


def resolve(text: str, ha_states: Callable[[str], Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], Dict[str, str]]]:
    """The home reflex as the agent loop sees it (injected as CourageAgent.reflex_resolve, so the loop never imports this
    module): (entity, order) for a bare on/off order naming exactly one device, else None."""
    order = parse(text)
    if not order:
        return None
    ent = match(order, candidates(ha_states))
    return (ent, order) if ent else None


# ------------------------------------------------------------ learned phrasings ----
LEARNABLE_SERVICES = {"turn_on", "turn_off", "toggle"}
PHRASE_FILLER = {"please", "courage", "computer", "hey", "hi", "can", "could", "would", "you", "the", "my", "our",
                 "now", "for", "me", "just", "go", "ahead", "and"}


def phrase_key(text: str) -> str:
    """'Please, kill the TV lights!' -> 'kill tv lights' (order kept: the phrase is the cache key)."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in PHRASE_FILLER)


class LearnedReflexes:
    """Exact phrasings that the LLM loop turned into a successful direct on/off order, replayed without the LLM.
    Only direct orders are learned (never approvals or inferred remarks), and a replay still goes through the
    tool's validation (allowlist, server-plug guard, entity must still exist)."""

    def __init__(self, path: str, max_items: int = 300):
        import threading
        self.path, self.max_items = path, max_items
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=1)
        os.replace(tmp, self.path)

    def lookup(self, text: str) -> Optional[Dict[str, Any]]:
        key = phrase_key(text)
        with self._lock:
            return dict(self.items[key], key=key) if key in self.items else None

    def learn(self, text: str, args: Dict[str, Any], name: str) -> bool:
        key = phrase_key(text)
        if (not key or len(text) > 60 or args.get("service") not in LEARNABLE_SERVICES or args.get("data")
                or COMPOUND.search(text.lower()) or QUESTION_START.match(key)):
            return False
        with self._lock:
            old = self.items.get(key, {})
            self.items[key] = {"phrase": text.strip(), "domain": args["domain"], "service": args["service"],
                               "entity_id": args["entity_id"], "name": name, "hits": old.get("hits", 0),
                               "learned_at": old.get("learned_at", time.time()), "last_used": time.time()}
            if len(self.items) > self.max_items:  # drop the least recently used
                oldest = min(self.items, key=lambda k: self.items[k].get("last_used", 0))
                self.items.pop(oldest, None)
            self._save()
        return True

    def hit(self, key: str) -> None:
        with self._lock:
            if key in self.items:
                self.items[key]["hits"] = self.items[key].get("hits", 0) + 1
                self.items[key]["last_used"] = time.time()
                self._save()

    def forget(self, key: str) -> bool:
        with self._lock:
            found = self.items.pop(key, None) is not None
            if found:
                self._save()
            return found
