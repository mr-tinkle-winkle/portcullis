import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTCULLIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PORTCULLIS_SOCKET", str(tmp_path / "run" / "c.sock"))
    monkeypatch.delenv("PORTCULLIS_CGROUP_ROOT", raising=False)
