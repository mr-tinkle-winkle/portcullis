"""
portcullis -- per-app network gate.

    portcullis                         # list profiles
    portcullis apps                    # running apps, and which profile covers each
    portcullis add NAME --running sober --block-in --delay-out 150
    portcullis add NAME --app flatpak:org.vinegarhq.Sober --enable
    portcullis set NAME [--block-in on|off|toggle] [--block-out ...] [--delay-in MS] [--delay-out MS]
    portcullis on|off|toggle NAME      # the master switch -- bind these to keys / a Stream Deck
    portcullis remove NAME
    portcullis launch NAME -- COMMAND  # start a command in its own app unit (so it can be matched)
    portcullis gui | status | doctor | daemon
"""
from __future__ import annotations

import argparse
import secrets
import subprocess
import sys

from . import __version__, ipc

PAST = {"drop": "dropped", "queue": "delayed"}
ONOFF = {"on": True, "off": False, "toggle": "toggle", "true": True, "false": False}


def _call(cmd: dict) -> dict:
    try:
        reply = ipc.request(cmd)
    except ConnectionError as e:
        print(f"portcullis: {e}", file=sys.stderr)
        raise SystemExit(1)
    if not reply.get("ok"):
        print(f"portcullis: {reply.get('error', 'failed')}", file=sys.stderr)
        raise SystemExit(1)
    return reply


def describe(p: dict) -> str:
    bits = []
    for d, label in (("out", "outgoing"), ("in", "incoming")):
        if p[f"block_{d}"]:
            bits.append(f"block {label}")
        elif p[f"delay_{d}_ms"]:
            bits.append(f"delay {label} {p[f'delay_{d}_ms']} ms")
    return ", ".join(bits) or "no effect set"


def _print_profiles(profiles: "list[dict]") -> None:
    if not profiles:
        print("no profiles yet. Try: portcullis add NAME --running APP --block-out")
        return
    for p in profiles:
        state = "ACTIVE " if p["enabled"] else "off    "
        run = f"  [running: {len(p['running'])}]" if p.get("running") else ""
        print(f"{state}{p['name']:<20} {describe(p)}   (matches {', '.join(p['match']) or 'nothing'}){run}")


def _resolve_running(pattern: str) -> str:
    apps = _call({"cmd": "apps"})["apps"]
    found = sorted({a["identity"] for a in apps if pattern.lower() in a["identity"].lower()})
    if not found:
        raise SystemExit(f"portcullis: no running app matches {pattern!r} (see `portcullis apps`)")
    if len(found) > 1:
        raise SystemExit(f"portcullis: {pattern!r} matches several apps: {', '.join(found)}; be more specific")
    return found[0]


def _changes(args) -> dict:
    ch: dict = {}
    for key, val in (("block_in", args.block_in), ("block_out", args.block_out)):
        if val is not None:
            ch[key] = ONOFF[val]
    for key, val in (("delay_in_ms", args.delay_in), ("delay_out_ms", args.delay_out)):
        if val is not None:
            ch[key] = val
    return ch


