import struct
import threading

import pytest

from portcullis import appid, ask, decisions, flowqueue, flows, rules
from portcullis.engine import Engine
from portcullis.profiles import ProfileStore, StoreError
from portcullis.settings import Settings, SettingsStore
from test_unit import FakeQueues, _app


def ipv4(src, dst, sport, dport, proto=6):
    pk = bytes([0x45, 0, 0, 40, 0, 0, 0, 0, 64, proto, 0, 0]) + bytes(map(int, src.split("."))) + bytes(map(int, dst.split(".")))
    return pk + struct.pack("!HH", sport, dport) + b"\0" * 16


# ---- parsing -------------------------------------------------------------------------------------
def test_parse_ipv4_tcp_and_udp_and_icmp():
    p = flows.parse_packet(ipv4("10.0.0.5", "1.2.3.4", 5555, 443))
    assert (p.proto, p.src, p.dst, p.sport, p.dport) == ("tcp", "10.0.0.5", "1.2.3.4", 5555, 443)
    assert flows.parse_packet(ipv4("10.0.0.5", "1.2.3.4", 5, 53, proto=17)).proto == "udp"
    assert flows.parse_packet(ipv4("10.0.0.5", "1.2.3.4", 0, 0, proto=1)).proto == "icmp"


def test_parse_ipv6_and_garbage():
    import ipaddress
    hdr = bytes([0x60, 0, 0, 0, 0, 20, 17, 64]) + ipaddress.ip_address("fd00::5").packed + ipaddress.ip_address("2001:db8::1").packed
    p = flows.parse_packet(hdr + struct.pack("!HH", 4000, 3478) + b"\0" * 16)
    assert (p.proto, p.dst, p.dport) == ("udp", "2001:db8::1", 3478)
    assert flows.parse_packet(b"") is None and flows.parse_packet(b"\x45\x00") is None and flows.parse_packet(b"\x10" * 30) is None


def test_remote_of_picks_the_far_end_for_each_hook():
    p = flows.parse_packet(ipv4("10.0.0.5", "1.2.3.4", 5555, 443))
    assert flows.remote_of(p, "out") == ("1.2.3.4", 443, 5555)
    assert flows.remote_of(p, "in") == ("10.0.0.5", 5555, 443)        # input hook: the packet's source is the remote


def test_conntrack_parsing_skips_closing_tcp():
    text = ("ipv4 2 tcp 6 431999 ESTABLISHED src=10.0.0.5 dst=1.2.3.4 sport=5555 dport=443 src=1.2.3.4 dst=10.0.0.5 sport=443 dport=5555 [ASSURED]\n"
            "ipv4 2 tcp 6 60 TIME_WAIT src=10.0.0.5 dst=9.9.9.9 sport=1 dport=2 src=9.9.9.9 dst=10.0.0.5 sport=2 dport=1\n"
            "ipv4 2 udp 17 20 src=10.0.0.5 dst=8.8.8.8 sport=4000 dport=53 src=8.8.8.8 dst=10.0.0.5 sport=53 dport=4000\n")
    s = flows.parse_conntrack(text)
    assert ("tcp", "1.2.3.4", 443, 5555) in s and ("udp", "8.8.8.8", 53, 4000) in s
    assert not any(k[1] == "9.9.9.9" for k in s)


def test_flow_table_groups_by_remote_and_tracks_open_flows():
    t = [1000.0]
    ft = flows.FlowTable(clock=lambda: t[0])
    ft.record("app:x", "u1", "out", "tcp", "1.2.3.4", 443, 5555)
    ft.record("app:x", "u1", "out", "tcp", "1.2.3.4", 80, 5556)
    ft.record("app:x", "u1", "in", "udp", "5.6.7.8", 9999, 3478)
    r = ft.remotes("app:x")
    assert {(g["ip"], g["direction"]) for g in r} == {("1.2.3.4", "out"), ("5.6.7.8", "in")}
    g = next(g for g in r if g["ip"] == "1.2.3.4")
    assert g["count"] == 2 and [p["port"] for p in g["ports"]] == [80, 443] or {p["port"] for p in g["ports"]} == {80, 443}
    t[0] += 120
    assert not any(g["active"] for g in ft.remotes("app:x"))               # no conntrack -> recency: stale
    ft.refresh_open({("tcp", "1.2.3.4", 443, 5555)})
    act = {g["ip"]: g["active"] for g in ft.remotes("app:x")}
    assert act == {"1.2.3.4": True, "5.6.7.8": False}
    assert ft.remotes("nobody") == []


