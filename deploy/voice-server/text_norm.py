"""Transcript clean-up shared by every STT engine.

Parakeet spells acronyms with spaces ("T V lights"); the reflex matcher in StoneSage looks for "tv", so an unfixed transcript
would make "turn off the T V lights" miss its shortcut and go to the model.
"""

import re

# a run of two or more single capital letters separated by single spaces: "T V", "A C", "U S B"
_SPACED_CAPS = re.compile(r"\b(?:[A-Z] ){1,}[A-Z]\b")


def normalize_transcript(text: str) -> str:
    text = (text or "").strip()
    text = _SPACED_CAPS.sub(lambda m: m.group(0).replace(" ", ""), text)
    text = re.sub(r"\s+", " ", text)
    return text
