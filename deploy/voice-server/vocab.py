"""Household vocabulary for speech recognition.

Recognizers get the names of people and pets wrong ("Rex" -> "Rexx", "Bella" -> "Bela") because those words are rare in
their training data. Two cheap fixes, both driven by one small file (vocab.txt):
  * a hint for Whisper (its initial prompt), so it expects the words;
  * `correct()`: a token that is a near miss for a known NAME is replaced by it. Deliberately narrow: same first letter,
    at most 1 edit for short words and 2 for longer ones, names only (never the hint-only words), tokens of 4+ letters.

vocab.txt (your household's own: copy vocab.example.txt and put your names in it; without one the server runs and
corrects nothing): one term per line; `#` comments; a leading `+` marks a hint-only word (device words, "Computer") that is given to
Whisper but never used for correction.
"""

import re
from typing import List, Optional, Tuple

_TOKEN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def load(path: str) -> Tuple[List[str], List[str]]:
    """(names, hints): names are corrected to; hints (names included) go into Whisper's prompt."""
    names: List[str] = []
    hints: List[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                t = line.split("#", 1)[0].strip()
                if not t:
                    continue
                if t.startswith("+"):
                    hints.append(t[1:].strip())
                else:
                    names.append(t)
                    hints.append(t)
    except OSError:
        pass
    return names, hints


def prompt_from(hints: List[str]) -> str:
    """Whisper's initial prompt: the words it should expect, as a plain sentence-like list."""
    return ", ".join(hints) + "." if hints else ""


def _edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (adjacent swaps count as one edit)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _best(token: str, names: List[str]) -> Optional[str]:
    t = token.lower()
    if len(t) < 4:
        return None
    best, best_d = None, 99
    for n in names:
        nl = n.lower()
        if nl == t:
            return None                                   # already right
        if nl[0] != t[0]:
            continue
        limit = 1 if max(len(t), len(nl)) <= 5 else 2
        d = _edit_distance(t, nl)
        if d <= limit and d < best_d:
            best, best_d = n, d
    return best


def correct(text: str, names: List[str]) -> str:
    """Replace near-miss spellings of known names; everything else (punctuation, other words, case elsewhere) is kept."""
    if not names or not text:
        return text

    def fix(m: "re.Match[str]") -> str:
        word = m.group(0)
        possessive = ""
        if word.lower().endswith("'s"):
            word, possessive = word[:-2], word[-2:]
        fixed = _best(word, names)
        return (fixed + possessive) if fixed else m.group(0)
    return _TOKEN.sub(fix, text)
