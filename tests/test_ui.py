import math
import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from portcullis import daemon, flows, ipc
from portcullis.engine import Engine
from portcullis.profiles import ProfileStore
from portcullis.settings import SettingsStore
from portcullis.ui import config as guicfg
from portcullis.ui import model
from portcullis.ui.bridge import Bridge
from portcullis.ui.mapview import MapWidget, View, arc_control, merc, unmerc
from portcullis.ui.notifier import ACTIONS, Notifier, describe
from test_flows import FakeFlows
from test_unit import FakeQueues, _app
from uifake import FakeGeo, overview, remote

import portcullis.ui_kit as kit


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    kit.set_settings_provider(lambda: guicfg.load_readonly().theme)
    return app


def wait(ms=150):
    # qWait holds the GIL, which would starve the Bridge's worker threads: pump in short slices
    import time
    end = time.time() + ms / 1000
    while time.time() < end:
        QTest.qWait(5)
        time.sleep(0.01)


# ---- model ------------------------------------------------------------------------------------------------------
def test_build_apps_locates_remotes_and_separates_local_ones():
    apps = model.build_apps(overview(), FakeGeo().lookup)
    by = {a.identity: a for a in apps}
    sober = by["flatpak:org.vinegarhq.Sober"]
    assert sober.name == "Sober" and sober.running and sober.allowed
    lan = next(r for r in sober.remotes if r.ip == "192.168.1.1")
    assert lan.local and lan.place is None and lan.where == "Local network"
    blocked = next(r for r in sober.remotes if r.ip == "35.186.224.25")
    assert blocked.blocked and blocked.place.city == "Frankfurt"
    assert not by["app:steam"].allowed                         # both directions blocked -> master Allow is off
    assert [a.identity for a in apps][-1] == "app:discord"     # not running sorts last


def test_pins_merge_remotes_in_the_same_place():
    apps = model.build_apps(overview(), FakeGeo().lookup)
    pins = model.build_pins(apps)
    chicago = next(p for p in pins if p.label.startswith("Chicago"))
    assert len(chicago.items) == 2 and [a.name for a in chicago.apps()] == ["Sober"]
    london = next(p for p in pins if p.label.startswith("London"))
    assert {a.name for a in london.apps()} == {"Sober", "firefox"}
    assert not any(p.label == "Local network" for p in pins) and len(model.local_items(apps)) == 1


def test_signature_ignores_timestamps_but_sees_real_changes():
    a = model.build_apps(overview(), FakeGeo().lookup)
    b = model.build_apps(overview(), FakeGeo().lookup)
    assert model.signature(a, []) == model.signature(b, [])
    b[0].remotes[0].blocked = not b[0].remotes[0].blocked
    assert model.signature(a, []) != model.signature(b, [])
    assert model.signature(a, []) != model.signature(a, [{"id": 1, "identity": "x", "ip": "1.1.1.1", "port": 0}])


# ---- config ---------------------------------------------------------------------------------------------------------
def test_config_roundtrip_defaults_and_bad_values(tmp_path):
    p = tmp_path / "c.toml"
    c = guicfg.load(p)
    assert c.my_lat == guicfg.DEFAULT_LAT and c.theme.color_accent == "#4a4a4a" and not c.advanced_ports
    c.my_lat, c.my_lon, c.advanced_ports = 41.8, -87.9, True
    c.theme.color_accent = "#336699"
    guicfg.save(c, p)
    again = guicfg.load(p)
    assert (again.my_lat, again.my_lon, again.advanced_ports, again.theme.color_accent) == (41.8, -87.9, True, "#336699")
    p.write_text('my_lat = "north"\nadvanced_ports = 3\nwin_w = 5\n[theme]\ncolor_accent = 12\n')
    bad = guicfg.load(p)
    assert bad.my_lat == guicfg.DEFAULT_LAT and bad.advanced_ports is False and bad.win_w == 900 and bad.theme.color_accent == "#4a4a4a"
    p.write_text("not [valid toml")
    assert guicfg.load(p).my_lat == guicfg.DEFAULT_LAT


def test_config_readonly_is_cached_until_the_file_changes(tmp_path):
    p = tmp_path / "c.toml"
    first = guicfg.load_readonly(p)
    assert guicfg.load_readonly(p) is first
    c = guicfg.load(p)
    c.my_lat = 10.0
    guicfg.save(c, p)
    assert guicfg.load_readonly(p) is not first and guicfg.load_readonly(p).my_lat == 10.0


# ---- map widget --------------------------------------------------------------------------------------------------------------
def test_projection_roundtrips_and_mercator_is_invertible():
    v = View(zoom=2.3, cx=15.0, cy=40.0)
    for lon, lat in ((0, 0), (-122.4, 37.8), (139.7, 35.7), (151.2, -33.9)):
        x, y = v.project(lon, lat, 800, 500)
        lo, la = v.unproject(x, y, 800, 500)
        assert math.isclose(lo, lon, abs_tol=1e-6) and math.isclose(la, lat, abs_tol=1e-6)
    assert math.isclose(unmerc(merc(50.0)), 50.0, abs_tol=1e-9)
    assert View().project(0, 18, 800, 500) == (400.0, 250.0)


def test_arc_bows_upwards_between_two_points():
    c = arc_control(QPointF(0, 100), QPointF(200, 100))
    assert c.x() == 100 and c.y() < 100


def _map(qapp, selected=None):
    m = MapWidget()
    m.resize(900, 560)
    m.show()
    apps = model.build_apps(overview(), FakeGeo().lookup)
    m.set_data(model.build_pins(apps), model.local_items(apps), selected)
    m.set_me(41.84, -87.98)
    return m, apps


