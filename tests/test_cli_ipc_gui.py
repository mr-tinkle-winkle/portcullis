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
def test_profile_flags_block_allow_toggle_and_latency(served, capsys):
    store, eng, applied = served
    run(["add", "X", "--app", "app:firefox"], capsys)
    assert not store.find("X").enabled
    code, out, _ = run(["--profile", "X", "--blockIncoming", "--outgoingLatency=250"], capsys)
    p = store.find("X")
    assert code == 0 and p.block_in and not p.block_out and p.delay_out_ms == 250
    assert p.enabled and "ACTIVE" in out and "block incoming" in out and "delay outgoing 250 ms" in out
    run(["--profile=X", "--toggleIncoming", "--toggleOutgoing"], capsys)
    p = store.find("x")
    assert not p.block_in and p.block_out
    run(["--profile", "X", "--allowOutgoing", "--outgoingLatency", "0", "--incomingLatency", "90"], capsys)
    p = store.find("X")
    assert not p.block_out and p.delay_out_ms == 0 and p.delay_in_ms == 90


def test_profile_without_actions_just_reports(served, capsys):
    store, *_ = served
    run(["add", "X", "--app", "app:firefox", "--block-out"], capsys)
    code, out, _ = run(["--profile", "X"], capsys)
    assert code == 0 and "block outgoing" in out and not store.find("X").enabled


def test_profile_flag_errors(served, capsys):
    run(["add", "X", "--app", "app:firefox"], capsys)
    for argv, text in ((["--profile", "X", "--blockIncoming", "--allowIncoming"], "pick one"),
                       (["--profile", "X", "--toggleOutgoing", "--blockOutgoing"], "pick one"),
                       (["--profile", "X", "--incomingLatency", "9999"], "between 0 and 5000"),
                       (["--profile", "nope", "--blockIncoming"], "no profile named")):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert text in str(e.value)
    assert not served[0].find("X").block_in


def test_profile_warns_when_latency_is_shadowed_by_a_block(served, capsys):
    run(["add", "X", "--app", "app:firefox"], capsys)
    _, _, err = run(["--profile", "X", "--blockOutgoing", "--outgoingLatency", "100"], capsys)
    assert "no effect" in err


def test_named_ports_add_disable_toggle_enable_remove(served, capsys):
    store, eng, applied = served
    run(["add", "X", "--app", "app:firefox"], capsys)
    code, out, _ = run(["--profile", "X", "--addPort", "voice=3478/udp@out", "--addPort", "host=7777"], capsys)
    assert code == 0 and "voice: 3478/udp outgoing - enabled" in out and "host: 7777/both in+out - enabled" in out
    assert not store.find("X").enabled                                       # nothing blocked yet -> stays off
    code, out, _ = run(["--profile", "X", "--disablePort", "voice"], capsys)
    assert "voice: 3478/udp outgoing - DISABLED" in out and "ports off: voice" in out
    assert store.find("X").enabled                                           # a blocked port switches the profile on
    assert "th dport 3478" in applied[-1]
    run(["--profile", "X", "--togglePort", "voice", "--togglePort", "7777"], capsys)
    assert [x["enabled"] for x in store.find("X").ports] == [True, False]
    run(["--profile", "X", "--enablePort", "host", "--disablePort", "3478"], capsys)
    assert [x["enabled"] for x in store.find("X").ports] == [False, True]
    run(["--profile", "X", "--removePort", "voice"], capsys)
    assert [x["name"] for x in store.find("X").ports] == ["host"]
    assert "3478" not in applied[-1]


def test_named_port_errors(served, capsys):
    run(["add", "X", "--app", "app:firefox"], capsys)
    for argv, text in ((["--profile", "X", "--addPort", "nonsense"], "NAME=PORT"),
                       (["--profile", "X", "--addPort", "a=abc"], "must be a number"),
                       (["--profile", "X", "--addPort", "a=99999"], "1-65535"),
                       (["--profile", "X", "--enablePort", "ghost"], "no port 'ghost'")):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert text in str(exc.value.code) + capsys.readouterr().err


