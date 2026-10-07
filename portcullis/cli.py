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
    portcullis gui [--hidden] | overlay | status | doctor | daemon | geo-update | geo-status

    portcullis (--profile NAME | --app APP | --override_to_focused)
               [--blockIncoming] [--blockOutgoing] [--allowIncoming] [--allowOutgoing]
               [--toggleIncoming] [--toggleOutgoing] [--incomingLatency MS] [--outgoingLatency MS]
               [--autoUnblockIncoming S] [--autoUnblockOutgoing S] [--keepAlive BYTES]
               [--addPort NAME=PORT[/tcp|udp][@in|out]] [--enablePort|--disablePort|--togglePort|--removePort NAME]
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
            keep = p.get("block_out_above", 0) if d == "out" else 0
            auto = p.get(f"auto_unblock_{d}_s", 0)
            bits.append(f"block {label}" + (f" (UDP up to {keep} bytes still goes out)" if keep else "")
                        + (f" (unblocks after {auto:g}s)" if auto else ""))
        elif p[f"delay_{d}_ms"]:
            bits.append(f"delay {label} {p[f'delay_{d}_ms']} ms")
    off = [x["name"] for x in p.get("ports", []) if not x["enabled"]]
    if off:
        bits.append("ports off: " + ", ".join(off))
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


def parse_port_spec(text: str) -> dict:
    """``voice=3478/udp@out`` -> a port spec (proto defaults to both, direction to both)."""
    name, sep, rest = text.partition("=")
    if not sep or not name.strip():
        raise SystemExit(f"portcullis: --addPort wants NAME=PORT[/tcp|udp][@in|out], got {text!r}")
    rest, _, direction = rest.partition("@")
    num, _, proto = rest.partition("/")
    if not num.strip().isdigit():
        raise SystemExit(f"portcullis: {text!r}: the port must be a number")
    return {"name": name.strip(), "port": int(num), "proto": proto.strip() or "both", "direction": direction.strip() or "both"}


FOCUSED_FLAGS = ("--override_to_focused", "--override-to-focused", "--overrideToFocused")
TARGET_FLAGS = ("--profile", "--app") + FOCUSED_FLAGS


def _is_target_form(argv: "list[str]") -> bool:
    """``portcullis --profile X ...`` / ``--app Y ...`` / ``--override_to_focused ...`` (no subcommand)."""
    return bool(argv) and argv[0].startswith("--") and any(
        a == f or a.startswith(f + "=") for a in argv for f in TARGET_FLAGS)


def _resolve_app(pattern: str) -> str:
    """A full identity (``flatpak:...`` / ``app:...``) as given, otherwise the one running app it matches."""
    if ":" in pattern:
        return pattern.strip()
    return _resolve_running(pattern)


