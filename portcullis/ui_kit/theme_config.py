"""
The ONE place the UI kit reads its settings from.

Every widget in ui_kit calls `get_settings()` and reads fields off the
returned ThemeSettings. Nothing in the kit knows where an app keeps its
config. The host app wires that up once at startup:

    from ui_kit import theme_config
    theme_config.set_settings_provider(lambda: my_app_config.load().theme)

The provider must return a ThemeSettings instance (or any object with the
same attribute names). It's called often: from constructors and from
some paintEvents. It MUST be cheap. Cache the config, or re-read the
file only when its mtime changes. Afterglow measured 400+ calls per
Library refresh when its load() re-parsed TOML every time, and that was
over half the refresh time. See UI_THEMING_GUIDE.md, section 7, pitfall 1.

If no provider is set, the kit uses a module-level default
ThemeSettings(), so widgets still render in the monochrome placeholder
scheme. That's handy for tests and demos.

Colors differ per app on purpose. Each app keeps its own ThemeSettings
defaults and its own saved values. Don't import another app's palette.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable


@dataclass
class ThemeSettings:
    # ---- master switches ------------------------------------------------
    # custom_widgets_enabled: WHETHER the kit custom-paints at all.
    #   (Afterglow calls this "Custom Buttons".) Off = the app should use
    #   plain native Qt widgets instead. The kit widgets themselves don't
    #   check it; the app decides which class to construct. See guide.
    custom_widgets_enabled: bool = True
    # app_theme_enabled: WHICH colors custom-painted widgets use.
    #   On  = the fixed hex colors below.
    #   Off = sampled from the live system/KDE palette (QApplication.palette()).
    #   (Afterglow calls this "Afterglow Theme".)
    app_theme_enabled: bool = True

    # ---- shape / spacing -------------------------------------------------
    rounded_corners_enabled: bool = True
    rounded_corner_radius: int = 24      # px; clamped per-shape to half its short side
    ui_padding: int = 14                 # the ONE shared gap/margin value

    # ---- color roles (hex strings, "#RRGGBB" or "#AARRGGBB") -------------
    # PLACEHOLDER MONOCHROME SCHEME. Replace per app with the colors the
    # user provides. The ROLE names are shared across apps; the VALUES
    # are not.
    color_accent: str = "#4a4a4a"          # buttons, checkbox/scrollbar/lineedit outlines, scrollbar handle, group-box borders
    color_surface: str = "#2b2b2b"         # raised panels/cards, text-field + checkbox fill, scrollbar track, combo box fill
    color_app_background: str = "#1e1e1e"  # the main window's own background
    color_page_background: str = "#161616" # content-page / list-area background, dialog fill
    color_highlight: str = "#6a6a6a"       # nav/segment buttons (sidebar, page-switch tabs)
    color_text: str = "#e6e6e6"            # body text on surfaces, labels in checkboxes/dialogs
    color_text_outline: str = "#101010"    # OutlinedLabel stroke color
    text_outline_width: float = 1.0        # OutlinedLabel stroke width, px


# Field groups, used by the theme editor's "Revert" buttons.
COLOR_FIELDS = tuple(f.name for f in fields(ThemeSettings) if f.name.startswith("color_"))
ALL_FIELDS = tuple(f.name for f in fields(ThemeSettings))


_default = ThemeSettings()
_provider: "Callable[[], ThemeSettings] | None" = None


def set_settings_provider(provider: "Callable[[], ThemeSettings] | None") -> None:
    """Install the function the kit calls to get current settings. Call
    once at startup, BEFORE constructing any kit widget."""
    global _provider
    _provider = provider


def get_settings() -> ThemeSettings:
    """Current settings. Treat the result as READ-ONLY: it may be a
    shared cached instance. To change settings, edit the app's own
    config object and save it the normal way."""
    if _provider is not None:
        return _provider()
    return _default
