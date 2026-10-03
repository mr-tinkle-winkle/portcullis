import pytest

from portcullis import appid, delay, rules
from portcullis.appid import AppCgroup
from portcullis.engine import Engine, QueueManager
from portcullis.profiles import Profile, ProfileStore, StoreError


# ---- identity / matching ----------------------------------------------------------------------------
@pytest.mark.parametrize("unit,ident", [
    ("app-flatpak-org.vinegarhq.Sober-1788577750.scope", "flatpak:org.vinegarhq.Sober"),
    ("app-flatpak-org.vinegarhq.Sober-42.scope", "flatpak:org.vinegarhq.Sober"),          # next launch: same identity
    ("app-firefox-4242.scope", "app:firefox"),
    ("app-org.kde.konsole@3f2a9c.service", "app:org.kde.konsole"),
    ("app-Alacritty@bd8a.service", "app:Alacritty"),
    ("app-my\\x2dgame-77.scope", "app:my-game"),
    ("app-gnome-foo-12.scope", "app:gnome-foo"),
])
def test_identity_strips_the_per_launch_part(unit, ident):
    assert appid.identity(unit) == ident


@pytest.mark.parametrize("unit", ["session-2.scope", "plasma-kwin_wayland.service", "dbus.service", "app.slice"])
def test_non_app_units_have_no_identity(unit):
    assert appid.identity(unit) is None


def _app(unit):
    return AppCgroup(unit, "user.slice/x/" + unit, appid.identity(unit), 1000)


def test_matching_forms():
    sober = _app("app-flatpak-org.vinegarhq.Sober-9.scope")
    assert appid.matches("flatpak:org.vinegarhq.Sober", sober)
    assert appid.matches("FLATPAK:ORG.VINEGARHQ.SOBER", sober)
    assert appid.matches("org.vinegarhq.Sober", sober)                    # bare id
    assert appid.matches("unit:app-flatpak-*Sober*", sober)
    assert not appid.matches("flatpak:org.other.App", sober)
    assert not appid.matches("app:org.vinegarhq.Sober", sober)            # wrong kind
    assert not appid.matches("", sober)


def test_scan_finds_app_units_of_every_user(tmp_path):
    root = tmp_path / "cg"
    for uid, unit in ((1000, "app-flatpak-org.vinegarhq.Sober-5.scope"), (1001, "app-firefox-9.scope")):
        (root / f"user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}").mkdir(parents=True)
    (root / "user.slice/user-1000.slice/user@1000.service/app.slice/dbus.service").mkdir()
    apps = appid.scan(str(root))
    assert [(a.identity, a.uid) for a in apps] == [("flatpak:org.vinegarhq.Sober", 1000), ("app:firefox", 1001)]
    assert apps[0].relpath == "user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.vinegarhq.Sober-5.scope"


# ---- profile store -------------------------------------------------------------------------------------------
def test_store_add_update_persist_and_validate(tmp_path):
    s = ProfileStore(tmp_path / "p.json")
    p = s.add(name="Sober", match=["flatpak:org.vinegarhq.Sober"], delay_out_ms=150)
    assert p.qid == 0 and s.add(name="Other").qid == 1
    with pytest.raises(StoreError):
        s.add(name="sober")                                               # names are unique, case-insensitively
    s.update("SOBER", {"block_in": True, "enabled": "toggle", "delay_in_ms": 99999})
    q = ProfileStore(tmp_path / "p.json").find("sober")                   # survives a restart
    assert q.block_in and q.enabled and q.delay_in_ms == 5000 and q.delay_out_ms == 150
    s.remove("Other")
    assert s.add(name="Third").qid == 2                                   # qids are never reused
    for bad in ({"nope": 1}, {"block_in": "yes"}, {"delay_in_ms": "5"}, {"match": "x"}):
        with pytest.raises(StoreError):
            s.update("Sober", bad)
    with pytest.raises(StoreError):
        s.update("Sober", {"name": "third"})


def test_store_survives_a_corrupt_file(tmp_path):
    f = tmp_path / "p.json"
    f.write_text("{not json")
    assert ProfileStore(f).all() == []


# ---- ruleset ------------------------------------------------------------------------------------------------------
def tgt(unit="app-x-1.scope", **kw):
    p = Profile(name="x", qid=kw.pop("qid", 3), enabled=True, **kw)
    return rules.Target(unit, f"user.slice/user-1000.slice/user@1000.service/app.slice/{unit}", p)