def _flag(p: argparse.ArgumentParser, name: str, create: bool) -> None:
    if create:
        p.add_argument(f"--{name}", dest=name.replace("-", "_"), action="store_const", const="on", default=None,
                       help=f"{name.replace('-', ' ')}")
    else:
        p.add_argument(f"--{name}", dest=name.replace("-", "_"), choices=list(ONOFF), default=None)


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="portcullis", description="Block or delay an app's incoming / outgoing network traffic.")
    p.add_argument("--version", action="version", version=f"portcullis {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list profiles (default)")
    sub.add_parser("apps", help="running apps and the profile covering each")
    sub.add_parser("status", help="profiles with live packet counters")
    sub.add_parser("doctor", help="check what this system supports")
    sub.add_parser("gui", help="open the window")
    sub.add_parser("daemon", help="the system service")

    a = sub.add_parser("add", help="create a profile")
    a.add_argument("name")
    a.add_argument("--app", action="append", default=[], metavar="ID", help="app identity (flatpak:ID, app:NAME, unit:GLOB); repeatable")
    a.add_argument("--running", metavar="TEXT", help="use the identity of the running app containing TEXT")
    for f in ("block-in", "block-out"):
        _flag(a, f, True)
    a.add_argument("--delay-in", type=int, metavar="MS")
    a.add_argument("--delay-out", type=int, metavar="MS")
    a.add_argument("--enable", action="store_true", help="switch it on right away")

    s = sub.add_parser("set", help="change a profile")
    s.add_argument("name")
    for f in ("block-in", "block-out"):
        _flag(s, f, False)
    s.add_argument("--delay-in", type=int, metavar="MS")
    s.add_argument("--delay-out", type=int, metavar="MS")
    s.add_argument("--app", action="append", metavar="ID", help="REPLACE the match list")
    s.add_argument("--rename")
    for name in ("on", "off", "toggle", "remove"):
        sub.add_parser(name, help=f"{name} a profile").add_argument("name")
    l = sub.add_parser("launch", help="run a command in its own app unit")
    l.add_argument("name", help="short name; the app becomes identity app:NAME")
    l.add_argument("command", nargs=argparse.REMAINDER)

    args = p.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd == "daemon":
        from . import daemon
        return daemon.run()
    if cmd == "gui":
        from . import gui
        return gui.run()
    if cmd in ("list", "status"):
        st = _call({"cmd": "status"})
        _print_profiles(st["profiles"])
        if cmd == "status":
            for prof in st["profiles"]:
                for d in ("out", "in"):
                    c = (prof["counters"] or {}).get(d)
                    if c:
                        print(f"  {prof['name']} {d}: {PAST.get(c['verdict'], c['verdict'])} {c['packets']} packets ({c['bytes']} bytes)")
        for e in [st.get("error"), *st.get("queue_errors", [])]:
            if e:
                print(f"problem: {e}", file=sys.stderr)
        return 0
    if cmd == "apps":
        for ap in _call({"cmd": "apps"})["apps"]:
            print(f"{ap['identity']:<45} {ap['unit']}" + (f"   <- {ap['profile']}" if ap["profile"] else ""))
        return 0
    if cmd == "doctor":
        r = _call({"cmd": "selftest"})["result"]
        bad = 0
        for k, v in r.items():
            print(f"{k:<18} {v}")
            bad += v != "ok"
        print("\nall good" if not bad else "\nsomething isn't supported -- see above")
        return 1 if bad else 0
    if cmd == "add":
        match = list(args.app)
        if args.running:
            match.append(_resolve_running(args.running))
        fields = {"name": args.name, "match": match, "enabled": args.enable, **_changes(args)}
        _call({"cmd": "add", "fields": fields})
        print(f"added {args.name!r}" + ("" if args.enable else " (switched off: `portcullis on NAME` to apply it)"))
        return 0
    if cmd == "set":
        ch = _changes(args)
        if args.app is not None:
            ch["match"] = args.app
        if args.rename:
            ch["name"] = args.rename
        if not ch:
            raise SystemExit("portcullis: nothing to change")
        _call({"cmd": "set", "name": args.name, "changes": ch})
        return 0
    if cmd in ("on", "off", "toggle"):
        _call({"cmd": "set", "name": args.name, "changes": {"enabled": ONOFF[cmd]}})
        now = [x for x in _call({"cmd": "list"})["profiles"] if x["name"].lower() == args.name.lower()]
        print(f"{args.name}: {'ACTIVE' if now and now[0]['enabled'] else 'off'}")
        return 0
    if cmd == "remove":
        _call({"cmd": "remove", "name": args.name})
        return 0
    if cmd == "launch":
        command = [c for c in args.command if c != "--"] if args.command[:1] == ["--"] else args.command
        if not command:
            raise SystemExit("usage: portcullis launch NAME -- COMMAND [ARGS...]")
        safe = "".join(ch for ch in args.name if ch.isalnum())
        if not safe:
            raise SystemExit("portcullis: the name must contain letters or digits")
        unit = f"app-{safe}-{secrets.randbelow(10**9)}.scope"
        print(f"starting in {unit}  (identity: app:{safe})", file=sys.stderr)
        return subprocess.call(["systemd-run", "--user", "--scope", "--collect", "--slice=app.slice",
                                f"--unit={unit}", "--", *command])
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