def test_flow_table_forgets_old_closed_flows():
    t = [0.0]
    ft = flows.FlowTable(clock=lambda: t[0])
    ft.record("a", "u", "out", "tcp", "1.1.1.1", 1, 2)
    t[0] += flows.FORGET_AFTER + 1
    ft.refresh_open(set())
    assert ft.identities() == []


# ---- rules / decisions -------------------------------------------------------------------------------
def test_clean_rule_validates():
    assert decisions.clean_rule({"ip": "1.2.3.4", "verdict": "block"}) == {"ip": "1.2.3.4", "port": 0, "proto": "", "verdict": "block"}
    assert decisions.clean_rule({"ip": "::1", "port": 443, "proto": "TCP", "verdict": "allow"})["proto"] == "tcp"
    for bad in ({"ip": "nope", "verdict": "block"}, {"ip": "1.2.3.4", "verdict": "maybe"},
                {"ip": "1.2.3.4", "port": 70000, "proto": "tcp", "verdict": "block"},
                {"ip": "1.2.3.4", "port": 80, "verdict": "block"}):
        with pytest.raises(ValueError):
            decisions.clean_rule(bad)


def test_most_specific_rule_wins_and_block_beats_allow():
    rs = [decisions.clean_rule(r) for r in (
        {"ip": "1.2.3.4", "verdict": "block"},
        {"ip": "1.2.3.4", "port": 443, "proto": "tcp", "verdict": "allow"},
        {"ip": "5.5.5.5", "verdict": "allow"}, {"ip": "5.5.5.5", "verdict": "block"})]
    assert decisions.rule_verdict(rs, "1.2.3.4", 80, "tcp") == "block"
    assert decisions.rule_verdict(rs, "1.2.3.4", 443, "tcp") == "allow"       # the port rule is more specific
    assert decisions.rule_verdict(rs, "1.2.3.4", 443, "udp") == "block"
    assert decisions.rule_verdict(rs, "5.5.5.5", 1, "tcp") == "block"
    assert decisions.rule_verdict(rs, "9.9.9.9", 1, "tcp") is None


def test_unaskable_addresses():
    for ip in ("127.0.0.1", "224.0.0.251", "255.255.255.255", "0.0.0.0", "ff02::fb", "::1"):
        assert decisions.is_unaskable(ip)
    for ip in ("8.8.8.8", "192.168.1.1", "2001:db8::1"):
        assert not decisions.is_unaskable(ip)


def test_store_rules_ensure_profile_and_persist(tmp_path):
    st = ProfileStore(tmp_path / "p.json")
    p = st.set_rule("flatpak:org.vinegarhq.Sober", {"ip": "1.2.3.4", "verdict": "block"})
    assert p.name == "Sober" and p.enabled and p.match == ["flatpak:org.vinegarhq.Sober"]
    st.set_rule("flatpak:org.vinegarhq.Sober", {"ip": "1.2.3.4", "verdict": "allow"})          # replaces
    assert st.find("Sober").rules == [{"ip": "1.2.3.4", "port": 0, "proto": "", "verdict": "allow"}]
    st.set_rule("flatpak:org.vinegarhq.Sober", {"ip": "1.2.3.4"}, clear=True)
    assert st.find("Sober").rules == []
    st.set_rule("app:firefox", {"ip": "9.9.9.9", "verdict": "block"})
    assert ProfileStore(tmp_path / "p.json").find("firefox").rules[0]["ip"] == "9.9.9.9"      # persisted
    with pytest.raises(StoreError):
        st.set_rule("app:firefox", {"ip": "not-an-ip", "verdict": "block"})
    with pytest.raises(StoreError):
        st.update("firefox", {"ask": "sometimes"})


def test_ensure_for_reenables_a_switched_off_profile_and_names_dont_clash(tmp_path):
    st = ProfileStore(tmp_path / "p.json")
    st.add(name="Sober", match=["app:other-sober"], enabled=True)
    p = st.ensure_for("flatpak:org.vinegarhq.Sober")
    assert p.name == "Sober 2"
    st.update("Sober 2", {"enabled": False})
    assert st.ensure_for("flatpak:org.vinegarhq.Sober").enabled


