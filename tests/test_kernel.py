"""
Real-kernel tests: network namespaces joined by a veth pair, an app living in a real cgroup, the
real Engine loading real nftables rules.  Skipped unless this runs as root on a Linux with `ip`,
`nft`, cgroup2 and network namespaces (e.g. not in the nix build sandbox).
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import uuid

import pytest

from portcullis import appid, rules
from portcullis.engine import Engine
from portcullis.profiles import ProfileStore
from test_unit import FakeQueues


def _can_run() -> bool:
    if os.geteuid() != 0 or not shutil.which("ip") or not shutil.which("nft"):
        return False
    root = appid.cgroup2_root()
    probe = os.path.join(root, f"pt-probe-{os.getpid()}")
    try:
        os.mkdir(probe)
        os.rmdir(probe)
    except OSError:
        return False
    return subprocess.run(["ip", "netns", "add", "ptprobe"], capture_output=True).returncode == 0 and \
        subprocess.run(["ip", "netns", "del", "ptprobe"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _can_run(), reason="needs root, ip, nft, cgroup2 and network namespaces")

A_ADDR, B_ADDR = "10.77.0.1", "10.77.0.2"


def _nft_has_queue() -> bool:
    probe = ("table inet ptqprobe { chain o { type filter hook output priority 0; "
             "meta mark 0x7fffffff queue to 65000; }; }")
    r = subprocess.run(["nft", "-c", "-f", "-"], input=probe, capture_output=True, text=True)
    return r.returncode == 0


@pytest.fixture(autouse=True)
def _silent_out_where_supported(monkeypatch):
    """This test kernel may lack nft's queue statement; then outgoing blocks are loaded as plain drops (the silent
    variant's kernel behaviour is checked separately with iptables' NFQUEUE, which uses the same queue core)."""
    if not _nft_has_queue():
        monkeypatch.setattr(rules, "SILENT_OUT", False)


def ns(name):
    """Run a command inside a network namespace *without* `ip netns exec` (which remounts /sys and
    would hide the cgroup tree from nft)."""
    return ["nsenter", f"--net=/run/netns/{name}", "--"]


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class Lab:
    def __init__(self, tmp_path):
        tag = uuid.uuid4().hex[:6]
        self.a, self.b = f"pta{tag}", f"ptb{tag}"
        self.va, self.vb = f"va{tag}", f"vb{tag}"
        self.root = appid.cgroup2_root()
        self.cgs = []
        sh("ip", "netns", "add", self.a)
        sh("ip", "netns", "add", self.b)
        sh("ip", "link", "add", self.va, "type", "veth", "peer", "name", self.vb)
        sh("ip", "link", "set", self.va, "netns", self.a)
        sh("ip", "link", "set", self.vb, "netns", self.b)
        for ns, dev, addr in ((self.a, self.va, A_ADDR), (self.b, self.vb, B_ADDR)):
            sh("ip", "netns", "exec", ns, "ip", "addr", "add", f"{addr}/24", "dev", dev)
            sh("ip", "netns", "exec", ns, "ip", "link", "set", dev, "up")
            sh("ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up")
        self.store = ProfileStore(tmp_path / "p.json")
        self.engine = Engine(self.store, apply=self.apply_in_a, queues=FakeQueues(self.store),
                             counters=self.counters_in_a)

    # rules go into namespace A only
    def apply_in_a(self, text):
        r = sh(*ns(self.a), "nft", "-f", "-", input=text)
        return r.returncode == 0, r.stderr.strip()

    def counters_in_a(self):
        import json as j
        r = sh(*ns(self.a), "nft", "-j", "list", "table", "inet", rules.TABLE)
        if r.returncode:
            return {}
        out = {}
        import re
        for item in j.loads(r.stdout).get("nftables", []):
            rule = item.get("rule")
            m = rule and re.fullmatch(r"pt:(\d+):(in|out):(drop|queue)", rule.get("comment", ""))
            if m:
                for e in rule["expr"]:
                    if "counter" in e:
                        out[(int(m[1]), m[2])] = {"packets": e["counter"]["packets"], "bytes": 0, "verdict": m[3]}
        return out

    def make_scope(self, name):
        rel = f"user.slice/user-0.slice/user@0.service/app.slice/{name}"
        path = os.path.join(self.root, rel)
        os.makedirs(path, exist_ok=True)
        self.cgs.append(path)
        return path

    def drop_scope(self, path):
        try:
            os.rmdir(path)
        except OSError:
            pass

    def run_app(self, cgroup=None, mode="send", dest=B_ADDR, port=9000, wait=1.0, count=5):
        """A tiny UDP app in namespace A, optionally inside a cgroup.  Returns what it saw."""
        code = textwrap.dedent(f"""
            import json, os, socket, sys, time
            cg = {cgroup!r}
            if cg: open(os.path.join(cg, "cgroup.procs"), "w").write(str(os.getpid()))
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rx.bind(("0.0.0.0", 9001)); rx.settimeout(0.2)
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sent_ok = 0
            if {mode!r} == "send":
                for i in range({count}):
                    try: tx.sendto(b"x", ({dest!r}, {port})); sent_ok += 1
                    except OSError: pass
                    time.sleep(0.02)
            end = time.time() + {wait}
            got = 0
            while time.time() < end:
                try: rx.recvfrom(10); got += 1
                except socket.timeout: pass
            print(json.dumps({{"sent_ok": sent_ok, "received": got}}))
        """)
        r = sh(*ns(self.a), sys.executable, "-c", code)
        return json.loads(r.stdout)

    def b_listen(self, seconds=2.0):
        code = textwrap.dedent(f"""
            import json, socket, time
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", 9000)); s.settimeout(0.2)
            end = time.time() + {seconds}; n = 0
            while time.time() < end:
                try: s.recvfrom(10); n += 1
                except socket.timeout: pass
            print(json.dumps({{"received": n}}))
        """)
        return subprocess.Popen([*ns(self.b), sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)

    def b_send(self, n=5):
        code = (f"import socket,time\ns=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                f"for i in range({n}):\n s.sendto(b'y', ('{A_ADDR}', 9001)); time.sleep(0.02)\n")
        return subprocess.Popen([*ns(self.b), sys.executable, "-c", code])

    def close(self):
        sh("ip", "netns", "del", self.a)
        sh("ip", "netns", "del", self.b)
        for p in self.cgs:
            self.drop_scope(p)


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    yield lab
    lab.close()


def server_count(lab, **app_kw):
    srv = lab.b_listen(2.0)
    time.sleep(0.2)
    lab.run_app(**app_kw)
    return json.loads(srv.communicate()[0])["received"]


def test_baseline_traffic_flows(lab):
    assert server_count(lab, cgroup=None) == 5


def test_blocking_outgoing_stops_only_the_matching_app(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    lab.store.add(name="T", match=["app:pttest"], enabled=True, block_out=True)
    assert lab.engine.step() is True
    assert server_count(lab, cgroup=scope) == 0                      # the app: blocked
    assert server_count(lab, cgroup=None) == 5                       # any other process: untouched


def test_blocking_incoming_stops_only_the_matching_app(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    lab.store.add(name="T", match=["app:pttest"], enabled=True, block_in=True)
    lab.engine.step()
    import threading
    for cg, expected in ((scope, 0), (None, 5)):
        result = {}
        t = threading.Thread(target=lambda: result.update(lab.run_app(cgroup=cg, mode="listen", wait=2.0)))
        t.start()
        time.sleep(1.0)                                   # let the app start and bind first
        lab.b_send(5).wait()
        t.join()
        assert result["received"] == expected, (cg, result)


def test_directions_are_independent(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    lab.store.add(name="T", match=["app:pttest"], enabled=True, block_in=True)       # incoming only
    lab.engine.step()
    assert server_count(lab, cgroup=scope) == 5                       # outgoing still flows


def test_relaunched_app_with_a_new_unit_name_is_caught_automatically(lab):
    old = lab.make_scope("app-pttest-111.scope")
    lab.store.add(name="T", match=["app:pttest"], enabled=True, block_out=True)
    lab.engine.step()
    lab.drop_scope(old)
    new = lab.make_scope("app-pttest-222.scope")                      # "the app was restarted"
    assert lab.engine.step() is True
    assert server_count(lab, cgroup=new) == 0


def test_switching_off_restores_traffic_and_counters_show_the_drops(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    lab.store.add(name="T", match=["app:pttest"], enabled=True, block_out=True)
    lab.engine.step()
    server_count(lab, cgroup=scope)
    c = lab.engine.status()["profiles"][0]["counters"]["out"]
    assert c["verdict"] == "drop" and c["packets"] >= 5
    lab.store.update("T", {"enabled": False})
    lab.engine.step()
    assert server_count(lab, cgroup=scope) == 5


def test_loopback_is_exempt_so_local_ipc_keeps_working(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    lab.store.add(name="T", match=["app:pttest"], enabled=True, block_out=True, block_in=True)
    lab.engine.step()
    r = lab.run_app(cgroup=scope, dest="127.0.0.1", port=9001, wait=0.6)          # app talks to itself over lo
    assert r["received"] == 5


def test_hybrid_cgroup_root_prefix_is_resolved_by_the_real_nft(lab):
    # on this machine the v2 hierarchy may be mounted below /sys/fs/cgroup; either way the real nft
    # must accept the path the engine generates (a wrong prefix is a load error)
    lab.make_scope("app-pttest-111.scope")
    lab.store.add(name="T", match=["app:pttest"], enabled=True, block_out=True)
    lab.engine.step()
    assert lab.engine.status()["error"] == ""


def test_selftest_reports_what_the_kernel_supports(lab):
    res = rules.selftest(lab.root)
    assert res["socket_cgroupv2"] == "ok"
    assert set(res) == {"socket_cgroupv2", "queue", "netfilterqueue"}


# ---- fake latency on real queued packets ----------------------------------------------------------------------------------
# This kernel has no nft `queue` statement, so these use iptables' NFQUEUE target (matching by port) to feed the
# very same DelayQueue.  Which packets reach the queue (cgroup matching) is covered by the tests above.
HELPER = os.path.join(os.path.dirname(__file__), "delay_helper.py")


def _timed_sender_in(lab, ns_name, dest, port, n=6):
    code = (f"import socket,time,struct\ns=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            f"for i in range({n}):\n s.sendto(struct.pack('!dI', time.time(), i), ('{dest}', {port})); time.sleep(0.03)\n")
    return subprocess.Popen([*ns(ns_name), sys.executable, "-c", code])


def _timed_receiver_in(ns_name, port, seconds):
    code = textwrap.dedent(f"""
        import json, socket, struct, time
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", {port})); s.settimeout(0.1)
        end = time.time() + {seconds}; out = []
        while time.time() < end:
            try:
                d, _ = s.recvfrom(64); ts, i = struct.unpack("!dI", d); out.append((i, time.time() - ts))
            except socket.timeout: pass
        print(json.dumps(out))
    """)
    return subprocess.Popen([*ns(ns_name), sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)


def _iptables(lab, *args):
    r = sh(*ns(lab.a), "iptables", *args)
    assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def queued(lab):
    _iptables(lab, "-A", "OUTPUT", "-p", "udp", "--dport", "9000", "-j", "NFQUEUE", "--queue-num", "2000", "--queue-bypass")
    _iptables(lab, "-A", "INPUT", "-p", "udp", "--dport", "9001", "-j", "NFQUEUE", "--queue-num", "2001", "--queue-bypass")
    return lab


def run_helper(lab, seconds, *specs):
    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))}
    return subprocess.Popen([*ns(lab.a), sys.executable, HELPER, str(seconds), *specs], stdout=subprocess.PIPE, text=True, env=env)


def test_outgoing_packets_are_delayed_by_the_configured_time_in_order_without_loss(queued):
    rx = _timed_receiver_in(queued.b, 9000, 3.0)
    helper = run_helper(queued, 3.0, "2000:250")
    time.sleep(0.8)
    _timed_sender_in(queued, queued.a, B_ADDR, 9000).wait()
    got = json.loads(rx.communicate()[0])
    info = json.loads(helper.communicate()[0])
    assert info["errors"] == [], info
    assert [i for i, _ in got] == list(range(6))                                  # nothing lost, nothing reordered
    for _, latency in got:
        assert 0.24 <= latency <= 0.40, got                                       # 250 ms + a little scheduling jitter


def test_incoming_packets_are_delayed_independently_of_outgoing(queued):
    rx = _timed_receiver_in(queued.a, 9001, 3.0)
    helper = run_helper(queued, 3.0, "2000:400", "2001:120")                      # out 400 ms, in 120 ms
    time.sleep(0.8)
    _timed_sender_in(queued, queued.b, A_ADDR, 9001).wait()
    got = json.loads(rx.communicate()[0])
    helper.communicate()
    assert [i for i, _ in got] == list(range(6))
    for _, latency in got:
        assert 0.11 <= latency <= 0.25, got                                       # the 120 ms incoming delay, not the 400 ms


def test_with_nothing_listening_packets_are_accepted_not_stranded(queued):
    rx = _timed_receiver_in(queued.b, 9000, 1.5)
    time.sleep(0.5)
    _timed_sender_in(queued, queued.a, B_ADDR, 9000).wait()                       # no DelayQueue running at all
    got = json.loads(rx.communicate()[0])
    assert len(got) == 6 and max(l for _, l in got) < 0.1                         # `bypass`: a dead daemon can't cut the network


def test_stopping_the_queue_releases_held_packets(queued):
    rx = _timed_receiver_in(queued.b, 9000, 3.0)
    helper = run_helper(queued, 1.2, "2000:5000")                                 # 5 s delay but the helper stops after 1.2 s
    time.sleep(0.6)
    _timed_sender_in(queued, queued.a, B_ADDR, 9000, n=4).wait()
    info = json.loads(helper.communicate()[0])
    got = json.loads(rx.communicate()[0])
    assert info["released"]["2000"] == 4 and len(got) == 4                        # flushed at shutdown, none dropped


# ---- connection tracking + ask mode (FlowService/Broker on real NFQUEUE traffic) ---------------------------------------
# The nft `queue` statement can't be loaded on every kernel (this sandbox's lacks NFT_QUEUE), so the listener is fed by
# iptables NFQUEUE rules here -- the same queue protocol, the same listener code.
FLOW_HELPER = os.path.join(os.path.dirname(__file__), "flow_helper.py")


@pytest.fixture
def flowlab(lab):
    for chain, qn in (("OUTPUT", "2100"), ("INPUT", "2101")):
        _iptables(lab, "-A", chain, "-p", "tcp", "-m", "conntrack", "--ctstate", "NEW", "-j", "NFQUEUE",
                  "--queue-num", qn, "--queue-bypass")
    return lab


def run_flow_helper(lab, seconds, mode, hold=20):
    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))}
    p = subprocess.Popen([*ns(lab.a), sys.executable, FLOW_HELPER, str(seconds), mode, str(hold)],
                         stdout=subprocess.PIPE, text=True, env=env)
    time.sleep(1.0)
    return p


def tcp_server_in(lab, name, port, seconds):
    code = (f"import socket,time\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            f"s.bind(('0.0.0.0',{port})); s.listen(5); s.settimeout(0.2); end=time.time()+{seconds}\n"
            f"while time.time()<end:\n try: c,_=s.accept(); c.close()\n except socket.timeout: pass\n")
    p = subprocess.Popen([*ns(name), sys.executable, "-c", code])
    time.sleep(0.4)
    return p


def tcp_connect_from(lab, name, dest, port, timeout=6.0):
    code = (f"import socket,time,json\nt=time.time(); s=socket.socket(); s.settimeout({timeout})\n"
            f"try:\n s.connect(('{dest}',{port})); ok=True\nexcept OSError: ok=False\n"
            f"print(json.dumps({{'ok':ok,'secs':round(time.time()-t,2)}}))\n")
    r = sh(*ns(name), sys.executable, "-c", code)
    return json.loads(r.stdout)


def test_observe_mode_records_connections_without_getting_in_the_way(flowlab):
    helper = run_flow_helper(flowlab, 4.0, "observe")
    srv = tcp_server_in(flowlab, flowlab.b, 9100, 3.0)
    res = tcp_connect_from(flowlab, flowlab.a, B_ADDR, 9100)
    srv.wait()
    out = json.loads(helper.communicate()[0])
    assert res["ok"] and res["secs"] < 1.0
    assert out["errors"] == {}, out
    assert out["remotes"] == [{"ip": B_ADDR, "direction": "out", "count": 1, "ports": [9100]}], out


def test_ask_mode_holds_a_new_outgoing_connection_until_allowed(flowlab):
    helper = run_flow_helper(flowlab, 5.0, "ask:allow_always:1.0")
    srv = tcp_server_in(flowlab, flowlab.b, 9100, 4.0)
    res = tcp_connect_from(flowlab, flowlab.a, B_ADDR, 9100)
    out = json.loads(helper.communicate()[0])
    srv.wait()
    assert res["ok"] and 0.9 <= res["secs"] <= 3.0, res                      # waited for the answer, then went through
    assert out["asked"] == [{"direction": "out", "ip": B_ADDR, "port": 0}]
    assert out["rules"] == [{"ip": B_ADDR, "port": 0, "proto": "", "verdict": "allow"}]


def test_ask_mode_block_always_refuses_the_connection(flowlab):
    helper = run_flow_helper(flowlab, 5.0, "ask:block_always:0.5")
    srv = tcp_server_in(flowlab, flowlab.b, 9100, 4.0)
    res = tcp_connect_from(flowlab, flowlab.a, B_ADDR, 9100, timeout=3.0)
    out = json.loads(helper.communicate()[0])
    srv.wait()
    assert not res["ok"], res
    assert out["rules"][0]["verdict"] == "block"


def test_an_unanswered_question_times_out_and_drops(flowlab):
    helper = run_flow_helper(flowlab, 7.0, "ask:none", hold=3)
    srv = tcp_server_in(flowlab, flowlab.b, 9100, 6.0)
    res = tcp_connect_from(flowlab, flowlab.a, B_ADDR, 9100, timeout=2.5)
    assert not res["ok"]                                                       # still waiting when the client gave up
    out = json.loads(helper.communicate()[0])
    srv.wait()
    assert out["errors"] == {}


def test_ask_mode_also_asks_about_new_incoming_connections(flowlab):
    helper = run_flow_helper(flowlab, 6.0, "ask:allow_temp:0.5")
    srv = tcp_server_in(flowlab, flowlab.a, 9101, 4.5)                          # an app in A listening
    res = tcp_connect_from(flowlab, flowlab.b, A_ADDR, 9101)
    out = json.loads(helper.communicate()[0])
    srv.wait()
    assert res["ok"], res
    assert out["asked"] == [{"direction": "in", "ip": B_ADDR, "port": 0}]
    assert out["rules"] == []                                                  # temporary: nothing stored


def test_remote_block_rules_in_the_real_ruleset_drop_only_that_remote_and_only_that_app(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    ft = rules.FlowTarget("app-pttest-111.scope", os.path.relpath(scope, lab.root), 20001, 20000,
                          ({"ip": B_ADDR, "port": 0, "proto": "", "verdict": "block"},))
    text = rules.build_ruleset([], lab.root, [ft])
    text = "\n".join(l for l in text.splitlines() if "queue" not in l)          # (queue not loadable here, see above)
    assert lab.apply_in_a(text) == (True, "")
    assert server_count(lab, cgroup=scope) == 0                                # the app -> that remote: dropped
    assert server_count(lab, cgroup=None) == 5                                 # other processes: fine
    # a different remote of the same app is untouched
    other = rules.build_ruleset([], lab.root, [rules.FlowTarget(ft.unit, ft.relpath, 20001, 20000,
                                ({"ip": "10.77.0.99", "port": 0, "proto": "", "verdict": "block"},))])
    assert lab.apply_in_a("\n".join(l for l in other.splitlines() if "queue" not in l)) == (True, "")
    assert server_count(lab, cgroup=scope) == 5


def test_a_port_rule_blocks_only_that_port(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    for port, expected in ((9000, 0), (9005, 5)):
        ft = rules.FlowTarget("app-pttest-111.scope", os.path.relpath(scope, lab.root), 20001, 20000,
                              ({"ip": B_ADDR, "port": port, "proto": "udp", "verdict": "block"},))
        text = "\n".join(l for l in rules.build_ruleset([], lab.root, [ft]).splitlines() if "queue" not in l)
        assert lab.apply_in_a(text) == (True, "")
        assert server_count(lab, cgroup=scope) == expected, port


def test_incoming_remote_block_stops_that_remote_reaching_the_app(lab):
    import threading
    scope = lab.make_scope("app-pttest-111.scope")
    ft = rules.FlowTarget("app-pttest-111.scope", os.path.relpath(scope, lab.root), 20001, 20000,
                          ({"ip": B_ADDR, "port": 0, "proto": "", "verdict": "block"},))
    text = "\n".join(l for l in rules.build_ruleset([], lab.root, [ft]).splitlines() if "queue" not in l)
    assert lab.apply_in_a(text) == (True, "")
    result = {}
    t = threading.Thread(target=lambda: result.update(lab.run_app(cgroup=scope, mode="listen", wait=2.0)))
    t.start()
    time.sleep(1.0)
    lab.b_send(5).wait()
    t.join()
    assert result["received"] == 0, result


# -- named ports ------------------------------------------------------------------------------------------
from portcullis.profiles import Profile  # noqa: E402


def _port_target(lab, scope, *ports):
    prof = Profile(name="p", qid=1, enabled=True, ports=list(ports)).sanitize()
    return rules.Target("app-pttest-111.scope", os.path.relpath(scope, lab.root), prof)


def _port(port, **kw):
    return {"name": f"p{port}", "port": port, "proto": "udp", "direction": "both", "enabled": False, **kw}


def test_a_disabled_named_port_blocks_only_that_port_for_that_app(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    for port, expected in ((9000, 0), (9005, 5)):
        assert lab.apply_in_a(rules.build_ruleset([_port_target(lab, scope, _port(port))], lab.root)) == (True, "")
        assert server_count(lab, cgroup=scope) == expected, port
        assert server_count(lab, cgroup=None) == 5                           # other apps unaffected


def test_an_enabled_named_port_does_nothing(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    assert lab.apply_in_a(rules.build_ruleset([_port_target(lab, scope, _port(9000, enabled=True))], lab.root)) == (True, "")
    assert server_count(lab, cgroup=scope) == 5


def test_named_port_protocol_and_direction_are_respected(lab):
    scope = lab.make_scope("app-pttest-111.scope")
    # a tcp-only port doesn't touch udp; an incoming-only port doesn't stop outgoing traffic
    for spec in (_port(9000, proto="tcp"), _port(9000, direction="in")):
        assert lab.apply_in_a(rules.build_ruleset([_port_target(lab, scope, spec)], lab.root)) == (True, "")
        assert server_count(lab, cgroup=scope) == 5, spec
    # 'both' protocols and an outgoing-only port do block it
    for spec in (_port(9000, proto="both"), _port(9000, direction="out")):
        assert lab.apply_in_a(rules.build_ruleset([_port_target(lab, scope, spec)], lab.root)) == (True, "")
        assert server_count(lab, cgroup=scope) == 0, spec


def test_a_disabled_incoming_named_port_stops_what_the_app_listens_on(lab):
    import threading
    scope = lab.make_scope("app-pttest-111.scope")
    for spec, expected in ((_port(9001, direction="in"), 0), (_port(9001, direction="out"), 5)):
        assert lab.apply_in_a(rules.build_ruleset([_port_target(lab, scope, spec)], lab.root)) == (True, "")
        result = {}
        t = threading.Thread(target=lambda: result.update(lab.run_app(cgroup=scope, mode="listen", wait=2.0)))
        t.start()
        time.sleep(1.0)
        lab.b_send(5).wait()
        t.join()
        assert result["received"] == expected, (spec, result)


# -- connections that already exist (found through the socket table, not the packet path) ---------------------------
def _ss_in(lab, name):
    """ss inside a namespace, with a pure cgroup2 mount (so it can name the cgroups even on hybrid test hosts)."""
    script = ("umount -l /sys/fs/cgroup 2>/dev/null; mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null; "
              "ss -H -tunaO --cgroup")
    r = sh(*ns(name), "unshare", "-m", "sh", "-c", script)
    return r.stdout


def test_existing_tcp_connections_are_found_with_their_app_and_direction(lab):
    from portcullis import flows
    from portcullis.appid import AppCgroup
    if not shutil.which("ss"):
        pytest.skip("no ss")
    scope = lab.make_scope("app-pttest-222.scope")
    rel = os.path.relpath(scope, lab.root)
    app = AppCgroup("app-pttest-222.scope", rel, "app:pttest")
    srv = subprocess.Popen([*ns(lab.b), sys.executable, "-c",          # a server in B that keeps the connection
                            "import socket,time\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                            "s.bind(('0.0.0.0',9100)); s.listen(5); c,_=s.accept(); time.sleep(6)"])
    time.sleep(0.4)
    code = textwrap.dedent(f"""
        import os, socket, time
        open(os.path.join({scope!r}, "cgroup.procs"), "w").write(str(os.getpid()))
        out = socket.create_connection(({B_ADDR!r}, 9100))         # app -> B   (outgoing)
        lst = socket.socket(); lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lst.bind(("0.0.0.0", 9200)); lst.listen(5)
        print("ready", flush=True)
        conn, _ = lst.accept()                                       # B -> app   (incoming)
        time.sleep(4)
    """)
    p = subprocess.Popen([*ns(lab.a), sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    peer = subprocess.Popen([*ns(lab.b), sys.executable, "-c",
                             f"import socket,time; s=socket.create_connection(('{A_ADDR}',9200)); time.sleep(4)"])
    time.sleep(1.0)
    found = flows.socket_flows(flows.parse_ss(_ss_in(lab, lab.a)), [app])
    p.kill(); peer.kill(); srv.kill()
    by_dir = {(d, rip, rport if d == "out" else lport) for _, _, d, proto, rip, rport, lport in found if proto == "tcp"}
    assert ("out", B_ADDR, 9100) in by_dir, found
    assert ("in", B_ADDR, 9200) in by_dir, found
    assert all(i == "app:pttest" for i, *_ in found)
    # and the table shows them as open connections of that app, although no packet was ever queued
    t = flows.FlowTable()
    t.observe(found)
    t.refresh_open(None)
    rem = {(r["ip"], r["direction"]): r for r in t.remotes("app:pttest")}
    assert rem[(B_ADDR, "out")]["active"] and rem[(B_ADDR, "in")]["active"]



# -- why outgoing blocks are queued, not dropped ----------------------------------------------------------------------
SEND = """
import json, socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
res = []
for i in range(3):
    try:
        s.sendto(b"x" * 100, ("%s", 9000)); res.append("ok")
    except OSError as e:
        res.append(e.errno)
