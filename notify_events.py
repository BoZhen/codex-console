"""Read-only mobile notifications with bounded, process-local replay."""
from collections import deque
import re
import secrets
import time


class NotifyHub:
    LIMIT = 512
    MAX_AGE = 24 * 60 * 60

    def __init__(self, sessions, clock=time.time):
        self.sessions = sessions
        self.clock = clock
        self.epoch = secrets.token_hex(12)
        self.sequence = 0
        self.history = deque(maxlen=self.LIMIT)
        self.streams = set()
        self.waiting = {}

    @property
    def cursor(self):
        return f"{self.epoch}:{self.sequence}"

    def _trim(self):
        cutoff = self.clock() - self.MAX_AGE
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def publish(self, event):
        self.sequence += 1
        event = dict(event, id=self.cursor)
        self._trim()
        self.history.append((self.clock(), self.sequence, event))
        for stream in tuple(self.streams):
            stream.send(event)

    @staticmethod
    def _session(session):
        return {"session": session.id, "title": (session.title() or "Codex session")[:100]}

    def on_events(self, session, events):
        for event in events:
            kind = event.get("kind")
            if kind in ("question", "approval"):
                aid = event.get("aid")
                if not isinstance(aid, str) or not aid:
                    continue
                # No question, command, path, answer, or reply text leaves this endpoint.
                item = dict(self._session(session), aid=aid, kind=kind,
                            blocking=event.get("isBlocking") is not False)
                self.waiting[(session.id, aid)] = item
                self.publish(dict(item, type="waiting"))
            elif kind in ("question_resolved", "approval_resolved"):
                aid = event.get("aid")
                if self.waiting.pop((session.id, aid), None) is not None:
                    self.publish({"type": "resolved", "session": session.id, "aid": aid})
            elif kind == "turn_done":
                if (event.get("parent") or event.get("subtype") == "compact"
                        or event.get("notificationSilent")):
                    continue
                self.publish(dict(self._session(session), type="finished",
                                  error=bool(event.get("isError")),
                                  interrupted=bool(event.get("interrupted"))))

    def session_gone(self, session):
        for key in list(self.waiting):
            if key[0] == session.id:
                del self.waiting[key]
        self.publish({"type": "ended", "session": session.id})

    def opening(self, cursor):
        """Snapshot first, retained completions next, then an acknowledged boundary.

        Waiting items are state, not replayable actions. Old completions are never
        replayed on a first connection or across a server restart.
        """
        self._trim()
        match = re.fullmatch(r"([0-9a-f]{24}):(\d{1,16})", cursor or "")
        after = int(match[2]) if match and match[1] == self.epoch else None
        if after is not None and after > self.sequence:
            after = None
        live = {s.id for s in self.sessions().values() if not s.ended}
        oldest = self.history[0][1] if self.history else self.sequence + 1
        snapshot = {"type": "snapshot", "reset": after is None,
                    "gap": after is not None and after < oldest - 1,
                    "waiting": [v for (sid, _), v in self.waiting.items() if sid in live]}
        latest = {}
        if after is not None:
            for _, seq, ev in self.history:
                if seq > after and ev["type"] == "finished" and ev["session"] in live:
                    latest[ev["session"]] = (seq, ev)
        replay = [dict(ev, replayed=True) for _, ev in sorted(latest.values(), key=lambda pair: pair[0])]
        return [snapshot, *replay, {"type": "checkpoint", "id": self.cursor}]