def test_settings_validate_clamp_and_persist(tmp_path):
    ss = SettingsStore(tmp_path / "s.json")
    assert ss.get().ask_default is False
    ss.update({"ask_default": True, "hold_seconds": 9999})
    assert SettingsStore(tmp_path / "s.json").get().ask_default is True and ss.get().hold_seconds == 120
    with pytest.raises(StoreError):
        ss.update({"nope": 1})
    with pytest.raises(StoreError):
        ss.update({"ask_default": "yes"})


# ---- the ruleset ---------------------------------------------------------------------------------------
def test_flow_chains_come_before_profile_chains_and_carry_blocks():
    a = _app("app-firefox-1.scope")
    ft = rules.FlowTarget(a.unit, a.relpath, 20001, 20000, (
        {"ip": "1.2.3.4", "port": 0, "proto": "", "verdict": "block"},
        {"ip": "2001:db8::1", "port": 443, "proto": "tcp", "verdict": "block"},
        {"ip": "9.9.9.9", "port": 0, "proto": "", "verdict": "allow"}))
    text = rules.build_ruleset([], "/sys/fs/cgroup", [ft])
    assert "chain out_flows" in text and "chain in_flows" in text and "priority -11" in text
    assert "ip daddr 1.2.3.4 counter queue to 65000" in text and "ip saddr 1.2.3.4 counter drop" in text
    assert "ip6 daddr 2001:db8::1 tcp dport 443 counter queue to 65000" in text and "ip6 saddr 2001:db8::1 tcp sport 443 counter drop" in text
    assert "9.9.9.9" not in text                                   # allow rules never become nft rules
    assert "ct state new queue flags bypass to 20000" in text and "ct state new queue flags bypass to 20001" in text
    # block rules sit before the queue rule of the same app
    out = text.split("chain in_flows")[0]
    assert out.index("queue to 65000") < out.index("queue flags bypass")


def test_no_flow_targets_means_the_old_ruleset_unchanged():
    assert "flows" not in rules.build_ruleset([], "/sys/fs/cgroup")


# ---- the ask broker --------------------------------------------------------------------------------------------
class P:
    def __init__(self):
        self.verdict = None
    def accept(self): self.verdict = "accept"
    def drop(self): self.verdict = "drop"


@pytest.fixture
def broker(tmp_path):
    st = ProfileStore(tmp_path / "p.json")
    ss = SettingsStore(tmp_path / "s.json")
    clock = [100.0]
    given = []
    b = ask.Broker(owner_for=lambda ident: next((p for p in st.all() if ident in p.match and p.enabled), None),
                   settings=ss.get, add_rule=lambda ident, r: st.set_rule(ident, r),
                   sink=lambda pkt, v: (given.append((pkt, v)), getattr(pkt, v)()), clock=lambda: clock[0])
    return b, st, ss, clock, given


def test_without_ask_mode_everything_is_accepted(broker):
    b, *_ = broker
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) == "accept"


def test_ask_mode_holds_unknown_asks_once_and_answers_settle_all_packets(broker):
    b, st, ss, clock, given = broker
    ss.update({"ask_default": True})
    p1, p2 = P(), P()
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, p1) is None
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 80, p2) is None            # same ip, per-ip ask: one question
    q = b.pending()
    assert len(q) == 1 and q[0]["ip"] == "8.8.8.8" and q[0]["port"] == 0 and q[0]["seconds_left"] == 20
    assert b.answer(q[0]["id"], "allow_always") and [v for _, v in given] == ["accept", "accept"]
    assert st.find("x").rules == [{"ip": "8.8.8.8", "port": 0, "proto": "", "verdict": "allow"}]
    assert b.on_flow("app:x", "out", "udp", "8.8.8.8", 53, P()) == "accept"        # the rule now covers it
    assert b.pending() == [] and not b.answer(q[0]["id"], "ignore")                # answering twice does nothing


def test_block_always_stores_a_block_rule_and_drops(broker):
    b, st, ss, *_ = broker
    ss.update({"ask_default": True})
    pk = P()
    b.on_flow("app:x", "in", "tcp", "6.6.6.6", 5000, pk)
    b.answer(b.pending()[0]["id"], "block_always")
    assert pk.verdict == "drop" and st.find("x").rules[0]["verdict"] == "block"
    assert b.on_flow("app:x", "in", "tcp", "6.6.6.6", 1, P()) == "drop"


