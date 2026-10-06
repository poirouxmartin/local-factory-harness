"""Chat sessions on disk: one session, one JSON file.

Same philosophy as job_store: the filesystem is the database, every write is
atomic, a reader never sees a half-written session. Flat files (not a directory
per session) because a session has exactly one document and no artifacts.
"""
import json
import time
import uuid
from pathlib import Path

import lessons
from job_store import atomic_write_json, read_json

TITLE_LEN = 60


class ChatNotFound(Exception):
    pass


def auto_title(text):
    """A session's name, taken from the first thing asked of it.

    The raw first 60 characters were the name until now, which on a pasted
    spec produced a title made of a Markdown heading glued to half a bullet.
    Take the first line that actually says something, drop the markup that
    starts it, and cut on a word rather than mid-syllable.
    """
    line = ""
    for raw in (text or "").splitlines():
        line = " ".join(raw.split()).lstrip("#>-*+ ").strip()
        if line:
            break
    if len(line) <= TITLE_LEN:
        return line
    # -1 : l'ellipse compte dans la limite, sinon le nom auto est le seul qui
    # puisse dépasser ce que `set_title` accepte.
    cut = line[:TITLE_LEN - 1].rsplit(" ", 1)[0] or line[:TITLE_LEN - 1]
    return cut.rstrip(" ,;:.") + "…"


class ChatStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id):
        return self.root / (session_id + ".json")

    def create(self, model, now=None, kind=None, mode=None, workspace=None,
              toolset=None):
        now = time.time() if now is None else now
        session = {"session_id": "c_" + uuid.uuid4().hex[:8], "title": "",
                   "model": model, "created_at": now, "updated_at": now,
                   "messages": []}
        if kind == "agent":
            session.update(kind="agent", mode=mode or "approve",
                           workspace=workspace, pending_calls=[],
                           toolset=toolset or "base")
        atomic_write_json(self._path(session["session_id"]), session)
        return session

    def get(self, session_id):
        try:
            return read_json(self._path(session_id))
        except FileNotFoundError:
            raise ChatNotFound(session_id)

    def _save(self, session, now):
        session["updated_at"] = time.time() if now is None else now
        atomic_write_json(self._path(session["session_id"]), session)
        return session

    def append(self, session_id, message, now=None):
        session = self.get(session_id)
        session["messages"].append(message)
        if (not session["title"] and not session.get("title_locked")
                and message.get("role") == "user"):
            session["title"] = auto_title(message.get("content"))
        return self._save(session, now)

    def set_title(self, session_id, title, now=None):
        """A name the operator chose. Locked: the auto-title fires on the first
        user message, and a rename that a later turn silently undoes is worse
        than no rename at all. An empty title hands the name back to the
        machine -- that is the only way out of the lock."""
        session = self.get(session_id)
        title = " ".join((title or "").split())[:TITLE_LEN]
        session["title_locked"] = bool(title)
        if not title:
            # Re-derived now, not on the next message: a session that has
            # already been talked to would otherwise sit nameless for a turn.
            first = next((m for m in session["messages"]
                          if m.get("role") == "user"), None)
            title = auto_title(first.get("content")) if first else ""
        session["title"] = title
        return self._save(session, now)

    def set_model(self, session_id, model, now=None):
        session = self.get(session_id)
        session["model"] = model
        return self._save(session, now)

    def set_mode(self, session_id, mode, now=None):
        session = self.get(session_id)
        session["mode"] = mode
        return self._save(session, now)

    def set_num_ctx(self, session_id, num_ctx, now=None):
        session = self.get(session_id)
        session["num_ctx"] = num_ctx
        return self._save(session, now)

    def set_pending_calls(self, session_id, calls, now=None):
        session = self.get(session_id)
        session["pending_calls"] = calls
        return self._save(session, now)

    def mark_stalled(self, session_id, now=None):
        """Flag the last assistant message as a turn that ended without the
        call it announced.

        The transcript is re-serialised into a prompt every turn, so a stall
        written once is replayed as `[assistant]` precedent for the rest of the
        session (session c_14f20c89: four "je n'ai pas les outils dans ce tour"
        in thirteen minutes, each one read again by the turn after it). The flag
        is what lets the loop send a neutral line in its place -- here, on disk,
        the words are kept: the audits and the replays read the real text.
        """
        session = self.get(session_id)
        for msg in reversed(session["messages"]):
            if msg.get("role") == "assistant":
                msg["stalled"] = True
                break
        return self._save(session, now)

    def set_summary(self, session_id, summary, now=None):
        session = self.get(session_id)
        session["summary"] = summary
        return self._save(session, now)

    def set_learned_upto(self, session_id, n, now=None):
        """How far into the transcript learning has already looked. Learning
        runs every turn over a growing transcript, so without this watermark
        the same failures are re-counted once per turn."""
        session = self.get(session_id)
        session["learned_upto"] = n
        return self._save(session, now)

    def set_memory_written(self, session_id, written, now=None):
        """How many failures of each command this session has already banked in
        the workspace MEMORY.md. Memory recounts the whole transcript every
        turn (its floor is two failures, which one turn rarely holds), so this
        is what keeps the same failure from being written twice."""
        session = self.get(session_id)
        session["memory_written"] = written
        return self._save(session, now)

    def set_traced_codes(self, session_id, codes, now=None):
        """Which trace findings this session has already turned into lessons.
        The analysis re-reads the whole trace every turn and `merge` adds
        counts, so this is what keeps one replayed call from reading as ten
        sessions after ten quiet turns."""
        session = self.get(session_id)
        session["traced_codes"] = sorted(set(codes))
        return self._save(session, now)

    def set_calibration(self, session_id, cal, now=None):
        session = self.get(session_id)
        session["calibration"] = cal
        return self._save(session, now)

    def delete(self, session_id):
        try:
            self._path(session_id).unlink()
        except FileNotFoundError:
            raise ChatNotFound(session_id)

    def list(self):
        out = []
        for p in sorted(self.root.glob("c_*.json")):
            s = read_json(p)
            out.append({"session_id": s["session_id"], "title": s["title"],
                        "model": s["model"], "updated_at": s["updated_at"],
                        "messages": len(s["messages"]), "kind": s.get("kind"),
                        "workspace": s.get("workspace")})
        out.sort(key=lambda s: s["updated_at"], reverse=True)
        return out

    # ---- Lessons / auto-improvement --------------------------------------------------

    def _lessons_path(self):
        return self.root / "lessons.json"

    def load_lessons(self):
        """Every lesson known, or [] -- a broken lessons file is never a
        reason the studio will not boot."""
        try:
            with open(self._lessons_path(), "r", encoding="utf-8") as fh:
                known = json.load(fh)
        except (OSError, ValueError):
            return []
        return known if isinstance(known, list) else []

    def record_lessons(self, new_lessons):
        """Merge `new_lessons` into the file (dedup by pattern) and return
        everything known afterwards."""
        if not new_lessons:
            return self.load_lessons()
        known = lessons.merge(self.load_lessons(), new_lessons)
        atomic_write_json(self._lessons_path(), known)
        return known
