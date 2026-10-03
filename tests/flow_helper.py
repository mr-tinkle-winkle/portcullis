"""Runs a real FlowService + Broker inside the current network namespace (used by test_kernel.py
through `nsenter --net=...`).

argv: SECONDS MODE [HOLD_SECONDS]      MODE = observe | ask:DECISION:AFTER_SECONDS | ask:none
Queue 2100 = output hook, 2101 = input hook (the tests' iptables rules feed them)."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from portcullis import ask, flowqueue, flows
from portcullis.profiles import ProfileStore
from portcullis.settings import SettingsStore

seconds, mode = float(sys.argv[1]), sys.argv[2]
hold = int(sys.argv[3]) if len(sys.argv) > 3 else 20
tmp = Path(tempfile.mkdtemp())
store, settings = ProfileStore(tmp / "p.json"), SettingsStore(tmp / "s.json")
if mode != "observe":
    settings.update({"ask_default": True, "hold_seconds": hold})
table = flows.FlowTable()
ref = {}
broker = ask.Broker(owner_for=lambda ident: next((p for p in store.all() if ident in p.match and p.enabled), None),
                    settings=settings.get, add_rule=lambda ident, r: store.set_rule(ident, r),
                    sink=lambda pkt, v: ref["svc"].give(pkt, v))
svc = flowqueue.FlowService(table, broker)
ref["svc"] = svc
svc.start()
svc.sync({2100: flowqueue.FlowMeta("app:t", "app-t-1.scope", "out"), 2101: flowqueue.FlowMeta("app:t", "app-t-1.scope", "in")})

asked = []
if mode.startswith("ask:") and mode != "ask:none":
    _, decision, after = mode.split(":")

    def answerer():
        while True:
            for a in broker.pending():
                if a["id"] not in [x["id"] for x in asked]:
                    asked.append(a)
                    time.sleep(float(after))
                    broker.answer(a["id"], decision)
            time.sleep(0.05)
    threading.Thread(target=answerer, daemon=True).start()

time.sleep(seconds)
pending_at_end = broker.pending()
svc.stop()
print(json.dumps({"remotes": [{k: g[k] for k in ("ip", "direction", "count")} | {"ports": [p["port"] for p in g["ports"]]}
                              for g in table.remotes("app:t")],
                  "rules": store.find("t").rules if store.find("t") else [],
                  "asked": [{k: a[k] for k in ("direction", "ip", "port")} for a in asked],
                  "pending_at_end": len(pending_at_end), "errors": svc.errors}))
