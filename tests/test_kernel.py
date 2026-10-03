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
