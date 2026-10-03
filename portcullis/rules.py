"""
The nftables side.  One table, ``inet portcullis``, regenerated whole and loaded atomically
(``nft -f`` is a single transaction), so the rules are never half-applied.

An app's traffic is picked out by the *cgroup of the socket* (``socket cgroupv2``), in the output
hook (the sending socket) and the input hook (the receiving socket).  That is exact -- no ports
or addresses involved -- and covers everything the app does, including DNS and QUIC.

  block  -> ``drop``                                   (in the kernel; no userspace involved)
  delay  -> ``queue flags bypass to N``                (delay.py holds the packets, then accepts)

``bypass`` makes the kernel *accept* queued packets when nothing is listening, so a crash of the
daemon can't cut anyone's network.  Loopback is exempt, so an app's local IPC keeps working.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from dataclasses import dataclass

from .profiles import Profile

TABLE = "portcullis"

# Outgoing packets are dropped by queueing them to a queue nobody listens on (and without ``bypass``): the kernel
# then discards them *silently*.  A plain ``drop`` in the output hook makes the sending call fail with EPERM
# ("Operation not permitted") inside the app -- and games (Roblox among them) treat that as a broken socket and
# stop receiving too, so "block outgoing" would cut both directions.  Windows firewalls drop silently, like this.
SILENT_DROP_QUEUE = 65000
SILENT_OUT = True          # tests on kernels without nft's queue statement switch this off


def drop_stmt(direction: str) -> str:
    """How to discard a packet in this direction (see SILENT_DROP_QUEUE)."""
    return f"queue to {SILENT_DROP_QUEUE}" if direction == "out" and SILENT_OUT else "drop"
QUEUE_BASE = 1000
NFT_CGROUP_ROOT = "/sys/fs/cgroup"                 # nft resolves cgroup paths relative to this
SAFE_PATH = re.compile(r"^[A-Za-z0-9._@:+\-/\\]+$")


@dataclass(frozen=True)
class FlowTarget:
    """An app whose new connections are watched (and, in ask mode, held), plus its per-remote block rules."""
    unit: str
    relpath: str
    queue_in: int
    queue_out: int
    blocks: tuple = ()          # of decisions-style rule dicts with verdict == "block"


def qnum(qid: int, direction: str) -> int:
    return QUEUE_BASE + qid * 2 + (1 if direction == "in" else 0)


@dataclass(frozen=True)
class Target:
    unit: str
    relpath: str            # relative to the cgroup2 root
    profile: Profile


def nft_path(relpath: str, v2root: str) -> str:
    """The path nft must be given: relative to /sys/fs/cgroup (differs from the v2 root only on
    hybrid-cgroup systems, where the v2 hierarchy is mounted below it)."""
    prefix = os.path.relpath(v2root, NFT_CGROUP_ROOT)
    return relpath if prefix == "." else f"{prefix}/{relpath}"


def level(relpath: str) -> int:
    return len([c for c in relpath.split("/") if c])


def verdict(profile: Profile, direction: str) -> "str | None":
    """'drop', 'queue' or None for one direction (a block beats a delay)."""
    if direction == "in":
        blocked, delay = profile.block_in, profile.delay_in_ms
    else:
        blocked, delay = profile.block_out, profile.delay_out_ms
    return "drop" if blocked else "queue" if delay > 0 else None


def _remote_match(rule: dict, direction: str) -> str:
    """The nft match for 'this remote (ip[, port/proto])' as seen in the output or input hook."""
    ip = ipaddress.ip_address(rule["ip"])
    fam = "ip6" if ip.version == 6 else "ip"
    if direction == "out":
        text = f"{fam} daddr {ip}"
        side = "dport"
    else:
        text = f"{fam} saddr {ip}"
        side = "sport"
    if rule.get("port"):
        text += f" {rule['proto']} {side} {int(rule['port'])}"
    return text


def _port_drops(profile: Profile, direction: str) -> "list[str]":
    """Match clauses that drop the profile's switched-off named ports (either end of the connection)."""
    out = []
    for x in profile.port_blocks():
        if x["direction"] not in ("both", direction):
            continue
        proto = "meta l4proto { tcp, udp }" if x["proto"] == "both" else f"meta l4proto {x['proto']}"
        for side in ("dport", "sport"):
            out.append((f"{proto} th {side} {int(x['port'])}", x["port"], side))
    return out