def test_clicking_a_pin_selects_its_app_and_remote(qapp):
    m, _ = _map(qapp)
    got = []
    m.appSelected.connect(lambda i, ip: got.append((i, ip)))
    tokyo = next(p for p in m.pins if p.label.startswith("Tokyo"))
    c = m._pt(tokyo.lat, tokyo.lon)
    QTest.mouseClick(m, Qt.LeftButton, Qt.NoModifier, c.toPoint())
    assert got == [("app:firefox", "13.107.42.14")]
    QTest.mouseClick(m, Qt.LeftButton, Qt.NoModifier, QPointF(c.x() + 200, c.y() + 5).toPoint())      # empty ocean: nothing
    assert len(got) == 1


def test_a_shared_pin_prefers_the_selected_app(qapp):
    m, _ = _map(qapp, selected="app:firefox")
    got = []
    m.appSelected.connect(lambda i, ip: got.append(i))
    london = next(p for p in m.pins if p.label.startswith("London"))
    QTest.mouseClick(m, Qt.LeftButton, Qt.NoModifier, m._pt(london.lat, london.lon).toPoint())
    assert got == ["app:firefox"]


def test_dragging_my_location_moves_it_and_reports_the_new_place(qapp):
    m, _ = _map(qapp)
    got = []
    m.locationChanged.connect(lambda la, lo: got.append((la, lo)))
    start = m._me_pt().toPoint()
    QTest.mousePress(m, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(m, start + QPointF(150, -40).toPoint())
    QTest.mouseRelease(m, Qt.LeftButton, Qt.NoModifier, start + QPointF(150, -40).toPoint())
    assert len(got) == 1 and got[0][1] > -87.98 + 5 and got[0][0] > 41.84     # moved east and north
    assert m.me == got[0]


def test_wheel_zoom_keeps_the_point_under_the_cursor_and_double_click_resets(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    m, _ = _map(qapp)
    pos = QPointF(300, 200)
    before = m.view.unproject(pos.x(), pos.y(), m.width(), m.height())
    ev = QWheelEvent(pos, m.mapToGlobal(pos), QPoint(0, 0), QPoint(0, 240), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    m.wheelEvent(ev)
    after = m.view.unproject(pos.x(), pos.y(), m.width(), m.height())
    assert m.view.zoom > 1.5 and math.isclose(before[0], after[0], abs_tol=0.5) and math.isclose(before[1], after[1], abs_tol=0.5)
    QTest.mouseDClick(m, Qt.LeftButton, Qt.NoModifier, QPointF(20, 20).toPoint())
    assert m.view.zoom == 1.0


def test_map_paints_without_a_database_or_data(qapp):
    m = MapWidget()
    m.resize(400, 300)
    m.show()
    m.set_hint("No location data yet")
    assert not m.grab().isNull()


# ---- notifier (fake backend) --------------------------------------------------------------------------------------------
from PySide6.QtCore import QObject, Signal


class FakeBackend(QObject):
    actionInvoked = Signal(int, str)
    closed = Signal(int)

    def __init__(self):
        super().__init__()
        self.shown, self.gone, self.n = [], [], 0

    def notify(self, title, body, actions, timeout_ms, replaces=0):
        self.n += 1
        self.shown.append((self.n, title, body, actions, timeout_ms))
        return self.n

    def close(self, nid):
        self.gone.append(nid)


ASK = {"id": 7, "identity": "app:firefox", "direction": "out", "ip": "1.2.3.4", "port": 0, "proto": "", "per_port": False, "seconds_left": 14.0}


def test_describe_reads_naturally():
    t, b = describe(ASK, "Frankfurt, Germany")
    assert t == "firefox wants to connect to 1.2.3.4" and "Frankfurt, Germany" in b and "outgoing" in b
    t2, b2 = describe({**ASK, "direction": "in", "port": 443, "proto": "tcp"})
    assert "contacted by" in t2 and "port 443/tcp" in b2


def test_one_notification_per_question_with_the_four_choices(qapp):
    be = FakeBackend()
    n = Notifier(be, label_for=lambda ip: "Frankfurt, Germany")
    got = []
    n.decided.connect(lambda i, d: got.append((i, d)))
    n.sync([ASK])
    n.sync([ASK])                                              # not shown twice
    assert len(be.shown) == 1
    _, title, body, actions, timeout = be.shown[0]
    assert actions[0::2] == [k for k, _ in ACTIONS] == ["allow_always", "allow_temp", "ignore", "block_always"]
    assert actions[1::2] == ["Always allow", "Allow temporarily", "Ignore", "Always block"] and timeout >= 3000
    be.actionInvoked.emit(1, "block_always")
    be.actionInvoked.emit(99, "ignore")                        # someone else's notification: ignored
    be.actionInvoked.emit(1, "format_disk")                    # not one of ours: ignored
    assert got == [(7, "block_always")]
    n.sync([])
    assert be.gone == [1]


def test_a_dismissed_notification_is_not_shown_again_and_disabled_closes_all(qapp):
    be = FakeBackend()
    n = Notifier(be)
    n.sync([ASK])
    be.closed.emit(1)
    n.sync([ASK])
    assert len(be.shown) == 1
    n.sync([ASK, {**ASK, "id": 8}])
    assert len(be.shown) == 2
    n.sync([ASK, {**ASK, "id": 8}], enabled=False)
    assert 2 in be.gone


def test_dbus_backend_degrades_quietly_without_a_session_bus(qapp, monkeypatch):
    from portcullis.ui.notifier import DBusBackend
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
    b = DBusBackend()
    assert b.notify("t", "b", [], 1000) == 0
    b.close(5)


# ---- the window against a fake bridge ------------------------------------------------------------------------------------------------
class Recorder:
    def __init__(self, ov=None):
        self.ov = ov or overview()
        self.sent = []
        self.lock = threading.Lock()

    def __call__(self, cmd, *a, **k):
        if cmd["cmd"] == "overview":
            return dict(self.ov)
        with self.lock:
            self.sent.append(cmd)
        return {"ok": True}


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from portcullis.ui.window import MainWindow
    rec = Recorder()
    w = MainWindow(bridge=Bridge(request=rec), geo=FakeGeo(), cfg=guicfg.load(), poll_ms=10 ** 6)
    w.resize(1500, 860)
    w.show()
    w.bridge.poll()
    wait(300)
    yield w, rec
    w.shutdown()
    w.close()


def sent(rec, token=None):
    wait(250)
    return list(rec.sent)


def test_window_lists_apps_and_selecting_one_fills_the_connection_panel(win):
    w, rec = win
    assert [a.name for a in w.apps] == ["firefox", "Sober", "steam", "discord"]
    assert len(w.applist._rows) == 4 and w.detail.app is None
    w.applist._rows["flatpak:org.vinegarhq.Sober"].clicked.emit("flatpak:org.vinegarhq.Sober")
    assert w.selected == "flatpak:org.vinegarhq.Sober" and w.detail.app.name == "Sober"
    assert w.map.selected == "flatpak:org.vinegarhq.Sober"
    texts = [lab.text() for lab in w.detail.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)]
    assert "128.116.21.3" in texts and "104.18.2.35" in texts and any("Outgoing connections (4)" in t for t in [g.title() for g in w.detail.findChildren(kit.CustomGroupBox)])


def test_clicking_a_pin_selects_the_app_and_focuses_that_remote(win):
    w, rec = win
    w.on_pin_selected("app:firefox", "13.107.42.14")
    assert w.selected == "app:firefox" and w.detail.focus_ip == "13.107.42.14"
    rows = w.detail.findChildren(__import__("portcullis.ui.widgets", fromlist=["ConnectionRow"]).ConnectionRow)
    assert [r.ip for r in rows if r.highlighted] == ["13.107.42.14"]


def test_each_connection_has_an_allow_toggle_on_by_default_that_sends_a_rule(win):
    from portcullis.ui.widgets import ConnectionRow
    w, rec = win
    w.select_app("app:firefox")
    rows = {r.ip: r for r in w.detail.findChildren(ConnectionRow)}
    assert all(r.allow.isChecked() for r in rows.values())                       # all allowed by default
    rows["104.18.2.35"].allow.setChecked(False)
    assert {"cmd": "rule", "identity": "app:firefox", "ip": "104.18.2.35", "verdict": "block"} in sent(rec)
    rows["104.18.2.35"].allow.setChecked(True)
    assert {"cmd": "rule", "identity": "app:firefox", "ip": "104.18.2.35", "verdict": "clear"} in sent(rec)


def test_in_ask_mode_allowing_a_connection_stores_an_explicit_allow(win):
    from portcullis.ui.widgets import ConnectionRow
    w, rec = win
    w.select_app("app:steam")
    row = next(r for r in w.detail.findChildren(ConnectionRow) if r.ip == "52.95.1.1")
    row.allow.setChecked(False)
    row.allow.setChecked(True)
    cmds = sent(rec)
    assert {"cmd": "rule", "identity": "app:steam", "ip": "52.95.1.1", "verdict": "allow"} in cmds


def test_blocked_connections_show_unchecked(win):
    from portcullis.ui.widgets import ConnectionRow
    w, rec = win
    w.select_app("flatpak:org.vinegarhq.Sober")
    rows = {r.ip: r for r in w.detail.findChildren(ConnectionRow)}
    assert not rows["35.186.224.25"].allow.isChecked() and rows["128.116.21.3"].allow.isChecked()


def test_advanced_mode_adds_per_port_toggles(win):
    from portcullis.ui.widgets import ConnectionRow
    w, rec = win
    w.select_app("flatpak:org.vinegarhq.Sober")
    assert all(r.toggle_btn is None for r in w.detail.findChildren(ConnectionRow))      # simple mode: no port rows
    w.settings_page.advanced.setChecked(True)
    wait(100)
    rows = {r.ip: r for r in w.detail.findChildren(ConnectionRow)}
    row = rows["128.116.21.3"]
    assert row.toggle_btn is not None and row.ports_box is not None
    row.toggle_btn.click()
    assert not row.ports_box.isHidden()
    boxes = row.ports_box.findChildren(kit.CustomCheckBox)
    assert len(boxes) == 2
    boxes[1].setChecked(False)                                  # the second port: tcp 443
    assert {"cmd": "rule", "identity": "flatpak:org.vinegarhq.Sober", "ip": "128.116.21.3", "verdict": "block",
            "port": 443, "proto": "tcp"} in sent(rec)
    assert guicfg.load().advanced_ports is True                 # persisted


def _caption_box(root, text):
    from portcullis.ui.widgets import ClickLabel
    return next(l for l in root.findChildren(ClickLabel) if l.text() == text)._box


def test_master_allow_and_traffic_rules_send_app_changes(win):
    w, rec = win
    w.select_app("flatpak:org.vinegarhq.Sober")
    w.applist._rows["flatpak:org.vinegarhq.Sober"].allow.setChecked(False)
    assert {"cmd": "app", "identity": "flatpak:org.vinegarhq.Sober", "changes": {"block_in": True, "block_out": True}} in sent(rec)
    block_out = _caption_box(w.detail, "Block outgoing")
    block_out.setChecked(True)
    assert {"cmd": "app", "identity": "flatpak:org.vinegarhq.Sober", "changes": {"block_out": True}} in sent(rec)
    spin = next(s for s in w.detail.findChildren(kit.CustomSpinBox) if s.value() == 120)
    spin.setValue(300)
    spin.editingFinished.emit()
    assert {"cmd": "app", "identity": "flatpak:org.vinegarhq.Sober", "changes": {"delay_out_ms": 300}} in sent(rec)
    combo = w.detail.findChildren(__import__("PySide6.QtWidgets", fromlist=["QComboBox"]).QComboBox)[0]
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    assert {"cmd": "app", "identity": "flatpak:org.vinegarhq.Sober", "changes": {"ask": "ask"}} in sent(rec)


def test_pending_banner_and_panel(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from portcullis.ui.window import MainWindow
    pend = [{**ASK, "id": 3}, {**ASK, "id": 4, "ip": "5.6.7.8", "direction": "in"}]
    rec = Recorder(overview(pending=pend))
    w = MainWindow(bridge=Bridge(request=rec), geo=FakeGeo(), cfg=guicfg.load(), poll_ms=10 ** 6, notifier=Notifier(FakeBackend()))
    w.show()
    w.bridge.poll()
    wait(300)
    assert "2 connections waiting" in w.banner.label.text() and not w.banner.button.isHidden()
    assert len(w.notifier.backend.shown) == 2
    w.show_pending()
    from portcullis.ui.widgets import CustomButton
    btns = [b for b in w.pending_dialog.panel.findChildren(CustomButton)]
    assert [b.text() for b in btns[:4]] == ["Always allow", "Allow temporarily", "Ignore", "Always block"]
    btns[1].click()
    assert {"cmd": "answer", "id": 3, "decision": "allow_temp"} in sent(rec)
    w.notifier.backend.actionInvoked.emit(2, "block_always")                       # the notification's button
    assert {"cmd": "answer", "id": 4, "decision": "block_always"} in sent(rec)
    w.shutdown()
    w.close()


def test_banner_explains_when_the_service_is_down(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from portcullis.ui.window import MainWindow

    def refuse(cmd, *a, **k):
        raise ConnectionError("the portcullis service isn't running (/run/portcullis/control.sock doesn't exist)")

    w = MainWindow(bridge=Bridge(request=refuse), geo=FakeGeo(), cfg=guicfg.load(), poll_ms=10 ** 6)
    w.show()
    w.bridge.poll()
    wait(300)
    assert "isn't running" in w.banner.label.text()
    w.shutdown()
    w.close()


def test_dragging_my_location_is_saved(win):
    w, rec = win
    w.map.locationChanged.emit(48.85, 2.35)
    c = guicfg.load()
    assert (c.my_lat, c.my_lon) == (48.85, 2.35) and (w.settings_page.lat.value(), w.settings_page.lon.value()) == (48.85, 2.35)


def test_settings_page_changes_daemon_settings_but_loading_them_does_not_echo(win):
    w, rec = win
    sp = w.settings_page
    n = len(sent(rec))
    sp.load_daemon_settings({"ask_default": True, "ask_per_port": True, "track_flows": False, "temp_allow_minutes": 30,
                             "hold_seconds": 40, "quiet_seconds": 9})
    assert sp.ask_default.isChecked() and sp.hold_seconds.value() == 40 and len(sent(rec)) == n      # no echo
    sp.ask_default.setChecked(False)
    assert {"cmd": "settings", "changes": {"ask_default": False}} in sent(rec)


def test_geo_update_button_runs_the_download_off_the_ui_thread_and_reports(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from portcullis.ui.window import MainWindow
    calls = []
    w = MainWindow(bridge=Bridge(request=Recorder()), geo=FakeGeo(), cfg=guicfg.load(), poll_ms=10 ** 6,
                   downloader=lambda: calls.append(threading.current_thread().name))
    w.show()
    w.settings_page.geo_button.click()
    assert "Downloading" in w.settings_page.geo_status.text() and not w.settings_page.geo_button.isEnabled()
    wait(900)
    assert calls and calls[0] != threading.main_thread().name and w.settings_page.geo_button.isEnabled()
    w.shutdown()
    w.close()


def test_no_unscoped_stylesheets_anywhere_in_the_window(win):
    """Kit pitfall 2: an unscoped setStyleSheet cascades into every child and breaks custom painting."""
    w, rec = win
    w.select_app("app:firefox")
    from PySide6.QtWidgets import QWidget
    for widget in [w] + w.findChildren(QWidget):
        css = widget.styleSheet().strip()
        assert not css or css.lstrip().split("{")[0].strip() != "" and "{" in css, (type(widget).__name__, css)


def test_window_has_no_oversized_minimum_so_it_can_shrink(win):
    w, rec = win
    assert w.minimumSizeHint().height() < 700 and w.minimumSizeHint().width() < 1200


# ---- end to end: window <-> real control server <-> real engine/store -------------------------------------------------------------------
@pytest.fixture
def live(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    store, sett = ProfileStore(tmp_path / "p.json"), SettingsStore(tmp_path / "s.json")
    table = flows.FlowTable()
    table.record("app:firefox", "app-firefox-2.scope", "out", "tcp", "142.250.80.46", 443, 5555)
    table.record("app:firefox", "app-firefox-2.scope", "out", "tcp", "104.18.2.35", 443, 5556)
    apps = [_app("app-firefox-2.scope")]
    applied = []
    ff = FakeFlows()
    eng = Engine(store, scan=lambda r: list(apps), apply=lambda t: (applied.append(t) or (True, "")), queues=FakeQueues(store),
                 v2root="/sys/fs/cgroup", counters=lambda: {}, flows=ff, table=table, settings=sett.get)
    ctl = daemon.Controller(store, eng, threading.Event(), sett, None)
    srv = ipc.ControlServer(ipc.socket_path(), ctl)
    srv.serve_in_thread()
    from portcullis.ui.window import MainWindow
    w = MainWindow(bridge=Bridge(), geo=FakeGeo(), cfg=guicfg.load(), poll_ms=10 ** 6)
    w.resize(1400, 800)
    w.show()
    w.bridge.poll()
    wait(400)
    yield w, store, sett, applied
    w.shutdown()
    w.close()
    srv.shutdown()
    srv.server_close()


def test_toggling_a_connection_in_the_window_blocks_that_address_in_the_ruleset(live):
    from portcullis.ui.widgets import ConnectionRow
    w, store, sett, applied = live
    w.select_app("app:firefox")
    row = next(r for r in w.detail.findChildren(ConnectionRow) if r.ip == "104.18.2.35")
    assert row.allow.isChecked()
    row.allow.setChecked(False)
    wait(500)
    assert store.find("firefox").rules == [{"ip": "104.18.2.35", "port": 0, "proto": "", "verdict": "block"}]
    assert "ip daddr 104.18.2.35" in applied[-1] and "ip saddr 104.18.2.35" in applied[-1]
    row2 = next(r for r in w.detail.findChildren(ConnectionRow) if r.ip == "104.18.2.35")      # the window re-read it
    assert not row2.allow.isChecked()
    assert next(a for a in w.apps if a.identity == "app:firefox").blocked_count == 1
    row2.allow.setChecked(True)
    wait(500)
    assert store.find("firefox").rules == [] and "104.18.2.35" not in applied[-1]


def test_master_allow_off_blocks_both_directions_and_on_restores(live):
    w, store, sett, applied = live
    w.applist._rows["app:firefox"].allow.setChecked(False)
    wait(500)
    p = store.find("firefox")
    assert p.enabled and p.block_in and p.block_out and "drop" in applied[-1]
    w.applist._rows["app:firefox"].allow.setChecked(True)
    wait(500)
    p = store.find("firefox")
    assert not p.block_in and not p.block_out


def test_flipping_never_allow_by_default_in_settings_reaches_the_daemon(live):
    w, store, sett, applied = live
    w.settings_page.ask_default.setChecked(True)
    wait(500)
    assert sett.get().ask_default is True
    w.settings_page.hold_seconds.setValue(45)
    w.settings_page.hold_seconds.editingFinished.emit()
    wait(500)
    assert sett.get().hold_seconds == 45


# -- named ports + signal colours ---------------------------------------------------------------------------
def _select_sober(w):
    w.select_app("flatpak:org.vinegarhq.Sober")
    return w.detail


def test_named_ports_are_listed_with_their_state(win):
    w, rec = win
    d = _select_sober(w)
    from PySide6.QtWidgets import QLabel
    texts = [l.text() for l in d.findChildren(QLabel)]
    assert "Named ports (2)" in [g.title() for g in d.findChildren(kit.CustomGroupBox)]
    assert "voice" in texts and "3478/udp · outgoing" in texts
    assert "host" in texts and "7777/tcp+udp · incoming" in texts


def test_port_toggle_remove_and_add_send_port_commands(win):
    from PySide6.QtWidgets import QComboBox
    w, rec = win
    d = _select_sober(w)
    ident = "flatpak:org.vinegarhq.Sober"
    d.portRequested.emit(ident, "disable", "voice", {})
    assert {"cmd": "port", "identity": ident, "action": "disable", "port_name": "voice"} in sent(rec)
    # the real checkbox of the 'host' row (currently off) -> enable
    from portcullis.ui.widgets import StateCheckBox
    boxes = [b for b in d.findChildren(StateCheckBox) if b.toolTip().startswith("Enabled:")]
    assert [b.isChecked() for b in boxes] == [True, False]
    boxes[1].setChecked(True)
    assert {"cmd": "port", "identity": ident, "action": "enable", "port_name": "host"} in sent(rec)
    boxes[0].setChecked(False)
    assert {"cmd": "port", "identity": ident, "action": "disable", "port_name": "voice"} in sent(rec)
    rm = next(b for b in d.findChildren(kit.CustomButton) if b.text() == "✕")
    rm.click()
    assert {"cmd": "port", "identity": ident, "action": "remove", "port_name": "voice"} in sent(rec)
    # the add form
    edit = next(e for e in d.findChildren(kit.CustomLineEdit) if e.placeholderText().startswith("Name"))
    portbox = next(s for s in d.findChildren(kit.CustomSpinBox) if s.specialValueText() == "port")
    combos = d.findChildren(QComboBox)
    proto, direction = combos[-2], combos[-1]
    add = next(b for b in d.findChildren(kit.CustomButton) if b.text() == "Add")
    add.click()                                                  # nothing typed yet: ignored
    assert not [c for c in rec.sent if c.get("action") == "add"]
    edit.setText("  stream ")
    portbox.setValue(9000)
    proto.setCurrentIndex(2)
    direction.setCurrentIndex(2)
    add.click()
    assert {"cmd": "port", "identity": ident, "action": "add", "port_name": "stream",
            "spec": {"name": "stream", "port": 9000, "proto": "udp", "direction": "out"}} in sent(rec)


def test_add_port_form_keeps_what_you_typed_when_the_panel_refreshes(win):
    w, rec = win
    d = _select_sober(w)
    edit = next(e for e in d.findChildren(kit.CustomLineEdit) if e.placeholderText().startswith("Name"))
    edit.setText("half typed")
    d._sig = None
    d.show_app(w.app(w.selected), False, "128.116.21.3")         # forces a rebuild
    edit = next(e for e in d.findChildren(kit.CustomLineEdit) if e.placeholderText().startswith("Name"))
    assert edit.text() == "half typed"


def test_signal_colours_defaults_validation_and_direction_mapping(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from portcullis.ui import palette
    sig = palette.signals()
    assert sig.for_direction("in").name() == "#f2994a" and sig.for_direction("out").name() == "#4da3ff"
    assert sig.for_direction("both").name() == "#b07cff" == sig.special.name()
    assert sig.on.name() == "#3fd08b" and sig.off.name() == "#f25c5c"
    cfg = guicfg.load()
    cfg.signal_on, cfg.signal_off = "#112233", "not a colour"
    guicfg.save(cfg)
    again = palette.signals()
    assert again.on.name() == "#112233" and again.off.name() == "#f25c5c"          # the bad one falls back


def test_toggles_are_green_when_on_and_red_when_off_and_block_boxes_are_inverted(qapp):
    from portcullis.ui import palette
    from portcullis.ui.widgets import StateCheckBox
    sig = palette.signals()
    cb = StateCheckBox("")
    assert cb._theme.accent().name() == sig.off.name()
    cb.setChecked(True)
    assert cb._theme.accent().name() == sig.on.name()
    block = StateCheckBox("", invert=True)
    assert block._theme.accent().name() == sig.on.name()          # not blocking = traffic on = green
    block.setChecked(True)
    assert block._theme.accent().name() == sig.off.name()


def test_pins_are_coloured_by_direction(win):
    w, rec = win
    kinds = {p.label: p.kind for p in model.build_pins(w.apps)}
    assert set(kinds.values()) <= {"in", "out", "both"} and "out" in kinds.values()
    only_in = model.Pin(0, 0, "x", [(w.apps[0], remote_view("in"))])
    assert only_in.kind == "in"
    mixed = model.Pin(0, 0, "x", [(w.apps[0], remote_view("in")), (w.apps[0], remote_view("out"))])
    assert mixed.kind == "both"


def remote_view(direction):
    return model.RemoteView(ip="1.1.1.1", direction=direction, place=None, local=False, active=True, blocked=False,
                            rule=None, temp_allowed=False, count=1, last=0.0)


def test_settings_page_saves_signal_colours(win):
    w, rec = win
    page = w.settings_page if hasattr(w, "settings_page") else next(p for p in w.findChildren(__import__("portcullis.ui.pages", fromlist=["SettingsPage"]).SettingsPage))
    page.signal_edits["signal_on"].setText("#00ff00")
    page.signal_edits["signal_off"].setText("nope")
    from unittest import mock
    with mock.patch("portcullis.ui.pages.show_message") as sm:
        page._save_theme()
    assert page.cfg.signal_on == "#00ff00" and page.cfg.signal_off == "#f25c5c"
    assert "signal_off" in sm.call_args[0][2]


def test_adding_and_switching_a_named_port_in_the_window_reaches_the_daemon_and_the_ruleset(live):
    w, store, sett, applied = live
    d = w.detail
    w.select_app("app:firefox")
    edit = next(e for e in d.findChildren(kit.CustomLineEdit) if e.placeholderText().startswith("Name"))
    portbox = next(s for s in d.findChildren(kit.CustomSpinBox) if s.specialValueText() == "port")
    edit.setText("voice")
    portbox.setValue(3478)
    next(b for b in d.findChildren(kit.CustomButton) if b.text() == "Add").click()
    wait(600)
    assert store.find("firefox").ports == [{"name": "voice", "port": 3478, "proto": "both", "direction": "both", "enabled": True}]
    assert "3478" not in applied[-1]                                   # enabled: no rule
    from portcullis.ui.widgets import StateCheckBox
    box = next(b for b in w.detail.findChildren(StateCheckBox) if b.toolTip().startswith("Enabled:"))
    assert box.isChecked()
    box.setChecked(False)
    wait(600)
    assert store.find("firefox").ports[0]["enabled"] is False
    assert "th dport 3478 counter drop" in applied[-1]
    box = next(b for b in w.detail.findChildren(StateCheckBox) if b.toolTip().startswith("Enabled:"))
    assert not box.isChecked()                                         # the window re-read the daemon
    box.setChecked(True)
    wait(600)
    assert "3478" not in applied[-1]


def test_dbus_backend_connects_its_signals_and_receives_them(qapp):
    """Regression: bus.connect() with 5 arguments raised TypeError and stopped the GUI starting."""
    import shutil, subprocess, sys, textwrap
    from portcullis.ui.notifier import DBusBackend
    DBusBackend()                                                    # must never raise, bus or no bus
    if not shutil.which("dbus-run-session"):
        pytest.skip("no dbus-run-session")
    code = textwrap.dedent("""
        import subprocess
        from PySide6.QtCore import QCoreApplication, QTimer
        from portcullis.ui import notifier
        notifier.SERVICE = ""                         # no notification daemon here: match any sender
        app = QCoreApplication([])
        b = notifier.DBusBackend()
        got = []
        b.actionInvoked.connect(lambda n, k: got.append((n, k)))
        b.closed.connect(lambda n: got.append(("closed", n)))
        def fire():
            for member, args in (("ActionInvoked", ["uint32:7", "string:allow_always"]), ("NotificationClosed", ["uint32:8", "uint32:2"])):
                subprocess.run(["dbus-send", "--session", "--type=signal", "/org/freedesktop/Notifications",
                                "org.freedesktop.Notifications." + member, *args])
        QTimer.singleShot(300, fire); QTimer.singleShot(1500, app.quit)
        app.exec()
        print(got)
    """)
    r = subprocess.run(["dbus-run-session", "--", sys.executable, "-c", code], capture_output=True, text=True,
                       env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"})
    assert "[(7, 'allow_always'), ('closed', 8)]" in r.stdout, r.stdout + r.stderr


# -- pinning, dialog sizing, importing a location file --------------------------------------------------------
def test_pinning_an_app_moves_it_to_the_top_keeps_it_and_persists(win):
    w, rec = win
    assert [a.name for a in w.apps] == ["firefox", "Sober", "steam", "discord"]
    w.applist._rows["app:steam"].star.setChecked(True)
    assert [a.name for a in w.apps][0] == "steam" and w.cfg.pinned == ["app:steam"]
    w.applist._rows["app:discord"].star.setChecked(True)
    assert [a.name for a in w.apps][:2] == ["steam", "discord"]               # in the order pinned
    assert guicfg.load().pinned == ["app:steam", "app:discord"]               # saved
    assert [i for i, r in w.applist._rows.items()][:2] == ["app:steam", "app:discord"]
    assert w.applist._rows["app:steam"].star.isChecked() and not w.applist._rows["app:firefox"].star.isChecked()
    w.applist._rows["app:steam"].star.setChecked(False)
    assert w.cfg.pinned == ["app:discord"] and [a.name for a in w.apps][0] == "discord"


def test_a_pinned_app_the_daemon_has_not_seen_still_appears():
    apps = model.build_apps(overview(), FakeGeo().lookup, None, ["flatpak:com.example.Game", "app:steam"])
    assert [a.identity for a in apps][:2] == ["flatpak:com.example.Game", "app:steam"]
    ghost = apps[0]
    assert ghost.name == "Game" and not ghost.running and ghost.remotes == []


def test_config_cleans_the_pinned_list(tmp_path):
    cfg = guicfg.GuiConfig(pinned=["a", "a", "", 5, "b"]).sanitize()
    assert cfg.pinned == ["a", "b"]


def test_long_messages_are_not_cut_off(qapp):
    from portcullis.ui_kit.custom_message_dialog import CustomMessageDialog
    text = ("couldn't download the location database (2026-10: HTTP Error 403: Forbidden; 2026-09: HTTP Error 403: "
            "Forbidden; 2026-08: HTTP Error 403: Forbidden). You can download dbip-city-lite-YYYY-MM.mmdb.gz yourself.")
    d = CustomMessageDialog("Location data", text)
    d.show()
    label = [l for l in d.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel) if l.text() == text][0]
    need = label.heightForWidth(label.width())
    assert label.height() >= need > 40, (label.height(), need)
    assert d.height() >= need + 60
    d.close()


def test_importing_a_location_file_from_the_settings_page(win, tmp_path):
    w, rec = win
    calls = []
    import portcullis.ui.window as winmod
    orig = winmod.geomod.install_file
    winmod.geomod.install_file = lambda p: calls.append(p) or tmp_path
    try:
        w.settings_page.importGeoClicked.connect(lambda: None)
        w.import_geo("/some/db.mmdb.gz")
        wait(900)
    finally:
        winmod.geomod.install_file = orig
    assert calls == ["/some/db.mmdb.gz"]


# -- restart button, visibility, splitter, globe ----------------------------------------------------------------------
class FakeService(__import__("PySide6.QtCore", fromlist=["QObject"]).QObject):
    finished = __import__("PySide6.QtCore", fromlist=["Signal"]).Signal(bool, str)

    def __init__(self, ok=True, msg=""):
        super().__init__()
        self.calls, self.ok, self.msg, self.busy = 0, ok, msg, False

    def restart(self):
        self.calls += 1
        self.finished.emit(self.ok, self.msg)


def _down_window(qapp, tmp_path, monkeypatch, service):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from portcullis.ui.window import MainWindow
    def down(cmd):
        raise ConnectionError("the portcullis service isn't running (/run/portcullis/control.sock doesn't exist)")
    w = MainWindow(bridge=Bridge(request=down), geo=FakeGeo(), cfg=guicfg.load(), poll_ms=10 ** 6, service=service)
    w.show()
    wait(300)
    return w


def test_restart_button_appears_when_the_service_is_down_and_runs_systemctl(qapp, tmp_path, monkeypatch):
    svc = FakeService(ok=True)
    w = _down_window(qapp, tmp_path, monkeypatch, svc)
    assert w.banner.isVisible() and w.banner.restart.isVisible() and "isn't running" in w.banner.label.text()
    w.banner.restart.click()
    assert svc.calls == 1
    w.shutdown()


def test_a_failed_restart_says_why(qapp, tmp_path, monkeypatch):
    from unittest import mock
    svc = FakeService(ok=False, msg="Access denied")
    w = _down_window(qapp, tmp_path, monkeypatch, svc)
    with mock.patch("portcullis.ui.window.show_message") as sm:
        w.banner.restart.click()
    assert "Access denied" in sm.call_args[0][2] and w.banner.restart.isVisible()
    w.shutdown()


def test_service_control_reports_success_and_failure_of_the_real_process(qapp, tmp_path):
    from portcullis.ui.service import ServiceControl
    results = []
    for script, expect in (("#!/bin/sh\nexit 0\n", True), ("#!/bin/sh\necho 'Failed to restart: Access denied' >&2\nexit 1\n", False)):
        prog = tmp_path / f"fake-systemctl-{expect}"
        prog.write_text(script)
        prog.chmod(0o755)
        sc = ServiceControl(program=str(prog))
        got = []
        sc.finished.connect(lambda ok, msg: got.append((ok, msg)))
        sc.restart()
        for _ in range(100):
            if got:
                break
            wait(20)
        results.append(got[0])
        assert got[0][0] is expect
    assert "Access denied" in results[1][1]
    sc = ServiceControl(program=str(tmp_path / "does-not-exist"))
    got = []
    sc.finished.connect(lambda ok, msg: got.append((ok, msg)))
    sc.restart()
    wait(300)
    assert got and got[0][0] is False


def test_hiding_an_app_takes_it_off_the_map_and_the_list(win):
    w, rec = win
    def on_map(ident):
        return any(a.identity == ident for p in w.map.pins for a in p.apps()) or any(a.identity == ident for a, _ in w.map.local_items)
    assert on_map("app:firefox")
    w.applist._rows["app:firefox"].eye.setChecked(False)
    assert w.cfg.hidden_apps == ["app:firefox"] and guicfg.load().hidden_apps == ["app:firefox"]
    assert not on_map("app:firefox") and "app:firefox" not in w.applist._rows
    assert w.applist.hidden_btn.isVisible() and "Show 1 hidden app" in w.applist.hidden_btn.text()
    w.applist.hidden_btn.click()                                  # list them (dimmed), still off the map
    assert "app:firefox" in w.applist._rows and w.applist._rows["app:firefox"].graphicsEffect() is not None
    assert not on_map("app:firefox") and w.settings_page.show_hidden.isChecked()
    w.applist._rows["app:firefox"].eye.setChecked(True)
    assert w.cfg.hidden_apps == [] and on_map("app:firefox")


def test_hiding_one_connection_removes_only_that_pin(win):
    from portcullis.ui.widgets import ConnectionRow
    w, rec = win
    w.select_app("app:firefox")
    tokyo = lambda: [p for p in w.map.pins if any(r.ip == "13.107.42.14" for _, r in p.items)]
    assert tokyo()
    n = len(w.map.pins)
    row = next(r for r in w.detail.findChildren(ConnectionRow) if r.ip == "13.107.42.14")
    row.eye.setChecked(False)
    assert w.cfg.hidden_remotes == ["app:firefox|13.107.42.14"] and not tokyo() and len(w.map.pins) == n - 1
    row = next(r for r in w.detail.findChildren(ConnectionRow) if r.ip == "13.107.42.14")
    assert not row.eye.isChecked() and row.graphicsEffect() is not None          # still listed, dimmed
    row.eye.setChecked(True)
    assert w.cfg.hidden_remotes == [] and tokyo()


def test_for_map_filters_without_touching_the_originals():
    apps = model.build_apps(overview(), FakeGeo().lookup)
    ff = next(a for a in apps if a.identity == "app:firefox")
    shown = model.for_map(apps, ["app:steam"], ["app:firefox|13.107.42.14"])
    assert "app:steam" not in [a.identity for a in shown]
    assert "13.107.42.14" not in [r.ip for a in shown if a.identity == "app:firefox" for r in a.remotes]
    assert "13.107.42.14" in [r.ip for r in ff.remotes]


def test_splitter_resizes_the_map_and_remembers_it(win):
    w, rec = win
    w.resize(1500, 860)
    wait(100)
    w.split.setSizes([300, 900, 300])
    w.split.splitterMoved.emit(0, 1)
    wait(600)
    saved = guicfg.load().split_sizes
    assert len(saved) == 3 and saved == w.split.sizes() and w.map.width() == saved[1]


def test_config_cleans_visibility_and_layout_values():
    c = guicfg.GuiConfig(hidden_apps=["a", "a", 3], hidden_remotes=["a|1.2.3.4", "junk"], map_mode="cube",
                         split_sizes=[1, 2]).sanitize()
    assert c.hidden_apps == ["a"] and c.hidden_remotes == ["a|1.2.3.4"] and c.map_mode == "flat" and c.split_sizes == []
    assert guicfg.GuiConfig(split_sizes=[200, 600, 300]).sanitize().split_sizes == [200, 600, 300]


def test_globe_projection_roundtrips_and_hides_the_far_side():
    from portcullis.ui.mapview import Globe
    g = Globe(zoom=1.3, lon0=-90.0, lat0=40.0)
    for lon, lat in ((-90, 40), (-122.4, 37.8), (-46.6, -23.5), (0, 51.5)):
        assert g.visible(lon, lat)
        x, y = g.project(lon, lat, 800, 600)
        lo, la = g.unproject(x, y, 800, 600)
        assert math.isclose(lo, lon, abs_tol=1e-6) and math.isclose(la, lat, abs_tol=1e-6)
    assert g.project(-90, 40, 800, 600) == (400.0, 300.0)           # the facing point is the centre
    assert not g.visible(90, -40)                                    # the antipode is behind
    assert g.unproject(5, 5, 800, 600) is None                       # a corner is off the globe


def test_globe_mode_toggle_drag_spins_and_pins_behind_are_not_clickable(qapp):
    m, _ = _map(qapp)
    modes = []
    m.modeChanged.connect(modes.append)
    m.repaint()
    QTest.mouseClick(m, Qt.LeftButton, Qt.NoModifier, m._mode_rect.center().toPoint())
    assert m.mode == "globe" and modes == ["globe"]
    assert math.isclose(m.view.lon0, -87.98, abs_tol=0.01)          # turned to face you
    m.repaint()
    lon0 = m.view.lon0
    start = QPointF(m.width() / 2 + 120, m.height() / 2 + 150).toPoint()
    QTest.mousePress(m, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(m, start + QPointF(-200, 0).toPoint())
    QTest.mouseRelease(m, Qt.LeftButton, Qt.NoModifier, start + QPointF(-200, 0).toPoint())
    assert m.view.lon0 > lon0 + 10                                   # dragged left -> the globe turned east
    sydney = next(p for p in m.pins if p.label.startswith("Sydney"))
    m.view.lon0, m.view.lat0 = -40.0, 40.0                           # Sydney is now on the far side
    assert not m.view.visible(sydney.lon, sydney.lat)
    assert m._pin_at(m._pt(sydney.lat, sydney.lon)) is None
    m.repaint()
    QTest.mouseClick(m, Qt.LeftButton, Qt.NoModifier, m._mode_rect.center().toPoint())
    assert m.mode == "flat" and modes == ["globe", "flat"]


def test_map_mode_follows_settings_and_persists(win):
    w, rec = win
    w.settings_page.globe.setChecked(True)
    assert w.map.mode == "globe" and guicfg.load().map_mode == "globe"
    w.map.repaint()
    QTest.mouseClick(w.map, Qt.LeftButton, Qt.NoModifier, w.map._mode_rect.center().toPoint())
    assert w.map.mode == "flat" and guicfg.load().map_mode == "flat" and not w.settings_page.globe.isChecked()
