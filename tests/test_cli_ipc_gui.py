import threading

import pytest

from portcullis import cli, daemon, ipc
from portcullis.engine import Engine
from portcullis.profiles import ProfileStore
from test_unit import FakeQueues, _app


@pytest.fixture
def served(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    apps = [_app("app-flatpak-org.vinegarhq.Sober-1.scope"), _app("app-firefox-2.scope")]
    applied = []
    eng = Engine(store, scan=lambda r: list(apps), apply=lambda t: (applied.append(t) or (True, "")),
                 queues=FakeQueues(store), v2root="/sys/fs/cgroup", counters=lambda: {(0, "out"): {"packets": 7, "bytes": 700, "verdict": "drop"}})
    ctl = daemon.Controller(store, eng, threading.Event())
    srv = ipc.ControlServer(ipc.socket_path(), ctl)
    srv.serve_in_thread()
    yield store, eng, applied
    srv.shutdown()
    srv.server_close()


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_add_from_a_running_app_then_toggle_applies_rules(served, capsys):
    store, eng, applied = served
    assert run(["add", "Sober", "--running", "sober", "--block-out", "--delay-in", "120"], capsys)[0] == 0
    assert store.find("sober").match == ["flatpak:org.vinegarhq.Sober"] and store.find("Sober").block_out
    assert "chain" not in applied[-1]                                       # added switched off
    code, out, _ = run(["on", "Sober"], capsys)
    assert "ACTIVE" in out and "Sober-1.scope" in applied[-1]
    assert "queue" in applied[-1] and "drop" in applied[-1]
    assert "off" in run(["toggle", "Sober"], capsys)[1]
    assert "chain" not in applied[-1]


def test_set_with_on_off_toggle_and_delays(served, capsys):
    store, *_ = served
    run(["add", "X", "--app", "app:firefox"], capsys)
    run(["set", "X", "--block-in", "toggle", "--delay-out", "75"], capsys)
    p = store.find("X")
    assert p.block_in and p.delay_out_ms == 75
    run(["set", "X", "--block-in", "off"], capsys)
    assert not store.find("X").block_in


def test_running_pattern_must_be_unique_and_exist(served, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["add", "N", "--running", "nonexistent"])
    assert "no running app" in str(e.value)
    with pytest.raises(SystemExit) as e:
        cli.main(["add", "N", "--running", "o"])                           # "flatpak:org..." and "app:firefox" both contain "o"
    assert "matches several" in str(e.value)


def test_list_apps_status_and_errors(served, capsys):
    run(["add", "Sober", "--running", "vinegar", "--enable", "--block-out"], capsys)
    _, out, _ = run([], capsys)
    assert "ACTIVE" in out and "block outgoing" in out and "[running: 1]" in out
    _, out, _ = run(["apps"], capsys)
    assert "<- Sober" in out and "app:firefox" in out
    _, out, _ = run(["status"], capsys)
    assert "dropped 7 packets" in out


def test_duplicate_and_unknown_profile_errors(served, capsys):
    run(["add", "A"], capsys)
    with pytest.raises(SystemExit):
        cli.main(["add", "a"])
    assert "already exists" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["on", "ghost"])
    assert "no profile named" in capsys.readouterr().err


def test_cli_without_a_daemon_explains(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["list"])
    assert "service isn't running" in capsys.readouterr().err


def test_unknown_command_over_the_socket(served):
    assert ipc.request({"cmd": "bogus"})["ok"] is False
    assert ipc.request({"cmd": "ping"})["ok"] is True


def test_socket_mode_is_group_only(served):
    import os
    import stat
    assert stat.S_IMODE(os.stat(ipc.socket_path()).st_mode) == 0o660


def test_launch_builds_a_systemd_run_command(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: seen.update(cmd=cmd) or 0)
    assert cli.main(["launch", "my game", "--", "steam-run", "./game"]) == 0
    c = seen["cmd"]
    assert c[:3] == ["systemd-run", "--user", "--scope"] and c[-2:] == ["steam-run", "./game"]
    unit = next(x for x in c if x.startswith("--unit="))[7:]
    from portcullis import appid
    assert appid.identity(unit) == "app:mygame"


# ---- GUI -------------------------------------------------------------------------------------------------------------------------
def test_gui_edits_go_through_the_daemon(served, qapp_fixture=None):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from portcullis import gui
    store, eng, applied = served
    store.add(name="Sober", match=["flatpak:org.vinegarhq.Sober"])
    w = gui.Window()
    w.timer.stop()
    assert w.list.count() == 1 and w.title.text() == "Sober" and not w.active.isChecked()
    w.block_out.setChecked(True)
    w.active.setChecked(True)
    assert store.find("Sober").block_out and store.find("Sober").enabled
    assert "Sober-1.scope" in applied[-1] and "drop" in applied[-1]
    w.delay_in.setValue(250)
    w._delay_timer.stop()
    w._delay_edited()
    assert store.find("Sober").delay_in_ms == 250
    w.refresh()
    assert "Running now" in w.running.text() and "dropped 7 packets" in w.counters.text()
    w.close()


def test_gui_shows_a_banner_when_the_service_is_down(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from portcullis import gui
    w = gui.Window()
    w.timer.stop()
    assert "isn't running" in w.banner.text() and not w.editor.isEnabled()
    w.close()