def test_keep_alive_flag(served, capsys):
    store, eng, applied = served
    run(["add", "X", "--app", "app:firefox"], capsys)
    code, out, _ = run(["--profile", "X", "--blockOutgoing", "--keepAlive=120"], capsys)
    assert code == 0 and store.find("X").block_out_above == 120 and "UDP up to 120 bytes still goes out" in out
    assert "udp length > 128" in applied[-1]
    with pytest.raises(SystemExit) as e:
        cli.main(["--profile", "X", "--keepAlive=5000"])
    assert "between 0 and 1500" in str(e.value.code)


# -- per-app targets and the focused window --------------------------------------------------------------------------
def test_app_target_blocks_a_running_app_by_name_creating_its_profile(served, capsys):
    store, eng, applied = served
    code, out, _ = run(["--app", "sober", "--blockOutgoing", "--autoUnblockOutgoing=2.5"], capsys)
    p = next(x for x in store.all() if "flatpak:org.vinegarhq.Sober" in x.match)
    assert code == 0 and p.enabled and p.block_out and p.auto_unblock_out_s == 2.5
    assert "(flatpak:org.vinegarhq.Sober)" in out and "unblocks after 2.5s" in out
    assert "Sober-1.scope" in applied[-1]
    run(["--app", "flatpak:org.vinegarhq.Sober", "--toggleOutgoing", "--addPort", "voice=3478/udp"], capsys)
    p = next(x for x in store.all() if "flatpak:org.vinegarhq.Sober" in x.match)
    assert not p.block_out and p.ports[0]["name"] == "voice"
    code, out, _ = run(["--app", "firefox"], capsys)                   # report only: creates nothing
    assert "app:firefox: no profile yet" in out and not [x for x in store.all() if "app:firefox" in x.match]


def test_override_to_focused_targets_the_focused_windows_app(served, capsys, monkeypatch):
    from portcullis import focus
    store, eng, applied = served
    run(["add", "Lag", "--app", "app:firefox"], capsys)
    monkeypatch.setattr(focus, "focused_identity", lambda: ("flatpak:org.vinegarhq.Sober", "app-flatpak-org.vinegarhq.Sober-1.scope"))
    for flag in ("--override_to_focused", "--override-to-focused", "--overrideToFocused"):
        code, out, _ = run(["--profile", "Lag", flag, "--toggleIncoming"], capsys)    # focused wins over --profile
        assert code == 0 and "(flatpak:org.vinegarhq.Sober)" in out
    sober = next(x for x in store.all() if "flatpak:org.vinegarhq.Sober" in x.match)
    assert sober.block_in is True and not store.find("Lag").block_in   # toggled 3 times; Lag untouched

    def broken():
        raise focus.FocusError("the focused window isn't running in its own app unit")
    monkeypatch.setattr(focus, "focused_identity", broken)
    with pytest.raises(SystemExit) as e:
        cli.main(["--override_to_focused", "--blockOutgoing"])
    assert "own app unit" in str(e.value.code)


def test_target_form_needs_a_target_and_leaves_subcommands_alone(served, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--blockOutgoing"])
    store, *_ = served
    assert run(["add", "X", "--app", "app:firefox", "--block-out"], capsys)[0] == 0   # `add --app` still the subcommand
    assert store.find("X").match == ["app:firefox"]


def test_focus_reads_the_window_pid_and_its_app_unit(tmp_path):
    from portcullis import focus
    calls = {"getactivewindow": "{abc}", "getwindowpid": "4242", "getwindowclassname": "sober"}
    run_ = lambda *a: calls.get(a[0])                                    # noqa: E731
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "cgroup").write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.vinegarhq.Sober-77.scope\n")
    assert focus.focused_identity(run_, str(tmp_path)) == ("flatpak:org.vinegarhq.Sober", "app-flatpak-org.vinegarhq.Sober-77.scope")
    (tmp_path / "4242" / "cgroup").write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                                              "app-org.kde.konsole-12.scope\n")
    assert focus.focused_identity(run_, str(tmp_path))[0] == "app:org.kde.konsole"
    (tmp_path / "4242" / "cgroup").write_text("0::/user.slice/user-1000.slice/session-2.scope\n")
    with pytest.raises(focus.FocusError, match="own app unit"):
        focus.focused_identity(run_, str(tmp_path))
    with pytest.raises(focus.FocusError, match="which window"):
        focus.focused_identity(lambda *a: None, str(tmp_path))
