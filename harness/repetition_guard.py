"""Degenerate-generation detector, shared by every streaming client.

Lives outside any runtime client: it judges text, not transports.
"""
from collections import deque


class RepetitionGuard:
    """Detects degenerate generation and lets the caller cut it short.

    July 14 session evidence: one thinking block held 1909 lines with only 35
    unique ("Je vais lancer le test." x376); another alternated two lines 272
    times. 44 tok/s wasted for minutes until a human hit Stop. The guard keeps
    the last `window` non-blank lines: when the window is full and holds at
    most `min_unique` distinct lines, the generation is looping.

    Legitimate output never does this -- 40 consecutive lines with <=8 distinct
    values does not occur in prose or code.
    """
    WINDOW = 40
    MIN_UNIQUE = 8
    THINKING_CAP = 120_000  # chars; backstop for non-line-shaped degeneration

    def __init__(self, window=WINDOW, min_unique=MIN_UNIQUE):
        self._pending = ""
        self._recent = deque(maxlen=window)
        self._min_unique = min_unique
        self.thinking_chars = 0

    def feed(self, text, thinking=False):
        """Accumulate a streamed delta; True means: stop the generation."""
        if thinking:
            self.thinking_chars += len(text)
            if self.thinking_chars > self.THINKING_CAP:
                return True
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            line = line.strip()
            if line:
                self._recent.append(line)
        return (len(self._recent) == self._recent.maxlen
                and len(set(self._recent)) <= self._min_unique)
