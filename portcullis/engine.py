"""The brain: look at the running apps, decide which profiles apply, keep nftables and the delay
queues in step with that."""
from __future__ import annotations

import logging
import threading

from . import appid, rules
from .delay import DelayQueue
from .profiles import ProfileStore

logger = logging.getLogger("portcullis.engine")


class QueueManager:
    """Owns one DelayQueue thread per wanted NFQUEUE number."""

    def __init__(self, store: ProfileStore, make_queue=DelayQueue):
        self.store = store
        self._make = make_queue
        self.queues: "dict[int, tuple[DelayQueue, threading.Thread]]" = {}

    def _delay_getter(self, qid: int, direction: str):
        def get() -> int:
            p = self.store.by_qid(qid)
            if p is None or not p.enabled:
                return 0
            return p.delay_in_ms if direction == "in" else p.delay_out_ms
        return get

    def ensure(self, wanted: "set[tuple[int, str]]") -> None:
        for qid, direction in wanted:
            n = rules.qnum(qid, direction)
            if n not in self.queues:
                q = self._make(n, self._delay_getter(qid, direction))
                t = threading.Thread(target=q.run, name=f"nfq-{n}", daemon=True)
                self.queues[n] = (q, t)
                t.start()

    def retire(self, wanted: "set[tuple[int, str]]") -> None:
        keep = {rules.qnum(qid, d) for qid, d in wanted}
        for n in [n for n in self.queues if n not in keep]:
            q, t = self.queues.pop(n)
            q.stop()
            t.join(2)

    def stop_all(self) -> None:
        self.retire(set())

    def errors(self) -> "list[str]":
        return [q.error for q, _ in self.queues.values() if q.error]

    def held(self) -> int:
        return sum(q.held for q, _ in self.queues.values())


class Engine:
    def __init__(self, store: ProfileStore, *, scan=appid.scan, apply=rules.apply_ruleset,
                 queues: "QueueManager | None" = None, v2root: "str | None" = None, counters=rules.counters):
        self.store = store
        self._scan, self._apply, self._counters = scan, apply, counters
        self.queues = queues if queues is not None else QueueManager(store)
        self.v2root = v2root or appid.cgroup2_root()
        self.lock = threading.RLock()
        self._last_text = None
        self.error = ""
        self._apps: "list[appid.AppCgroup]" = []
        self._matches: "dict[str, list[appid.AppCgroup]]" = {}

    # -- matching ---------------------------------------------------------------------------------------
    def _match(self, apps) -> "tuple[list[rules.Target], dict]":
        profiles = self.store.all()
        per_profile: dict = {p.name: [] for p in profiles}
        targets = []
        for app in apps:
            claimed = False
            for p in profiles:                         # the first *active* matching profile applies to an app
                if any(appid.matches(m, app) for m in p.match):
                    per_profile[p.name].append(app)
                    if not claimed and p.enabled and p.has_effect():
                        targets.append(rules.Target(app.unit, app.relpath, p))
                        claimed = True
        return targets, per_profile

    def step(self) -> bool:
        """One scan-and-apply pass; returns True if the ruleset changed."""
        with self.lock:
            apps = self._scan(self.v2root)
            targets, per_profile = self._match(apps)
            self._apps, self._matches = apps, per_profile
            text = rules.build_ruleset(targets, self.v2root)
            wanted = {(t.profile.qid, d) for t in targets for d in ("in", "out") if rules.verdict(t.profile, d) == "queue"}
            changed = False
            if text != self._last_text:
                self.queues.ensure(wanted)             # listeners first, then the rules that feed them
                ok, err = self._apply(text)
                if ok:
                    self._last_text, self.error, changed = text, "", True
                    logger.info("rules updated: %d app(s) affected", len(targets))
                else:
                    self.error = err                   # e.g. the app closed between scan and load; retried next pass
                    logger.warning("nft rejected the ruleset: %s", err)
            self.queues.retire(wanted)
            return changed

    def shutdown(self) -> None:
        with self.lock:
            rules.remove_table()
            self._last_text = None
            self.queues.stop_all()

    # -- views --------------------------------------------------------------------------------------------
    def apps(self) -> "list[dict]":
        with self.lock:
            apps = self._scan(self.v2root)
            _, per_profile = self._match(apps)
            owner: dict = {}
            for p in self.store.all():                           # first matching profile (in list order) is shown
                for a in per_profile.get(p.name, []):
                    owner.setdefault(a.unit, p.name)
            return [{"unit": a.unit, "identity": a.identity, "uid": a.uid, "profile": owner.get(a.unit)} for a in apps]

    def status(self) -> dict:
        with self.lock:
            counts = self._counters()
            profiles = []
            for p in self.store.all():
                d = {k: getattr(p, k) for k in ("name", "match", "enabled", "block_in", "block_out", "delay_in_ms", "delay_out_ms")}
                d["running"] = [a.unit for a in self._matches.get(p.name, [])]
                d["counters"] = {dr: counts.get((p.qid, dr)) for dr in ("in", "out")}
                profiles.append(d)
            return {"profiles": profiles, "error": self.error, "queue_errors": self.queues.errors(),
                    "held_packets": self.queues.held()}
