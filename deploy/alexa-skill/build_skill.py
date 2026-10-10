"""Builds what is pasted into the Alexa developer console: index.js (the Lambda, with the shared key filled in) and the interaction
model JSON. Output goes to `out/` next to this file, which is GITIGNORED because index.js contains the key.

    python build_skill.py            # key from StoneSage/backend/config.json -> alexa_relay.key

Why so many intents: Alexa gives a skill free text only through an AMAZON.SearchQuery slot, and that slot needs a carrier word in
front of it, and the skill is never told which sample matched. So each sentence opener is its own intent ("turn {query}",
"is {query}", "where {query}" ...) and the Lambda puts the opener back: "turn" + "off the porch light".
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# sentence openers: commands, then questions. (stop/cancel/help/yes/no are Amazon's built-in intents.)
COMMANDS = ["turn", "switch", "set", "dim", "brighten", "lock", "unlock", "open", "close", "start", "show", "check", "tell", "give",
            "remind", "remember", "add", "play", "pause", "find", "call", "text", "send", "say", "announce", "make", "shut",
            "raise", "lower", "increase", "decrease", "warm", "cool", "flip", "toggle", "activate", "run", "look", "scan",
            "move", "take", "read", "put", "bring", "please", "i"]
QUESTIONS = ["is", "are", "was", "were", "where", "what", "who", "when", "how", "why", "do", "does", "did", "can", "could",
             "will", "would", "which", "has", "have", "any", "anyone", "should"]
# whole sentences that need no slot
SIMPLE = {"hello": ["hello", "hi", "hey", "hey there"], "morning": ["good morning"], "night": ["good night"],
          "thanks": ["thanks", "thank you", "thanks a lot"]}
SIMPLE_TEXT = {"hello": "hello", "morning": "good morning", "night": "good night", "thanks": "thank you"}


def intent_name(word: str) -> str:
    return "C_" + word


def carriers() -> dict:
    return {intent_name(w): w for w in COMMANDS + QUESTIONS}


def simple() -> dict:
    return {"S_" + k: SIMPLE_TEXT[k] for k in SIMPLE}


def model(invocation: str) -> dict:
    intents = [{"name": n, "samples": []} for n in ("AMAZON.CancelIntent", "AMAZON.HelpIntent", "AMAZON.StopIntent",
                                                    "AMAZON.NavigateHomeIntent", "AMAZON.FallbackIntent",
                                                    "AMAZON.YesIntent", "AMAZON.NoIntent")]
    for w in COMMANDS + QUESTIONS:
        intents.append({"name": intent_name(w), "slots": [{"name": "query", "type": "AMAZON.SearchQuery"}],
                        "samples": [f"{w} {{query}}"]})
    for k, phrases in SIMPLE.items():
        intents.append({"name": "S_" + k, "slots": [], "samples": phrases})
    return {"interactionModel": {"languageModel": {"invocationName": invocation, "intents": intents, "types": []}}}


def render_lambda(template: str, key: str) -> str:
    return (template.replace("__RELAY_KEY__", key).replace("__CARRIERS__", json.dumps(carriers(), indent=2))
            .replace("__SIMPLE__", json.dumps(simple(), indent=2)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "StoneSage", "backend", "config.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8-sig"))
    key = (cfg.get("alexa_relay") or {}).get("key", "")
    if len(key) < 32:
        sys.exit("config.json has no alexa_relay.key (>= 32 characters): add one first")
    os.makedirs(args.out, exist_ok=True)
    template = open(os.path.join(HERE, "index.template.js"), encoding="utf-8").read()
    open(os.path.join(args.out, "index.js"), "w", encoding="utf-8", newline="\n").write(render_lambda(template, key))
    open(os.path.join(args.out, "package.json"), "w", encoding="utf-8", newline="\n").write(
        json.dumps({"name": "computer-relay-skill", "version": "1.0.0", "main": "index.js", "private": True}, indent=2) + "\n")
    for name, inv in (("computer", "computer"), ("attic-computer", "attic computer")):
        json.dump(model(inv), open(os.path.join(args.out, f"interaction-model-{name}.json"), "w", encoding="utf-8", newline="\n"), indent=2)
    n = len(model("x")["interactionModel"]["languageModel"]["intents"])
    print(f"wrote index.js, package.json and 2 interaction models ({n} intents) to {args.out}")


if __name__ == "__main__":
    main()