print(json.dumps(res))
"""


def test_a_plain_output_drop_fails_the_apps_send_call_but_an_unheard_queue_drops_silently(lab):
    import errno
    send = SEND % B_ADDR
    srv = lab.b_listen(2.0)
    time.sleep(0.2)
    # plain drop: the app sees EPERM on every send
    assert lab.apply_in_a(f"table inet {rules.TABLE} {{\n chain out {{\n  type filter hook output priority -10;\n"
                          f"  udp dport 9000 drop\n }}\n}}\n") == (True, "")
    r = json.loads(sh(*ns(lab.a), sys.executable, "-c", send).stdout)
    assert r == [errno.EPERM] * 3, r
    sh(*ns(lab.a), "nft", "delete", "table", "inet", rules.TABLE)
    # queue to a queue nobody reads, without bypass (iptables NFQUEUE = the same kernel path as nft's queue)
    assert sh(*ns(lab.a), "iptables", "-A", "OUTPUT", "-p", "udp", "--dport", "9000", "-j", "NFQUEUE",
              "--queue-num", str(rules.SILENT_DROP_QUEUE)).returncode == 0
    r = json.loads(sh(*ns(lab.a), sys.executable, "-c", send).stdout)
    sh(*ns(lab.a), "iptables", "-F", "OUTPUT")
    assert r == ["ok"] * 3, r                                     # the app thinks it sent
    assert json.loads(srv.communicate()[0])["received"] == 0      # ...but nothing left the machine