def test_allow_temp_expires_and_asks_again(broker):
    b, st, ss, clock, _ = broker
    ss.update({"ask_default": True, "temp_allow_minutes": 10})
    b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P())
    b.answer(b.pending()[0]["id"], "allow_temp")
    assert st.find("x") is None                                                  # nothing permanent was stored
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) == "accept"
    clock[0] += 601
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) is None          # asks again


def test_ignore_drops_and_retries_inside_the_quiet_window_do_not_re_ask(broker):
    b, st, ss, clock, _ = broker
    ss.update({"ask_default": True, "quiet_seconds": 5})
    pk = P()
    b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, pk)
    b.answer(b.pending()[0]["id"], "ignore")
    assert pk.verdict == "drop" and st.find("x") is None
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) == "drop" and b.pending() == []
    clock[0] += 6
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) is None and len(b.pending()) == 1


def test_unanswered_questions_expire_dropping_the_packets(broker):
    b, st, ss, clock, _ = broker
    ss.update({"ask_default": True, "hold_seconds": 20})
    pk = P()
    b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, pk)
    clock[0] += 19
    b.expire()
    assert pk.verdict is None and b.pending()
    clock[0] += 2
    b.expire()
    assert pk.verdict == "drop" and b.pending() == []


def test_per_port_ask_mode_asks_per_port_and_rules_carry_the_port(broker):
    b, st, ss, *_ = broker
    ss.update({"ask_default": True, "ask_per_port": True})
    b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P())
    b.on_flow("app:x", "out", "tcp", "8.8.8.8", 80, P())
    assert sorted(a["port"] for a in b.pending()) == [80, 443]
    first = next(a for a in b.pending() if a["port"] == 443)
    b.answer(first["id"], "allow_always")
    assert st.find("x").rules == [{"ip": "8.8.8.8", "port": 443, "proto": "tcp", "verdict": "allow"}]
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) == "accept"
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 8080, P()) is None


def test_per_app_override_beats_the_global_setting_and_local_noise_is_never_asked(broker):
    b, st, ss, *_ = broker
    st.add(name="x", match=["app:x"], enabled=True, ask="allow")
    ss.update({"ask_default": True})
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) == "accept"             # override: allow
    st.update("x", {"ask": "default"})
    assert b.on_flow("app:x", "out", "udp", "224.0.0.251", 5353, P()) == "accept"        # multicast: never a popup
    assert b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, P()) is None
    st.update("x", {"ask": "ask", "enabled": False})
    assert b.on_flow("app:x", "out", "tcp", "1.1.1.1", 443, P()) is None                 # global still applies (no governing profile)


def test_shutdown_lets_held_packets_through(broker):
    b, st, ss, *_ = broker
    ss.update({"ask_default": True})
    pk = P()
    b.on_flow("app:x", "out", "tcp", "8.8.8.8", 443, pk)
    b.release_all()
    assert pk.verdict == "accept" and b.pending() == []


# ---- the flow listener (fake NetfilterQueue) -----------------------------------------------------------------
class FakePkt(P):
    def __init__(self, payload):
        super().__init__()
        self._payload = payload
    def get_payload(self):
        return self._payload


def test_flow_service_records_and_applies_the_broker_verdict(broker):
    b, st, ss, *_ = broker
    table = flows.FlowTable()
    svc = flowqueue.FlowService(table, b)
    meta = flowqueue.FlowMeta("app:x", "app-x-1.scope", "out")
    ss.update({"ask_default": True})
    pk = FakePkt(ipv4("10.0.0.5", "8.8.8.8", 5555, 443))
    svc._on_packet(meta, pk)
    assert table.remotes("app:x")[0]["ip"] == "8.8.8.8" and pk.verdict is None and len(b.pending()) == 1
    ss.update({"ask_default": False})
    junk = FakePkt(b"")
    svc._on_packet(meta, junk)
    assert junk.verdict == "accept"                                                      # unparseable -> never blocked
    ok = FakePkt(ipv4("10.0.0.5", "1.1.1.1", 5556, 443))
    svc._on_packet(meta, ok)
    assert ok.verdict == "accept"


# ---- the engine with flows --------------------------------------------------------------------------------------------
class FakeFlows:
    def __init__(self):
        self.wanted = {}
        self.errors = {}
    def sync(self, wanted):
        self.wanted = dict(wanted)


