import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from portcullis import daemon, ipc
from portcullis.engine import Engine
from portcullis.overlay import model
from portcullis.overlay.paint import PanelPainter
from portcullis.profiles import ProfileStore
from portcullis.ui import config as guicfg
from test_unit import FakeQueues, _app

import portcullis.ui_kit as kit

BLOCKS = [
    {"name": "Sober", "match": ["flatpak:org.vinegarhq.Sober"], "running": True, "in": False, "out": True,
     "in_left": None, "out_left": 2.5, "keep_alive": 100, "ports_off": [], "addresses": 0},
    {"name": "Lag", "match": ["app:firefox"], "running": True, "in": True, "out": False,
     "in_left": None, "out_left": None, "keep_alive": 0, "ports_off": ["voice"], "addresses": 3},
    {"name": "Old", "match": ["app:steam"], "running": False, "in": True, "out": True,
     "in_left": None, "out_left": None, "keep_alive": 0, "ports_off": [], "addresses": 0},
    {"name": "AddrOnly", "match": ["app:discord"], "running": True, "in": False, "out": False,
     "in_left": None, "out_left": None, "keep_alive": 0, "ports_off": [], "addresses": 2},
]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    kit.set_settings_provider(lambda: guicfg.load_readonly().theme)
    return app


def wait(ms=150):
    import time
    end = time.time() + ms / 1000
    while time.time() < end:
        QTest.qWait(5)
        time.sleep(0.01)


# -- what goes on the panel -----------------------------------------------------------------------------------------
def test_lines_list_blocked_directions_ports_and_countdowns():
    lines = model.build_lines(BLOCKS, now=100.0)
    assert [ln.app for ln in lines] == ["firefox", "Sober"]          # steam isn't running; discord: addresses off
    sober = lines[1]
    assert [(c.text, c.kind) for c in sober.chips] == [("OUT", "out"), ("keep-alive 100 B", "note")]
    assert sober.chips[0].deadline == 102.5
    ff = lines[0]
    assert [(c.text, c.kind) for c in ff.chips] == [("IN", "in"), ("port voice", "special")]
    assert model.countdown(102.5, 101.0) == "1.5s" and model.countdown(None, 0) == "" and model.countdown(100, 200) == "0.0s"


def test_options_change_what_is_listed():
    lines = model.build_lines(BLOCKS, 0.0, ports=False, addresses=True, only_running=False)
    names = [ln.app for ln in lines]
    assert names == ["discord", "firefox", "Sober", "steam"]
    assert [c.text for c in lines[0].chips] == ["2 addresses"]
    assert [c.text for c in lines[1].chips] == ["IN", "3 addresses"]
    assert model.build_lines([], 0.0) == []


def test_too_many_lines_are_summed_up():
    many = [dict(BLOCKS[0], match=[f"app:a{i:02d}"], name=f"a{i}") for i in range(12)]
    lines = model.build_lines(many, 0.0)
    assert len(lines) == model.MAX_LINES and lines[-1].app == f"+{12 - model.MAX_LINES + 1} more"


def test_corner_placement_and_sizes(qapp):
    pp = PanelPainter(1.0)
    lines = model.build_lines(BLOCKS, 0.0)
    w, h = pp.size(lines, 0.0)
    assert w > 100 and h > 40 and pp.size([], 0.0) == (0.0, 0.0)
    mw, mh = pp.max_size()
    assert mw >= w and mh >= h
    area = QRectF(0, 0, 1000, 600)
    assert pp.placed("top-left", (w, h), area).topLeft().toTuple() == (0, 0)
    br = pp.placed("bottom-right", (w, h), area)
    assert (br.right(), br.bottom()) == (1000, 600)
    assert PanelPainter(2.0).size(lines, 0.0)[0] > w * 1.6            # scales


# -- the overlay process --------------------------------------------------------------------------------------------
class Cfg(guicfg.GuiConfig):
    pass


def test_overlay_shows_only_while_something_is_blocked_and_follows_settings(qapp):
    from portcullis.overlay.app import Overlay
    cfg = guicfg.GuiConfig()
    replies = {"blocks": []}
    ov = Overlay(request=lambda cmd, **kw: {"ok": True, "blocks": replies["blocks"]}, config=lambda: cfg,
                 force_layer_shell=False)
    ov.apply([])
    assert ov.surface is None                                        # nothing blocked: no window at all
    ov.apply(BLOCKS)
    s = ov.surface
    assert s is not None and s.isVisible() and [ln.app for ln in s.lines] == ["firefox", "Sober"]
    pr = s.panel_rect()
    assert pr.right() == s.rect().right() + 1 and pr.top() == 0      # default top-right
    cfg.overlay_corner = "bottom-left"
    ov.apply(BLOCKS)
    assert ov.surface is not s and ov.surface.panel_rect().left() == 0
    assert ov.surface.panel_rect().bottom() == ov.surface.rect().bottom() + 1
    cfg.overlay_enabled = False
    ov.apply(BLOCKS)
    assert ov.surface is None
    cfg.overlay_enabled = True
    ov.apply(BLOCKS)
    ov.apply([])
    assert ov.surface is None
    ov.stop()


