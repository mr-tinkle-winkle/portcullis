"""The brain: look at the running apps, decide which profiles apply, keep nftables and the delay
queues in step with that."""
from __future__ import annotations

import logging
import threading

from . import appid, decisions, flowqueue, rules
from .delay import DelayQueue
from .profiles import ProfileStore
from .settings import Settings

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
                 queues: "QueueManager | None" = None, v2root: "str | None" = None, counters=rules.counters,
                 flows=None, table=None, broker=None, settings=lambda: Settings()):
        """``flows`` (a FlowService), ``table`` (FlowTable) and ``broker`` (ask.Broker) switch on the
        connection tracking / ask mode; without them only blocking and delay run."""
        self.store = store
        self._scan, self._apply, self._counters = scan, apply, counters
        self.flows, self.table, self.broker, self._settings = flows, table, broker, settings
        self._flow_idx: "dict[str, int]" = {}
        self._owner_name: "dict[str, str]" = {}        # identity -> name of the profile governing it
        self.queues = queues if queues is not None else QueueManager(store)
        self.v2root = v2root or appid.cgroup2_root()
        self.lock = threading.RLock()
        self._last_text = None
        self.error = ""
        self._apps: "list[appid.AppCgroup]" = []
        self._matches: "dict[str, list[appid.AppCgroup]]" = {}

    # -- matching ---------------------------------------------------------------------------------------
    def _match(self, apps) -> "tuple[list[rules.Target], dict, dict]":
        """(block/delay targets, {profile name: [apps]}, {unit: governing profile})."""
        profiles = self.store.all()
        per_profile: dict = {p.name: [] for p in profiles}
        targets, owners = [], {}
        for app in apps:
            for p in profiles:             # the first *active* matching profile governs an app
                if any(appid.matches(m, app) for m in p.match):
                    per_profile[p.name].append(app)
                    if app.unit not in owners and p.enabled and p.relevant():
                        owners[app.unit] = p
                        if p.has_effect() or p.port_blocks():
                            targets.append(rules.Target(app.unit, app.relpath, p))
        return targets, per_profile, owners

    def owner_of(self, identity: str):
        """The Profile governing an app identity right now (fresh from the store), or None."""
        name = self._owner_name.get(identity)
        return self.store.find(name) if name else None

    def _alloc_idx(self, relpaths: "list[str]") -> None:
        for rp in [r for r in self._flow_idx if r not in relpaths]:
            del self._flow_idx[rp]
        used = set(self._flow_idx.values())
        for rp in relpaths:
            if rp not in self._flow_idx:
                i = next(i for i in range(flowqueue.MAX_APPS * 4) if i not in used)
                self._flow_idx[rp], _ = i, used.add(i)

    def _flow_targets(self, apps, owners) -> "tuple[list[rules.FlowTarget], dict]":
        if self.flows is None:
            return [], {}
        s = self._settings()
        chosen = [a for a in apps if s.track_flows or decisions.effective_ask(owners.get(a.unit), s)]
        chosen = chosen[:flowqueue.MAX_APPS]
        self._alloc_idx([a.relpath for a in chosen])
        fts, wanted = [], {}
        for a in chosen:
            i = self._flow_idx[a.relpath]
            owner = owners.get(a.unit)
            blocks = tuple(r for r in (owner.rules if owner else []) if r["verdict"] == "block")
            fts.append(rules.FlowTarget(a.unit, a.relpath, flowqueue.qnum(i, "in"), flowqueue.qnum(i, "out"), blocks))
            for d in ("in", "out"):
                wanted[flowqueue.qnum(i, d)] = flowqueue.FlowMeta(a.identity, a.unit, d)
        return fts, wanted

    def step(self) -> bool:
        """One scan-and-apply pass; returns True if the ruleset changed."""
        with self.lock:
            apps = self._scan(self.v2root)
            targets, per_profile, owners = self._match(apps)
            self._apps, self._matches = apps, per_profile
            self._owner_name = {a.identity: owners[a.unit].name for a in apps if a.unit in owners}
            fts, wanted_flows = self._flow_targets(apps, owners)
            text = rules.build_ruleset(targets, self.v2root, fts)
            wanted = {(t.profile.qid, d) for t in targets for d in ("in", "out") if rules.verdict(t.profile, d) == "queue"}
            changed = False
            if text != self._last_text:
                self.queues.ensure(wanted)             # listeners first, then the rules that feed them
                if self.flows is not None:
                    self.flows.sync(wanted_flows)
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
            if self.flows is not None:
                self.flows.sync({})

    # -- views --------------------------------------------------------------------------------------------
    def current_apps(self) -> list:
        """The app cgroups found by the last pass (for matching sockets to apps)."""
        with self.lock:
            return list(self._apps)

    def apps(self) -> "list[dict]":
        with self.lock:
            apps = self._scan(self.v2root)
            _, per_profile, _o = self._match(apps)
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
                d = {k: getattr(p, k) for k in ("name", "match", "enabled", "block_in", "block_out", "delay_in_ms", "delay_out_ms", "ask", "rules", "ports", "block_out_above",
                                                       "auto_unblock_in_s", "auto_unblock_out_s")}
                d["running"] = [a.unit for a in self._matches.get(p.name, [])]
                d["counters"] = {dr: counts.get((p.qid, dr)) for dr in ("in", "out")}
                profiles.append(d)
            return {"profiles": profiles, "error": self.error, "queue_errors": self.queues.errors(),
                    "held_packets": self.queues.held()}

    def blocks(self) -> "list[dict]":
        """What is blocked right now (for the on-screen overlay): one entry per switched-on profile that blocks
        anything -- a whole direction, a named port or single addresses."""
        with self.lock:
            out = []
            for p in self.store.all():
                if not p.enabled:
                    continue
                ports_off = [x["name"] for x in p.ports if not x["enabled"]]
                addresses = sum(1 for r in p.rules if r["verdict"] == "block")
                if not (p.block_in or p.block_out or ports_off or addresses):
                    continue
                out.append({"name": p.name, "match": list(p.match),
                            "running": bool(self._matches.get(p.name)),
                            "in": p.block_in, "out": p.block_out,
                            "in_left": self.store.unblock_left(p, "in"), "out_left": self.store.unblock_left(p, "out"),
                            "keep_alive": p.block_out_above if p.block_out else 0,
                            "ports_off": ports_off, "addresses": addresses})
            return out

    def overview(self) -> dict:
        """Everything the map UI draws, in one call: every app (running, with a profile, or recently
        seen) with its settings, its remotes and what governs each of them."""
        with self.lock:
            s = self._settings()
            apps = self._scan(self.v2root)
            _, per_profile, owners = self._match(apps)
            profiles = {p.name: p for p in self.store.all()}
            by_identity: dict = {}
            for a in apps:
                d = by_identity.setdefault(a.identity, {"units": []})
                d["units"].append(a.unit)
            for p in profiles.values():
                for m in p.match:
                    if ":" in m and not m.lower().startswith("unit:"):
                        by_identity.setdefault(m, {"units": []})
            if self.table is not None:
                for ident in self.table.identities():
                    by_identity.setdefault(ident, {"units": []})
            counts = self._counters()
            out = []
            for ident, d in sorted(by_identity.items()):
                owner = None
                for a in apps:
                    if a.identity == ident and a.unit in owners:
                        owner = owners[a.unit]
                        break
                if owner is None:
                    owner = next((p for p in profiles.values() if ident in p.match and p.enabled), None) or \
                        next((p for p in profiles.values() if ident in p.match), None)
                rules_ = owner.rules if owner else []
                ask = decisions.effective_ask(owner if owner and owner.enabled else None, s)
                remotes = self.table.remotes(ident) if self.table is not None else []
                for r in remotes:
                    r["rule"] = decisions.rule_verdict(rules_, r["ip"], 0, "")
                    for p in r["ports"]:
                        p["rule"] = decisions.rule_verdict([x for x in rules_ if x["port"]], r["ip"], p["port"], p["proto"])
                    r["blocked"] = r["rule"] == "block"
                    r["temp_allowed"] = bool(self.broker and self.broker.temp.allowed(
                        decisions.Temporary.key(ident, r["ip"], 0, "", False)))
                seen = {r["ip"] for r in remotes}
                for rule in rules_:                      # rules for remotes not seen lately stay visible (to undo them)
                    if rule["ip"] not in seen and not rule["port"]:
                        seen.add(rule["ip"])
                        remotes.append({"ip": rule["ip"], "direction": "out", "first": 0, "last": 0, "count": 0,
                                        "active": False, "ports": [], "units": [], "rule": rule["verdict"],
                                        "blocked": rule["verdict"] == "block", "temp_allowed": False})
                entry = {"identity": ident, "units": d["units"], "running": bool(d["units"]),
                         "profile": owner.name if owner else None, "ask": ask, "remotes": remotes}
                if owner:
                    entry["settings"] = {k: getattr(owner, k) for k in
                                         ("enabled", "block_in", "block_out", "delay_in_ms", "delay_out_ms", "ask",
                                          "block_out_above", "auto_unblock_in_s", "auto_unblock_out_s")}
                    entry["rules"] = owner.rules
                    entry["ports"] = owner.ports
                    entry["counters"] = {dr: counts.get((owner.qid, dr)) for dr in ("in", "out")}
                out.append(entry)
            return {"apps": out, "pending": self.broker.pending() if self.broker else [],
                    "settings": vars(s), "error": self.error, "flow_errors": dict(self.flows.errors) if self.flows else {},
                    "temp_allows": [{"key": list(k), "seconds": sec} for k, sec in
                                    (self.broker.temp.allowed_list() if self.broker else [])]}
