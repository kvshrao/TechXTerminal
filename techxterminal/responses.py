"""Automatic responses: when a session's output shows some text, type an answer.

A session keeps its rules as text, one per line:

    --More-- => \\s
    [yes/no] => yes\\r
    Press any key => \\r

The left side is matched in the output, in any case. The right side is typed exactly as
written, so Enter must be asked for with \\r. Escapes: \\r Enter, \\n line feed, \\t Tab,
\\e Esc, \\s space, \\\\ a backslash and \\xHH any byte value.
"""

import re
import time
from dataclasses import dataclass

SEPARATOR = "=>"
WINDOW = 400  # characters of recent output searched, so text split between packets still counts
# A rule whose answer makes the pattern appear again would type for ever; this stops it.
MAX_RESPONSES = 20
PER_SECONDS = 5.0

_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{2}|.)")
_SIMPLE = {"r": "\r", "n": "\n", "t": "\t", "e": "\x1b", "s": " ", "\\": "\\"}


@dataclass
class Rule:
    pattern: str  # as written, for display
    reply: str  # with the escapes turned into the characters they stand for


def decode_reply(text: str) -> str | None:
    """The characters an answer stands for; None if it contains an escape that means nothing."""
    bad = False

    def one(match: re.Match) -> str:
        nonlocal bad
        code = match.group(1)
        if code[0] == "x" and len(code) == 3:
            return chr(int(code[1:], 16))
        if code in _SIMPLE:
            return _SIMPLE[code]
        bad = True
        return ""

    reply = _ESCAPE.sub(one, text)
    if text.endswith("\\") and not text.endswith("\\\\"):
        bad = True
    return None if bad else reply


def parse_rules(text: str) -> tuple[list[Rule], list[str]]:
    """The rules in a session's text, and the lines that could not be understood.

    Empty lines and lines starting with # are skipped.
    """
    rules, bad = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pattern, separator, reply = stripped.partition(SEPARATOR)
        pattern, reply = pattern.strip(), reply.strip()
        decoded = decode_reply(reply) if separator else None
        if not pattern or not decoded:
            bad.append(stripped)
            continue
        rules.append(Rule(pattern, decoded))
    return rules, bad


class AutoResponder:
    """Watches output text and says what to type in answer.

    Each appearance of a pattern is answered once: after a match, only output that arrives
    after it is searched. If answers come faster than MAX_RESPONSES in PER_SECONDS, the
    responder stops itself (`stopped` becomes True) instead of typing for ever.
    """

    def __init__(self, rules: list[Rule], clock=time.monotonic):
        self.rules = rules
        self.stopped = False
        self._patterns = [re.compile(re.escape(rule.pattern), re.IGNORECASE) for rule in rules]
        self._tail = ""
        self._clock = clock
        self._recent: list[float] = []

    def feed(self, text: str) -> list[str]:
        """Answers to type for this piece of output, in the order their patterns appeared."""
        if self.stopped or not self.rules:
            return []
        self._tail = (self._tail + text)[-WINDOW:]
        replies = []
        while True:
            found = []
            for pattern, rule in zip(self._patterns, self.rules):
                match = pattern.search(self._tail)
                if match:
                    found.append((match.start(), match.end(), rule))
            if not found:
                break
            _start, end, rule = min(found, key=lambda item: item[:2])  # the earliest, then the shortest
            self._tail = self._tail[end:]
            now = self._clock()
            self._recent = [moment for moment in self._recent if now - moment < PER_SECONDS] + [now]
            if len(self._recent) > MAX_RESPONSES:
                self.stopped = True
                break
            replies.append(rule.reply)
        return replies