def test_overlay_polls_the_service_and_survives_it_being_down(qapp):
    from portcullis.overlay.app import Overlay
    cfg = guicfg.GuiConfig()
    state = {"down": False}

    def req(cmd, **kw):
        assert cmd == {"cmd": "blocks"}
        if state["down"]:
            raise ConnectionError("no service")
        return {"ok": True, "blocks": BLOCKS[:1]}
    ov = Overlay(request=req, config=lambda: cfg, force_layer_shell=False)
    ov.start()
    wait(500)
    assert ov.surface is not None and ov.surface.lines[0].app == "Sober"
    state["down"] = True
    wait(600)
    assert ov.surface is None
    ov.stop()


def test_wayland_without_the_shim_never_shows(qapp, monkeypatch):
    from portcullis.overlay import app as appmod
    monkeypatch.setattr(appmod.QGuiApplication, "platformName", staticmethod(lambda: "wayland"))
    monkeypatch.setattr(appmod.layershell, "available", lambda: False)
    ov = appmod.Overlay(request=lambda c, **k: {"ok": True, "blocks": BLOCKS}, config=guicfg.GuiConfig)
    assert not ov.can_show
    ov.apply(BLOCKS)
    assert ov.surface is None
    ov.stop()


def test_layershell_anchor_bits_and_missing_library(monkeypatch, tmp_path):
    from portcullis.overlay import layershell
    assert layershell.anchor_bits(("top", "right")) == 1 | 8
    assert layershell.anchor_bits(("bottom", "left")) == 2 | 4
    monkeypatch.setattr(layershell, "_tried", False)
    monkeypatch.setattr(layershell, "_lib", None)
    monkeypatch.setenv("PORTCULLIS_LAYERSHELL_LIB", str(tmp_path / "nope.so"))
    monkeypatch.setattr(layershell, "_candidates", lambda: [tmp_path / "nope.so"])
    assert not layershell.available() and not layershell.configure(object(), ("top", "left"))


# -- the daemon side ---------------------------------------------------------------------------------------------------
def test_daemon_reports_blocks_with_seconds_left(tmp_path):
    now = [10.0]
    store = ProfileStore(tmp_path / "p.json", clock=lambda: now[0])
    apps = [_app("app-flatpak-org.vinegarhq.Sober-1.scope")]
    eng = Engine(store, scan=lambda r: list(apps), apply=lambda t: (True, ""), queues=FakeQueues(store),
                 v2root="/sys/fs/cgroup", counters=lambda: {})
    ctl = daemon.Controller(store, eng, threading.Event())
    store.add(name="Sober", match=["flatpak:org.vinegarhq.Sober"], enabled=True, auto_unblock_out_s=4)
    store.add(name="Idle", match=["app:x"], enabled=True)                         # blocks nothing: not listed
    store.add(name="Off", match=["app:y"], block_in=True)                          # switched off: not listed
    assert ctl({"cmd": "blocks"}) == {"ok": True, "blocks": []}
    ctl({"cmd": "set", "name": "Sober", "changes": {"block_out": True, "block_out_above": 90}})
    now[0] += 1.5
    b = ctl({"cmd": "blocks"})["blocks"]
    assert len(b) == 1 and b[0]["out"] and not b[0]["in"] and b[0]["running"] and b[0]["keep_alive"] == 90
    assert abs(b[0]["out_left"] - 2.5) < 1e-6 and b[0]["in_left"] is None


# -- settings -------------------------------------------------------------------------------------------------------------
def test_settings_page_overlay_controls_save_and_preview(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from portcullis.ui.pages import SettingsPage
    cfg = guicfg.load()
    page = SettingsPage(cfg)
    got = []
    page.guiSettingChanged.connect(lambda: got.append(1))
    page.ov_corner.setCurrentIndex(page.ov_corner.findData("bottom-left"))
    page.ov_corner.activated.emit(page.ov_corner.currentIndex())
    page.ov_margin.setValue(60)
    page.ov_scale.setValue(1.5)
    page.ov_addresses.setChecked(True)
    assert (cfg.overlay_corner, cfg.overlay_margin, cfg.overlay_scale, cfg.overlay_addresses) == ("bottom-left", 60, 1.5, True)
    assert got
    page.resize(800, 2400)
    page.ov_preview.grab()                                            # paints without errors


def test_config_cleans_overlay_values():
    c = guicfg.GuiConfig(overlay_corner="middle", overlay_margin=-5, overlay_scale=9).sanitize()
    assert (c.overlay_corner, c.overlay_margin, c.overlay_scale) == ("top-right", 0, 3.0)