@pytest.fixture
def feng(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    apps = [_app("app-flatpak-org.vinegarhq.Sober-1.scope"), _app("app-firefox-2.scope")]
    applied = []
    ff, table = FakeFlows(), flows.FlowTable()
    sett = [Settings()]
    eng = Engine(store, scan=lambda r: list(apps), apply=lambda t: (applied.append(t) or (True, "")),
                 queues=FakeQueues(store), v2root="/sys/fs/cgroup", counters=lambda: {},
                 flows=ff, table=table, settings=lambda: sett[0])
    return eng, store, apps, applied, ff, table, sett


def test_every_app_gets_flow_rules_with_its_own_queue_numbers(feng):
    eng, store, apps, applied, ff, *_ = feng
    eng.step()
    t = applied[-1]
    assert t.count("ct state new queue") == 4                                 # 2 apps x (out, in)
    assert len(ff.wanted) == 4 and {m.identity for m in ff.wanted.values()} == {"flatpak:org.vinegarhq.Sober", "app:firefox"}
    nums = sorted(ff.wanted)
    assert len(set(nums)) == 4 and all(n >= flowqueue.FLOW_BASE for n in nums)


def test_tracking_can_be_switched_off_unless_ask_mode_needs_the_queue(feng):
    eng, store, apps, applied, ff, table, sett = feng
    sett[0] = Settings(track_flows=False)
    eng.step()
    assert "flows" not in applied[-1] and ff.wanted == {}
    sett[0] = Settings(track_flows=False, ask_default=True)
    eng.step()
    assert applied[-1].count("ct state new queue") == 4


def test_block_rule_becomes_a_kernel_drop_in_that_apps_chain_only(feng):
    eng, store, apps, applied, ff, *_ = feng
    store.set_rule("app:firefox", {"ip": "1.2.3.4", "verdict": "block"})
    eng.step()
    t = applied[-1]
    ff_lines = [l for l in t.splitlines() if "ip daddr 1.2.3.4" in l]
    assert len(ff_lines) == 1 and "app-firefox-2.scope" in ff_lines[0]


def test_flow_queue_numbers_are_stable_while_an_app_runs_and_reused_afterwards(feng):
    eng, store, apps, applied, ff, *_ = feng
    eng.step()
    before = dict(ff.wanted)
    apps.append(_app("app-new-3.scope"))
    eng.step()
    assert all(k in ff.wanted and ff.wanted[k] == v for k, v in before.items()) and len(ff.wanted) == 6
    apps.pop()
    eng.step()
    assert len(ff.wanted) == 4


def test_overview_lists_apps_remotes_rules_and_settings(feng):
    eng, store, apps, applied, ff, table, sett = feng
    table.record("app:firefox", "app-firefox-2.scope", "out", "tcp", "1.2.3.4", 443, 5555)
    table.record("app:firefox", "app-firefox-2.scope", "out", "tcp", "5.6.7.8", 443, 5556)
    store.set_rule("app:firefox", {"ip": "1.2.3.4", "verdict": "block"})
    store.set_rule("app:firefox", {"ip": "9.9.9.9", "verdict": "block"})          # a remote not seen lately still listed
    store.update("firefox", {"block_out": True})
    eng.step()
    ov = eng.overview()
    fx = next(a for a in ov["apps"] if a["identity"] == "app:firefox")
    ips = {r["ip"]: r for r in fx["remotes"]}
    assert set(ips) == {"1.2.3.4", "5.6.7.8", "9.9.9.9"}
    assert ips["1.2.3.4"]["blocked"] and ips["5.6.7.8"]["rule"] is None and ips["9.9.9.9"]["blocked"]
    assert fx["profile"] == "firefox" and fx["settings"]["block_out"] and fx["running"] and fx["ask"] is False
    sober = next(a for a in ov["apps"] if a["identity"] == "flatpak:org.vinegarhq.Sober")
    assert sober["profile"] is None and "settings" not in sober
    assert ov["settings"]["track_flows"] is True and ov["pending"] == []


# -- the socket table (ss) ----------------------------------------------------------------------------------------------
SS = """tcp ESTAB 0 0 192.168.1.20:51644 160.79.104.10:443 cgroup:/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.vinegarhq.Sober-123.scope
tcp ESTAB 0 0 192.168.1.20:7777 5.6.7.8:40000 cgroup:/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.vinegarhq.Sober-123.scope/sub
tcp LISTEN 0 128 0.0.0.0:7777 0.0.0.0:* cgroup:/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.vinegarhq.Sober-123.scope/sub
tcp ESTAB 0 0 [2001:db8::5]:40100 [2606:4700::1]:443 cgroup:/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox-9.scope
udp ESTAB 0 0 192.168.1.20%wlan0:5353 224.0.0.251:5353 cgroup:/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox-9.scope
tcp ESTAB 0 0 127.0.0.1:5000 127.0.0.1:41000 cgroup:/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox-9.scope
tcp TIME-WAIT 0 0 192.168.1.20:5 9.9.9.9:443
tcp ESTAB 0 0 192.168.1.20:6000 1.2.3.4:443 cgroup:/system.slice/sshd.service
garbage line
"""


class _A:
    def __init__(self, unit, relpath, identity):
        self.unit, self.relpath, self.identity = unit, relpath, identity


def test_parse_ss_reads_addresses_ports_and_cgroups():
    socks = flows.parse_ss(SS)
    assert len(socks) == 7                                         # the TIME-WAIT line has no cgroup, garbage skipped
    v6 = next(s for s in socks if ":" in s.rip)
    assert (v6.lip, v6.lport, v6.rip, v6.rport) == ("2001:db8::5", 40100, "2606:4700::1", 443)
    mdns = next(s for s in socks if s.proto == "udp")
    assert mdns.lip == "192.168.1.20" and mdns.cgroup.endswith("app-firefox-9.scope")


def test_socket_flows_match_apps_by_cgroup_and_work_out_the_direction():
    base = "user.slice/user-1000.slice/user@1000.service/app.slice/"
    apps = [_A("app-flatpak-org.vinegarhq.Sober-123.scope", base + "app-flatpak-org.vinegarhq.Sober-123.scope", "flatpak:org.vinegarhq.Sober"),
            _A("app-firefox-9.scope", base + "app-firefox-9.scope", "app:firefox")]
    got = flows.socket_flows(flows.parse_ss(SS), apps)
    assert ("flatpak:org.vinegarhq.Sober", "app-flatpak-org.vinegarhq.Sober-123.scope", "out", "tcp", "160.79.104.10", 443, 51644) in got
    assert ("flatpak:org.vinegarhq.Sober", "app-flatpak-org.vinegarhq.Sober-123.scope", "in", "tcp", "5.6.7.8", 40000, 7777) in got
    assert ("app:firefox", "app-firefox-9.scope", "out", "tcp", "2606:4700::1", 443, 40100) in got
    assert not [g for g in got if g[4] in ("127.0.0.1", "224.0.0.251", "1.2.3.4")]     # loopback, multicast, not an app


def test_observe_adds_connections_that_predate_the_service_and_keeps_them_open():
    now = [1000.0]
    t = flows.FlowTable(clock=lambda: now[0])
    t.record("app:x", "u", "out", "udp", "9.9.9.9", 53, 4000)          # seen through the packet path
    t.observe([("app:x", "u", "out", "tcp", "1.1.1.1", 443, 5000),
               ("app:x", "u", "in", "udp", "9.9.9.9", 53, 4000)])     # same flow, other way round: not doubled
    t.refresh_open(None)
    rem = {r["ip"]: r for r in t.remotes("app:x")}
    assert set(rem) == {"1.1.1.1", "9.9.9.9"} and rem["1.1.1.1"]["active"] and rem["1.1.1.1"]["direction"] == "out"
    now[0] += 120                                                      # long after: still open while the socket exists
    t.observe([("app:x", "u", "out", "tcp", "1.1.1.1", 443, 5000)])
    t.refresh_open(None)
    rem = {r["ip"]: r for r in t.remotes("app:x")}
    assert rem["1.1.1.1"]["active"] and not rem["9.9.9.9"]["active"]
    t.observe([])                                                      # socket gone
    t.refresh_open(None)
    assert not {r["ip"]: r for r in t.remotes("app:x")}["1.1.1.1"]["active"]
    # with conntrack readable, sockets still count as open
    t.observe([("app:x", "u", "out", "tcp", "1.1.1.1", 443, 5000)])
    t.refresh_open(set())
    assert {r["ip"]: r for r in t.remotes("app:x")}["1.1.1.1"]["active"]