def build_ruleset(targets: "list[Target]", v2root: str, flow_targets: "list[FlowTarget] | tuple" = (),
                  *, exempt_loopback: bool = True) -> str:
    lines = [f"table inet {TABLE}", f"delete table inet {TABLE}"]
    chains = []
    # priority -11: before the profile rules below, so an allowed new flow's first packet still meets
    # any delay / block rule of the app (a queue verdict resumes at the NEXT base chain, not the next rule)
    for chain, hook, ifkey, direction in (("out_flows", "output", "oifname", "out"), ("in_flows", "input", "iifname", "in")):
        rules_ = []
        for ft in flow_targets:
            path = nft_path(ft.relpath, v2root)
            if not SAFE_PATH.match(path):
                continue
            sel = f'socket cgroupv2 level {level(ft.relpath)} "{path}"'
            for b in ft.blocks:
                if b.get("verdict") == "block":
                    rules_.append(f"    {sel} {_remote_match(b, direction)} counter {drop_stmt(direction)}")
            n = ft.queue_out if direction == "out" else ft.queue_in
            rules_.append(f'    {sel} ct state new queue flags bypass to {n} comment "pt:flow"')
        if rules_:
            head = [f"  chain {chain} {{", f"    type filter hook {hook} priority -11; policy accept;"]
            if exempt_loopback:
                head.append(f'    {ifkey} "lo" accept')
            chains.append("\n".join(head + rules_ + ["  }"]))
    for chain, hook, ifkey, direction in (("out", "output", "oifname", "out"), ("in", "input", "iifname", "in")):
        rules = []
        for t in targets:
            v = verdict(t.profile, direction)
            path = nft_path(t.relpath, v2root)
            if not SAFE_PATH.match(path):
                continue
            for clause, port, side in _port_drops(t.profile, direction):
                rules.append(f'    socket cgroupv2 level {level(t.relpath)} "{path}" {clause} counter {drop_stmt(direction)} '
                             f'comment "pt:{t.profile.qid}:{direction}:port{port}{side[0]}"')
            if v is None:
                continue
            comment = f"pt:{t.profile.qid}:{direction}:{v}"
            act = drop_stmt(direction) if v == "drop" else f"queue flags bypass to {qnum(t.profile.qid, direction)}"
            sel = f'socket cgroupv2 level {level(t.relpath)} "{path}"'
            above = t.profile.block_out_above if (direction == "out" and v == "drop") else 0
            if above:
                # "keep alive" block: small UDP packets (acks, pings) still go out, everything bigger does not
                rules.append(f'    {sel} udp length > {int(above) + 8} counter {act} comment "{comment}"')
                rules.append(f'    {sel} meta l4proto != udp counter {act} comment "{comment}"')
            else:
                rules.append(f'    {sel} counter {act} comment "{comment}"')
        if rules:
            head = [f"  chain {chain} {{", f"    type filter hook {hook} priority -10; policy accept;"]
            if exempt_loopback:
                head.append(f'    {ifkey} "lo" accept')
            chains.append("\n".join(head + rules + ["  }"]))
    if chains:
        lines += [f"table inet {TABLE} {{"] + chains + ["}"]
    return "\n".join(lines) + "\n"


def apply_ruleset(text: str, nft: str = "nft") -> "tuple[bool, str]":
    try:
        r = subprocess.run([nft, "-f", "-"], input=text, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.strip()


def remove_table(nft: str = "nft") -> None:
    try:
        subprocess.run([nft, "delete", "table", "inet", TABLE], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def counters(nft: str = "nft") -> "dict[tuple[int, str], dict]":
    """{(profile qid, 'in'|'out'): {'packets': n, 'bytes': n, 'verdict': ...}} from the live table."""
    try:
        r = subprocess.run([nft, "-j", "list", "table", "inet", TABLE], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    out: dict = {}
    for item in data.get("nftables", []):
        rule = item.get("rule")
        if not rule:
            continue
        m = re.fullmatch(r"pt:(\d+):(in|out):(drop|queue)", rule.get("comment", ""))
        if not m:
            continue
        for expr in rule.get("expr", []):
            c = expr.get("counter")
            if c:
                cur = out.setdefault((int(m[1]), m[2]), {"packets": 0, "bytes": 0, "verdict": m[3]})
                cur["packets"] += c.get("packets", 0)
                cur["bytes"] += c.get("bytes", 0)
    return out


def selftest(v2root: str, nft: str = "nft") -> dict:
    """What this kernel / nft supports, by loading throw-away rules."""
    result = {}
    try:
        probe = next(p for p in sorted(os.listdir(v2root)) if os.path.isdir(os.path.join(v2root, p)))
    except (OSError, StopIteration):
        probe = ""
    path = nft_path(probe, v2root) if probe else ""
    cases = {
        "socket_cgroupv2": f'socket cgroupv2 level 1 "{path}" counter accept',
        "queue": "queue flags bypass to 65000",
    }
    for key, rule in cases.items():
        text = (f"table inet {TABLE}_selftest\ndelete table inet {TABLE}_selftest\n"
                f"table inet {TABLE}_selftest {{\n  chain c {{\n    type filter hook output priority 0; policy accept;\n"
                f"    {rule}\n  }}\n}}\n")
        ok, err = apply_ruleset(text, nft)
        result[key] = "ok" if ok else (err.splitlines()[0] if err else "failed")
        subprocess.run([nft, "delete", "table", "inet", f"{TABLE}_selftest"], capture_output=True)
    try:
        import netfilterqueue  # noqa: F401
        result["netfilterqueue"] = "ok"
    except Exception as e:  # noqa: BLE001
        result["netfilterqueue"] = f"import failed: {e}"
    return result
