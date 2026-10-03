"""Starting the system service from the window (when it's down).  Uses ``systemctl``, which asks polkit:
the NixOS module allows members of the ``portcullis`` group to start / stop / restart the unit without a
password; elsewhere your desktop's polkit agent asks for one."""
from __future__ import annotations

import shutil

from PySide6.QtCore import QObject, QProcess, Signal

UNIT = "portcullis.service"


class ServiceControl(QObject):
    finished = Signal(bool, str)          # ok, message

    def __init__(self, parent=None, program: "str | None" = None):
        super().__init__(parent)
        self.program = program or shutil.which("systemctl") or "systemctl"
        self._proc: "QProcess | None" = None

    @property
    def busy(self) -> bool:
        return self._proc is not None

    def restart(self) -> None:
        if self._proc is not None:
            return
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.finished.connect(lambda code, status: self._done(p, code, status))
        p.errorOccurred.connect(lambda err: self._failed(p, err))
        self._proc = p
        p.start(self.program, ["restart", UNIT])

    def _done(self, p: QProcess, code: int, status) -> None:
        if self._proc is not p:
            return
        self._proc = None
        out = bytes(p.readAll()).decode(errors="replace").strip()
        ok = status == QProcess.NormalExit and code == 0
        self.finished.emit(ok, "" if ok else (out or f"systemctl exited with code {code}"))
        p.deleteLater()

    def _failed(self, p: QProcess, err) -> None:
        if err == QProcess.FailedToStart and self._proc is p:
            self._proc = None
            self.finished.emit(False, f"couldn't run {self.program}")
            p.deleteLater()
