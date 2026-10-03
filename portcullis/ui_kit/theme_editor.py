"""
ThemeEditorGroup: a ready-made Settings section for editing an app's
ThemeSettings. Mirrors afterglow's Settings > General toggles plus
Settings > Advanced color rows and "Revert" buttons, in one widget.

Usage inside an app's settings page:

    editor = ThemeEditorGroup(
        current=my_config.theme,               # the app's ThemeSettings (will not be mutated)
        defaults=ThemeSettings(**MY_APP_DEFAULTS),  # this APP's defaults, not the kit's placeholders
        labels={"color_accent": "Accent (buttons, outlines)", ...},  # optional: app-specific wording
    )
    layout.addWidget(editor)
    ...
    # on the page's Save button:
    editor.apply_to(my_config.theme)
    my_config.save()

Nothing is written until apply_to(). Kit widgets read settings at
construction, so after saving, rebuild the affected pages or tell the
user to restart. Afterglow shows a note to that effect.

"Revert to Default Colors" resets only color_* fields in the editor.
"Revert to Default Settings" resets every field. Both only change the
editor's fields; the user still has to press Save. These buttons exist
because, more than once, a stale saved color looked exactly like a
rendering bug. Try reverting before debugging colors.
"""
from __future__ import annotations

from dataclasses import fields

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QFormLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from .custom_button import CustomButton
from .custom_checkbox import CustomCheckBox
from .custom_group_box import CustomGroupBox
from .custom_line_edit import CustomLineEdit
from .custom_spinbox import CustomSpinBox
from .theme_config import COLOR_FIELDS, ThemeSettings

_DEFAULT_LABELS = {
    "custom_widgets_enabled": "Custom Widgets",
    "app_theme_enabled": "Use App Theme Colors (off = system palette)",
    "rounded_corners_enabled": "Rounded Corners",
    "rounded_corner_radius": "Corner Radius (px)",
    "ui_padding": "Padding (px)",
    "color_accent": "Accent (buttons, outlines):",
    "color_surface": "Surface (panels, fields):",
    "color_app_background": "App Background:",
    "color_page_background": "Page Background:",
    "color_highlight": "Navigation Buttons:",
    "color_text": "Text:",
    "color_text_outline": "Text Outline:",
    "text_outline_width": "Text Outline Width (px)",
}


class ThemeEditorGroup(QWidget):
    def __init__(self, current: ThemeSettings, defaults: ThemeSettings,
                 labels: "dict[str, str] | None" = None, parent=None):
        super().__init__(parent)
        self._defaults = defaults
        self._labels = {**_DEFAULT_LABELS, **(labels or {})}
        self._bools: dict[str, QCheckBox] = {}
        self._ints: dict[str, CustomSpinBox] = {}
        self._colors: dict[str, CustomLineEdit] = {}
        self._outline_width_spin: "CustomSpinBox | None" = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        general = CustomGroupBox("Appearance")
        g = general.make_layout(QFormLayout)
        for name in ("custom_widgets_enabled", "app_theme_enabled", "rounded_corners_enabled"):
            box = CustomCheckBox(self._labels[name])
            self._bools[name] = box
            g.addRow(box)
        for name, lo, hi in (("rounded_corner_radius", 0, 64), ("ui_padding", 0, 64)):
            spin = CustomSpinBox()
            spin.setRange(lo, hi)
            self._ints[name] = spin
            g.addRow(self._labels[name], spin)
        outer.addWidget(general)

        colors = CustomGroupBox("Colors")
        c = colors.make_layout(QFormLayout)
        for name in COLOR_FIELDS:
            row = QHBoxLayout()
            edit = CustomLineEdit()
            edit.setMaxLength(9)  # "#RRGGBB" or "#AARRGGBB"
            pick = CustomButton("Pick...")
            pick.clicked.connect(lambda _=False, e=edit: self._pick_color(e))
            row.addWidget(edit, stretch=1)
            row.addWidget(pick)
            self._colors[name] = edit
            c.addRow(self._labels[name], row)
        spin = CustomSpinBox()
        spin.setRange(0, 10)
        self._outline_width_spin = spin
        c.addRow(self._labels["text_outline_width"], spin)
        note = QLabel("Colors apply when both Custom Widgets and App Theme Colors are on. "
                      "Reopen the app after saving to see changes everywhere.")
        note.setWordWrap(True)
        c.addRow(note)
        outer.addWidget(colors)

        reset = CustomGroupBox("Reset")
        r = reset.make_layout(QVBoxLayout)
        revert_colors = CustomButton("Revert to Default Colors")
        revert_colors.clicked.connect(self.revert_colors)
        revert_all = CustomButton("Revert to Default Settings")
        revert_all.clicked.connect(self.revert_all)
        r.addWidget(revert_colors)
        r.addWidget(revert_all)
        outer.addWidget(reset)

        self._load_from(current)

    # ------------------------------------------------------------ load / save

    def _load_from(self, s: ThemeSettings, only_colors: bool = False) -> None:
        for name, edit in self._colors.items():
            edit.setText(getattr(s, name))
        if only_colors:
            return
        for name, box in self._bools.items():
            box.setChecked(bool(getattr(s, name)))
        for name, spin in self._ints.items():
            spin.setValue(int(getattr(s, name)))
        self._outline_width_spin.setValue(round(s.text_outline_width))

    def apply_to(self, target: ThemeSettings) -> list[str]:
        """Write the editor's values into `target`. Invalid color strings
        are skipped (target keeps its old value) and their field names
        are returned, so the caller can warn the user."""
        invalid = []
        for name, box in self._bools.items():
            setattr(target, name, box.isChecked())
        for name, spin in self._ints.items():
            setattr(target, name, spin.value())
        target.text_outline_width = float(self._outline_width_spin.value())
        for name, edit in self._colors.items():
            value = edit.text().strip()
            if QColor.isValidColorName(value) and value.startswith("#"):
                setattr(target, name, value.lower())
            else:
                invalid.append(name)
        return invalid

    def revert_colors(self) -> None:
        self._load_from(self._defaults, only_colors=True)

    def revert_all(self) -> None:
        self._load_from(self._defaults)

    def _pick_color(self, edit: CustomLineEdit) -> None:
        start = QColor(edit.text()) if QColor.isValidColorName(edit.text()) else QColor("#808080")
        color = QColorDialog.getColor(start, self)
        if color.isValid():
            edit.setText(color.name())


def settings_field_names() -> tuple[str, ...]:
    return tuple(f.name for f in fields(ThemeSettings))