def test_ruleset_blocks_and_delays_per_direction():
    text = rules.build_ruleset([tgt(block_out=True, delay_in_ms=120)], "/sys/fs/cgroup")
    assert 'socket cgroupv2 level 5 "user.slice/user-1000.slice/user@1000.service/app.slice/app-x-1.scope"' in text
    out_part, in_part = text.split("chain in")
    assert "counter queue to 65000" in out_part and "bypass" not in out_part and "drop" not in out_part.replace(":drop", "")
    assert f"queue flags bypass to {rules.qnum(3, 'in')}" in in_part and "drop" not in in_part
    assert text.startswith("table inet portcullis\ndelete table inet portcullis\n")
    assert 'oifname "lo" accept' in text and 'iifname "lo" accept' in text


def test_block_beats_delay_and_no_effect_means_no_rule():
    text = rules.build_ruleset([tgt(block_in=True, delay_in_ms=500)], "/sys/fs/cgroup")
    assert "queue" not in text
    assert "chain" not in rules.build_ruleset([tgt()], "/sys/fs/cgroup")      # just deletes the table


def test_queue_numbers_are_distinct_per_profile_and_direction():
    nums = {rules.qnum(q, d) for q in range(5) for d in ("in", "out")}
    assert len(nums) == 10 and min(nums) >= rules.QUEUE_BASE


def test_hybrid_cgroup_root_prefix_and_levels():
    assert rules.nft_path("a/b", "/sys/fs/cgroup") == "a/b"
    assert rules.nft_path("a/b", "/sys/fs/cgroup/unified") == "unified/a/b"
    assert rules.level("a/b/c") == 3


def test_unsafe_cgroup_paths_are_never_put_in_a_rule():
    t = rules.Target("u", 'a/b"; drop; "', Profile(name="x", enabled=True, block_out=True))
    assert "chain" not in rules.build_ruleset([t], "/sys/fs/cgroup")


def test_backslash_escaped_units_are_kept_literal():
    t = tgt(unit="app-my\\x2dgame-1.scope", block_out=True)
    assert 'app-my\\x2dgame-1.scope"' in rules.build_ruleset([t], "/sys/fs/cgroup")


# ---- engine (fake scan / nft / queues) ---------------------------------------------------------------------------------
class FakeQueues(QueueManager):
    def __init__(self, store):
        super().__init__(store)
        self.log = []

    def ensure(self, wanted):
        self.log.append(("ensure", sorted(wanted)))

    def retire(self, wanted):
        self.log.append(("retire", sorted(wanted)))

    def stop_all(self):
        self.log.append(("stop",))