def _profile_main(argv: "list[str]") -> int:
    """``portcullis (--profile NAME | --app APP | --override_to_focused) --blockIncoming --outgoingLatency=250 ...``"""
    p = argparse.ArgumentParser(
        prog="portcullis", allow_abbrev=False,
        description="Change one profile's -- or one app's -- blocking / latency in a single command.",
        epilog="Latency 0 removes the delay.  Changing a profile that has an effect also switches it on.  "
               "--app and --override_to_focused create a profile for the app if it has none yet.")
    tgt = p.add_argument_group("what to change (one of)")
    tgt.add_argument("--profile", metavar="NAME", help="a profile, by name")
    tgt.add_argument("--app", metavar="APP", help="an app: a running app's name (e.g. sober) or an identity "
                                                  "(flatpak:org.vinegarhq.Sober, app:firefox)")
    tgt.add_argument(*FOCUSED_FLAGS, dest="focused", action="store_true",
                     help="the app of the focused window (overrides --profile / --app; needs kdotool)")
    for flag, helptext in (("blockIncoming", "block incoming traffic"), ("blockOutgoing", "block outgoing traffic"),
                           ("allowIncoming", "stop blocking incoming traffic"),
                           ("allowOutgoing", "stop blocking outgoing traffic"),
                           ("toggleIncoming", "flip the incoming block"), ("toggleOutgoing", "flip the outgoing block")):
        p.add_argument(f"--{flag}", dest=flag, action="store_true", help=helptext)
    p.add_argument("--incomingLatency", type=int, metavar="MS", help="fake latency on incoming traffic (0 = none)")
    p.add_argument("--outgoingLatency", type=int, metavar="MS", help="fake latency on outgoing traffic (0 = none)")
    p.add_argument("--autoUnblockIncoming", type=float, metavar="SECONDS",
                   help="switch the incoming block off again this long after it goes on (0 = never)")
    p.add_argument("--autoUnblockOutgoing", type=float, metavar="SECONDS",
                   help="switch the outgoing block off again this long after it goes on (0 = never)")
    p.add_argument("--keepAlive", type=int, metavar="BYTES",
                   help="while outgoing is blocked, still let UDP packets up to BYTES through (acks / pings keep "
                        "the game connected); 0 = block everything")
    p.add_argument("--addPort", action="append", default=[], metavar="NAME=PORT[/tcp|udp][@in|out]",
                   help="name a port for this app, e.g. voice=3478/udp  or  host=7777/udp@in (repeatable)")
    for flag, helptext in (("enablePort", "allow that port's traffic again"), ("disablePort", "block that port's traffic"),
                           ("togglePort", "flip that port"), ("removePort", "forget that port")):
        p.add_argument(f"--{flag}", action="append", default=[], metavar="NAME", help=f"{helptext} (name or number; repeatable)")
    args = p.parse_args(argv)

    changes: dict = {}
    for d, word in (("in", "Incoming"), ("out", "Outgoing")):
        wanted = [n for n in ("block", "allow", "toggle") if getattr(args, f"{n}{word}")]
        if len(wanted) > 1:
            raise SystemExit(f"portcullis: pick one of --block{word}, --allow{word}, --toggle{word}")
        if wanted:
            changes[f"block_{d}"] = {"block": True, "allow": False, "toggle": "toggle"}[wanted[0]]
        ms = getattr(args, f"{word.lower()}Latency")
        if ms is not None:
            if not 0 <= ms <= 5000:
                raise SystemExit(f"portcullis: --{word.lower()}Latency must be between 0 and 5000 ms")
            changes[f"delay_{d}_ms"] = ms
        secs = getattr(args, f"autoUnblock{word}")
        if secs is not None:
            if not 0 <= secs <= 86400:
                raise SystemExit(f"portcullis: --autoUnblock{word} must be between 0 and 86400 seconds")
            changes[f"auto_unblock_{d}_s"] = secs
    if args.keepAlive is not None:
        if not 0 <= args.keepAlive <= 1500:
            raise SystemExit("portcullis: --keepAlive must be between 0 and 1500 bytes")
        changes["block_out_above"] = args.keepAlive
    port_ops = [("add", "", parse_port_spec(t)) for t in args.addPort] + [
        (action, name, None) for flag, action in (("enablePort", "enable"), ("disablePort", "disable"),
                                                  ("togglePort", "toggle"), ("removePort", "remove"))
        for name in getattr(args, flag)]

    # -- who
    identity = None
    if args.focused:
        from . import focus
        try:
            identity, unit = focus.focused_identity()
        except focus.FocusError as e:
            raise SystemExit(f"portcullis: {e}")
    elif args.app:
        identity = _resolve_app(args.app)
    elif not args.profile:
        raise SystemExit("portcullis: say what to change: --profile NAME, --app APP or --override_to_focused")

    profiles = lambda: _call({"cmd": "list"})["profiles"]                         # noqa: E731
    if identity is None:
        def current() -> dict:
            found = [x for x in profiles() if x["name"].lower() == args.profile.lower()]
            if not found:
                raise SystemExit(f"portcullis: no profile named {args.profile!r} "
                                 f"(create one: portcullis add {args.profile} --running APP, or use --app)")
            return found[0]
        name = current()["name"]
        for action, pname, spec in port_ops:
            _call({"cmd": "port", "name": name, "action": action, "port_name": pname, **({"spec": spec} if spec else {})})
        if changes:
            _call({"cmd": "set", "name": name, "changes": changes})
            prof = current()
            if not prof["enabled"] and (any(prof[k] for k in ("block_in", "block_out", "delay_in_ms", "delay_out_ms"))
                                        or any(not x["enabled"] for x in prof.get("ports", []))):
                _call({"cmd": "set", "name": name, "changes": {"enabled": True}})
        prof = current()
    else:
        name = None
        if changes or port_ops:
            name = _call({"cmd": "app", "identity": identity, "changes": changes})["name"]   # creates / switches on
            for action, pname, spec in port_ops:
                _call({"cmd": "port", "identity": identity, "action": action, "port_name": pname,
                       **({"spec": spec} if spec else {})})
        all_ = profiles()
        prof = next((x for x in all_ if x["name"] == name), None) if name else \
            next((x for x in all_ if identity in x["match"] and x["enabled"]), None) or \
            next((x for x in all_ if identity in x["match"]), None)
        if prof is None:
            print(f"{identity}: no profile yet (nothing blocked or delayed)")
            return 0
    who = f" ({identity})" if identity else ""
    print(f"{prof['name']}{who}: {'ACTIVE' if prof['enabled'] else 'off'} - {describe(prof)}")
    for x in prof.get("ports", []):
        where = {"both": "in+out", "in": "incoming", "out": "outgoing"}[x["direction"]]
        print(f"  port {x['name']}: {x['port']}/{x['proto']} {where} - {'enabled' if x['enabled'] else 'DISABLED (blocked)'}")
    for d, word in (("in", "incoming"), ("out", "outgoing")):
        if prof[f"block_{d}"] and prof[f"delay_{d}_ms"]:
            print(f"  note: {word} is blocked, so its {prof[f'delay_{d}_ms']} ms latency has no effect", file=sys.stderr)
    return 0


