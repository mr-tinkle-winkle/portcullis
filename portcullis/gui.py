"""The window: profiles on the left, the selected profile's settings on the right.  Everything is
applied immediately through the same control socket the CLI uses."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from . import ipc
from .profiles import MAX_DELAY_MS


PAST = {"drop": "dropped", "queue": "delayed"}


def call(cmd: dict) -> dict:
    return ipc.request(cmd, timeout=2.0)


class PickApp(QDialog):
    """Choose one of the running apps; the profile will match every future launch of it."""

    def __init__(self, apps: "list[dict]", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add from a running app")
        col = QVBoxLayout(self)
        col.addWidget(QLabel("Pick an app. The profile will also apply every time it is launched again."))
        self.list = QListWidget()
        seen = set()
        for a in apps:
            if a["identity"] in seen:
                continue
            seen.add(a["identity"])
            item = QListWidgetItem(a["identity"] + (f"   (already in “{a['profile']}”)" if a["profile"] else ""))
            item.setData(Qt.ItemDataRole.UserRole, a["identity"])
            self.list.addItem(item)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        col.addWidget(self.list)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        col.addWidget(box)
        self.resize(520, 360)

    def chosen(self) -> "str | None":
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Portcullis")
        self.resize(760, 460)
        self._status: dict = {}
        self._loading = False
        self._current: "str | None" = None

        root = QVBoxLayout(self)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("color: #b00020; font-weight: 600;")
        root.addWidget(self.banner)
        body = QHBoxLayout()
        root.addLayout(body, 1)

        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._selected)
        left.addWidget(self.list, 1)
        b_add = QPushButton("Add from running app…")
        b_add.clicked.connect(self._add_from_running)
        b_blank = QPushButton("Add blank profile…")
        b_blank.clicked.connect(self._add_blank)
        self.b_remove = QPushButton("Remove")
        self.b_remove.clicked.connect(self._remove)
        for b in (b_add, b_blank, self.b_remove):
            left.addWidget(b)
        body.addLayout(left, 1)

        self.editor = QWidget()
        right = QVBoxLayout(self.editor)
        self.title = QLabel("")
        self.title.setStyleSheet("font-size: 15pt; font-weight: 600;")
        right.addWidget(self.title)
        self.active = QCheckBox("Active  (applies these settings to the app)")
        self.active.toggled.connect(lambda v: self._edit({"enabled": v}))
        right.addWidget(self.active)

        self.match = QLineEdit()
        self.match.setPlaceholderText("flatpak:org.vinegarhq.Sober, app:firefox, unit:app-foo-*")
        self.match.editingFinished.connect(self._match_edited)
        form = QFormLayout()
        form.addRow("Applies to", self.match)
        right.addLayout(form)

        self.block_out = QCheckBox("Block outgoing")
        self.block_in = QCheckBox("Block incoming")
        self.delay_out = self._spin()
        self.delay_in = self._spin()
        self.block_out.toggled.connect(lambda v: self._edit({"block_out": v}))
        self.block_in.toggled.connect(lambda v: self._edit({"block_in": v}))
        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.setInterval(350)
        self._delay_timer.timeout.connect(self._delay_edited)
        self.delay_out.valueChanged.connect(lambda _: self._delay_timer.start())
        self.delay_in.valueChanged.connect(lambda _: self._delay_timer.start())
        for title, block, delay in (("Outgoing  (your game → the server)", self.block_out, self.delay_out),
                                    ("Incoming  (the server → your game)", self.block_in, self.delay_in)):
            box = QGroupBox(title)
            g = QFormLayout(box)
            g.addRow(block)
            g.addRow("Add fake latency", delay)
            right.addWidget(box)
        self.running = QLabel("")
        self.running.setWordWrap(True)
        right.addWidget(self.running)
        self.counters = QLabel("")
        right.addWidget(self.counters)
        self.note = QLabel("Blocking wins over latency in the same direction.")
        self.note.setStyleSheet("color: gray;")
        right.addWidget(self.note)
        right.addStretch(1)
        body.addWidget(self.editor, 2)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    @staticmethod
    def _spin() -> QSpinBox:
        s = QSpinBox()
        s.setRange(0, MAX_DELAY_MS)
        s.setSingleStep(10)
        s.setSuffix(" ms")
        s.setSpecialValueText("off")
        return s

    # -- talking to the daemon ------------------------------------------------------------------------------
    def _send(self, cmd: dict) -> "dict | None":
        try:
            reply = call(cmd)
        except ConnectionError as e:
            self.banner.setText(str(e))
            return None
        if not reply.get("ok"):
            QMessageBox.warning(self, "Portcullis", reply.get("error", "failed"))
            return None
        self.banner.setText("")
        return reply

    def _edit(self, changes: dict) -> None:
        if self._loading or not self._current:
            return
        if self._send({"cmd": "set", "name": self._current, "changes": changes}):
            self.refresh()

    def _delay_edited(self) -> None:
        self._edit({"delay_out_ms": self.delay_out.value(), "delay_in_ms": self.delay_in.value()})

    def _match_edited(self) -> None:
        self._edit({"match": [m.strip() for m in self.match.text().split(",") if m.strip()]})

    # -- profile list -----------------------------------------------------------------------------------------
    def _add_blank(self) -> None:
        name, ok = QInputDialog.getText(self, "New profile", "Name:")
        if ok and name.strip() and self._send({"cmd": "add", "fields": {"name": name.strip()}}):
            self._current = name.strip()
            self.refresh()

    def _add_from_running(self) -> None:
        reply = self._send({"cmd": "apps"})
        if not reply:
            return
        dlg = PickApp(reply["apps"], self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen():
            return
        ident = dlg.chosen()
        default = ident.split(":", 1)[1].split(".")[-1]
        name, ok = QInputDialog.getText(self, "New profile", "Name:", text=default)
        if ok and name.strip() and self._send({"cmd": "add", "fields": {"name": name.strip(), "match": [ident]}}):
            self._current = name.strip()
            self.refresh()

    def _remove(self) -> None:
        if self._current and QMessageBox.question(self, "Remove", f"Remove “{self._current}”?") == QMessageBox.StandardButton.Yes:
            if self._send({"cmd": "remove", "name": self._current}):
                self._current = None
                self.refresh()

    def _selected(self, item, _prev=None) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        if name != self._current:
            self._current = name
            self._fill()

    # -- refresh -----------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        reply = self._send({"cmd": "status"})
        if reply is None:
            self.editor.setEnabled(False)
            return
        self._status = reply
        self._loading = True
        self.list.blockSignals(True)
        self.list.clear()
        for p in reply["profiles"]:
            item = QListWidgetItem(("● " if p["enabled"] else "○ ") + p["name"])
            item.setData(Qt.ItemDataRole.UserRole, p["name"])
            self.list.addItem(item)
            if p["name"] == self._current:
                self.list.setCurrentItem(item)
        if self._current is None and self.list.count():
            self.list.setCurrentRow(0)
            self._current = self.list.item(0).data(Qt.ItemDataRole.UserRole)
        self.list.blockSignals(False)
        self._loading = False
        self._fill()
        problems = [e for e in [reply.get("error"), *reply.get("queue_errors", [])] if e]
        if problems:
            self.banner.setText("Problem: " + problems[0])

    def _fill(self) -> None:
        p = next((x for x in self._status.get("profiles", []) if x["name"] == self._current), None)
        self.editor.setEnabled(p is not None)
        self.b_remove.setEnabled(p is not None)
        if p is None:
            self.title.setText("No profile selected")
            self.running.setText("")
            self.counters.setText("")
            return
        self._loading = True
        self.title.setText(p["name"])
        self.active.setChecked(p["enabled"])
        if not self.match.hasFocus():
            self.match.setText(", ".join(p["match"]))
        self.block_out.setChecked(p["block_out"])
        self.block_in.setChecked(p["block_in"])
        if not self._delay_timer.isActive():
            if not self.delay_out.hasFocus():
                self.delay_out.setValue(p["delay_out_ms"])
            if not self.delay_in.hasFocus():
                self.delay_in.setValue(p["delay_in_ms"])
        self._loading = False
        run = p.get("running") or []
        self.running.setText("Running now: " + ", ".join(run) if run else
                             "Not running right now. It will be picked up automatically when it starts.")
        bits = []
        for d, label in (("out", "outgoing"), ("in", "incoming")):
            c = (p.get("counters") or {}).get(d)
            if c:
                bits.append(f"{label}: {PAST.get(c['verdict'], c['verdict'])} {c['packets']:,} packets")
        self.counters.setText("   ".join(bits))


def run() -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("portcullis")
    w = Window()
    w.show()
    return app.exec()