@pytest.fixture
def eng(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    apps = [_app("app-flatpak-org.vinegarhq.Sober-1.scope"), _app("app-firefox-2.scope")]
    applied = []
    e = Engine(store, scan=lambda root: list(apps), apply=lambda t: (applied.append(t) or (True, "")),
               queues=FakeQueues(store), v2root="/sys/fs/cgroup", counters=lambda: {})
    return e, store, apps, applied


def test_disabled_profile_affects_nothing_then_enabling_applies_to_the_matching_app_only(eng):
    e, store, apps, applied = eng
    store.add(name="Sober", match=["flatpak:org.vinegarhq.Sober"], block_out=True)
    e.step()
    assert "chain" not in applied[-1]                                       # not enabled yet
    store.update("Sober", {"enabled": True})
    assert e.step() is True
    assert applied[-1].count("socket cgroupv2") == 1 and "Sober-1.scope" in applied[-1] and "firefox" not in applied[-1]
    assert e.step() is False and len(applied) == 2                          # unchanged -> not re-applied


def test_new_launch_of_the_same_app_is_picked_up_automatically(eng):
    e, store, apps, applied = eng
    store.add(name="Sober", match=["flatpak:org.vinegarhq.Sober"], enabled=True, delay_in_ms=100)
    e.step()
    apps[0] = _app("app-flatpak-org.vinegarhq.Sober-777.scope")             # Sober restarted: new unit name
    assert e.step() is True and "Sober-777.scope" in applied[-1] and "Sober-1.scope" not in applied[-1]
    assert e.queues.log[-2][0] == "ensure" and e.queues.log[-2][1] == [(0, "in")]
    apps.pop(0)                                                             # Sober closed
    e.step()
    assert "chain" not in applied[-1] and e.queues.log[-1] == ("retire", [])


def test_first_matching_profile_wins_and_status_lists_running_units(eng):
    e, store, apps, applied = eng
    store.add(name="A", match=["firefox"], enabled=True, block_in=True)
    store.add(name="B", match=["app:firefox"], enabled=True, block_out=True)
    e.step()
    assert applied[-1].count("socket cgroupv2") == 1
    st = e.status()
    assert [p["running"] for p in st["profiles"]] == [["app-firefox-2.scope"], ["app-firefox-2.scope"]]
    assert e.apps()[1]["profile"] == "A"


def test_a_rejected_ruleset_is_retried_next_pass_and_reported(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.add(name="S", match=["firefox"], enabled=True, block_out=True)
    results = [(False, "No such file or directory"), (True, "")]
    e = Engine(store, scan=lambda r: [_app("app-firefox-2.scope")], apply=lambda t: results.pop(0),
               queues=FakeQueues(store), v2root="/sys/fs/cgroup", counters=lambda: {})
    assert e.step() is False and "No such file" in e.status()["error"]
    assert e.step() is True and e.status()["error"] == ""


# ---- delay queue core ----------------------------------------------------------------------------------------------------
class Pkt:
    def __init__(self):
        self.accepted = False
        self.retained = False

    def accept(self):
        self.accepted = True

    def retain(self):
        self.retained = True


def test_packets_are_held_for_the_delay_then_released_in_order():
    now, d = [0.0], [100]
    q = delay.DelayQueue(1000, lambda: d[0], clock=lambda: now[0])
    a, b = Pkt(), Pkt()
    q.on_packet(a)
    now[0] = 0.04
    q.on_packet(b)
    assert not a.accepted and q.held == 2
    assert 0.05 < q.timeout() < 0.0601
    now[0] = 0.099
    assert q.release_due() == 0
    now[0] = 0.1
    assert q.release_due() == 1 and a.accepted and not b.accepted
    now[0] = 0.14
    assert q.release_due() == 1 and b.accepted and q.timeout() is None


def test_lowering_the_delay_never_reorders_packets():
    now, d = [0.0], [500]
    q = delay.DelayQueue(1, lambda: d[0], clock=lambda: now[0])
    a, b = Pkt(), Pkt()
    q.on_packet(a)
    d[0] = 0
    now[0] = 0.01
    q.on_packet(b)                                  # would be due at once, but must wait behind a
    now[0] = 0.4
    assert q.release_due() == 0
    now[0] = 0.5
    assert q.release_due() == 2 and a.accepted and b.accepted


def test_zero_delay_passes_straight_through_and_overload_fails_open():
    q = delay.DelayQueue(1, lambda: 0, clock=lambda: 0.0)
    p = Pkt()
    q.on_packet(p)
    assert p.accepted and q.held == 0
    q2 = delay.DelayQueue(1, lambda: 1000, clock=lambda: 0.0, max_held=2)
    pk = [Pkt() for _ in range(3)]
    for x in pk:
        q2.on_packet(x)
    assert pk[2].accepted and not pk[0].accepted                           # over the cap: not held
    q2.release_all()
    assert all(x.accepted for x in pk)


def test_delay_getter_follows_the_profile_live_and_is_zero_when_switched_off(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.add(name="S", delay_out_ms=80, delay_in_ms=30, enabled=True)
    get_out = QueueManager(store)._delay_getter(0, "out")
    get_in = QueueManager(store)._delay_getter(0, "in")
    assert (get_out(), get_in()) == (80, 30)
    store.update("S", {"delay_out_ms": 200})
    assert get_out() == 200
    store.update("S", {"enabled": False})
    assert (get_out(), get_in()) == (0, 0)


def test_a_disabled_profile_does_not_shadow_a_later_enabled_one(eng):
    e, store, apps, applied = eng
    store.add(name="A", match=["firefox"], enabled=False, block_in=True)
    store.add(name="B", match=["firefox"], enabled=True, block_out=True)
    e.step()
    assert applied[-1].count("socket cgroupv2") == 1 and "drop" in applied[-1]
    assert "chain in" not in applied[-1]                                    # B (outgoing only) applies, not A


# -- named ports ------------------------------------------------------------------------------------------
def test_clean_port_validates_and_normalizes():
    from portcullis import decisions
    assert decisions.clean_port({"name": "  voice  chat ", "port": "3478", "proto": "UDP"}) == \
        {"name": "voice chat", "port": 3478, "proto": "udp", "direction": "both", "enabled": True}
    assert decisions.clean_port({"name": "x", "port": 1, "proto": "any"})["proto"] == "both"
    for bad in ({"port": 5}, {"name": "x"}, {"name": "x", "port": 0}, {"name": "x", "port": 70000},
                {"name": "x", "port": True}, {"name": "x", "port": 5, "proto": "icmp"},
                {"name": "x", "port": 5, "direction": "up"}, {"name": "x", "port": 5, "enabled": "yes"}):
        with pytest.raises(ValueError):
            decisions.clean_port(bad)


def test_port_actions_on_a_profile(tmp_path):
    from portcullis.profiles import ProfileStore, StoreError
    st = ProfileStore(tmp_path / "p.json")
    p = st.add(name="game", match=["app:g"], enabled=True)
    st.port_action(p, "add", spec={"name": "Voice", "port": 3478, "proto": "udp", "direction": "out"})
    st.port_action(p, "add", spec={"name": "host", "port": 7777})
    assert [x["enabled"] for x in p.ports] == [True, True]
    st.port_action(p, "disable", "voice")                       # case-insensitive name
    st.port_action(p, "toggle", "7777")                         # or the number
    assert [x["enabled"] for x in p.ports] == [False, False]
    st.port_action(p, "toggle", "host")
    st.port_action(p, "enable", "Voice")
    assert [x["enabled"] for x in p.ports] == [True, True] and p.port_blocks() == []
    st.port_action(p, "add", spec={"name": "VOICE", "port": 3479})   # same name replaces
    assert len(p.ports) == 2 and p.ports[-1]["port"] == 3479
    with pytest.raises(StoreError):
        st.port_action(p, "enable", "nope")
    st.port_action(p, "remove", "host")
    assert [x["name"] for x in p.ports] == ["VOICE"]
    assert ProfileStore(tmp_path / "p.json").find("game").ports == p.ports        # persisted


def test_disabled_ports_become_drop_rules_in_the_right_chains(tmp_path):
    from portcullis.profiles import Profile
    prof = Profile(name="g", qid=3, enabled=True, ports=[
        {"name": "a", "port": 3478, "proto": "udp", "direction": "out", "enabled": False},
        {"name": "b", "port": 7777, "proto": "both", "direction": "in", "enabled": False},
        {"name": "c", "port": 1111, "proto": "tcp", "direction": "both", "enabled": True}]).sanitize()
    text = rules.build_ruleset([rules.Target("app-x.scope", "a/b/app-x.scope", prof)], "/sys/fs/cgroup")
    out_chain, in_chain = text.split("chain in ")[0], text.split("chain in ")[1]
    assert "meta l4proto udp th dport 3478 counter queue to 65000" in out_chain and "th sport 3478" in out_chain
    assert "7777" not in out_chain and "3478" not in in_chain
    assert "meta l4proto { tcp, udp } th dport 7777 counter drop" in in_chain
    assert "1111" not in text                                    # enabled ports emit nothing


def test_outgoing_blocks_are_silent_and_incoming_ones_plain_drops(monkeypatch):
    """A plain drop in the output hook surfaces as EPERM in the app (games then stop receiving as well)."""
    assert rules.drop_stmt("out") == "queue to 65000" and rules.drop_stmt("in") == "drop"
    assert "bypass" not in rules.drop_stmt("out")                 # bypass would let the packets through
    monkeypatch.setattr(rules, "SILENT_OUT", False)
    assert rules.drop_stmt("out") == "drop"
    assert rules.SILENT_DROP_QUEUE not in {rules.qnum(q, d) for q in range(5000) for d in ("in", "out")}


def test_keep_alive_block_lets_small_udp_out_and_nothing_else():
    text = rules.build_ruleset([tgt(block_out=True, block_out_above=100)], "/sys/fs/cgroup")
    out_part = text.split("chain in")[0]
    assert "udp length > 108 counter" in out_part                  # payload > 100 bytes (+8 for the UDP header)
    assert "meta l4proto != udp counter" in out_part               # TCP etc.: still all blocked
    plain = rules.build_ruleset([tgt(block_out=True)], "/sys/fs/cgroup")
    assert "udp length" not in plain
    # the setting only shapes an outgoing *block*; it never applies to incoming or to a delay
    assert "udp length" not in rules.build_ruleset([tgt(block_in=True, block_out_above=100)], "/sys/fs/cgroup")
    assert "udp length" not in rules.build_ruleset([tgt(delay_out_ms=50, block_out_above=100)], "/sys/fs/cgroup")


def test_keep_alive_setting_is_validated(tmp_path):
    st = ProfileStore(tmp_path / "p.json")
    st.add(name="g", match=["app:g"])
    assert st.update("g", {"block_out_above": 120}).block_out_above == 120
    assert st.update("g", {"block_out_above": 99999}).block_out_above == 1500
    with pytest.raises(StoreError):
        st.update("g", {"block_out_above": "lots"})