def _geo_update(file: "str | None" = None) -> int:
    from . import geo
    dest = geo.db_path()
    try:
        used = geo.install_file(file, dest) if file else geo.update(dest, progress=lambda m: print(m, flush=True))
    except Exception as e:  # noqa: BLE001
        print(f"geo-update failed: {e}", file=sys.stderr)
        return 1
    print(f"installed {used}\n{geo.ATTRIBUTION}")
    return 0


def _geo_status() -> int:
    from . import geo
    g = geo.Geo()
    if not g.available:
        print(f"no location database at {geo.db_path()} (run: portcullis geo-update)")
        return 1
    print(f"{geo.db_path()} ({geo.age_days()} days old)\n{geo.ATTRIBUTION}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if _is_target_form(argv):
        return _profile_main(argv)
    p = argparse.ArgumentParser(prog="portcullis", description="Block or delay an app's incoming / outgoing network traffic.")
    p.add_argument("--version", action="version", version=f"portcullis {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list profiles (default)")
    sub.add_parser("apps", help="running apps and the profile covering each")
    sub.add_parser("status", help="profiles with live packet counters")
    sub.add_parser("doctor", help="check what this system supports")
    g = sub.add_parser("gui", help="open the window")
    g.add_argument("--hidden", action="store_true", help="start in the tray only (for autostart)")
    gu = sub.add_parser("geo-update", help="download the offline DB-IP City Lite location database")
    gu.add_argument("--file", metavar="PATH", help="install a database you downloaded yourself (.mmdb or .mmdb.gz)")
    sub.add_parser("geo-status", help="show whether the location database is installed")
    sub.add_parser("overlay", help="the on-screen 'what's blocked' panel (started with your session)")
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
        return gui.run(hidden=args.hidden)
    if cmd == "overlay":
        from .overlay.app import run as run_overlay
        return run_overlay()
    if cmd == "geo-update":
        return _geo_update(args.file)
    if cmd == "geo-status":
        return _geo_status()
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
