"""
ui_kit -- custom-painted, themeable PySide6 widgets shared (by copy)
across afterglow, Conduit and Puppetry. Read UI_THEMING_GUIDE.md first.

Wire settings once at startup, before building any widget:

    from .ui_kit import theme_config
    theme_config.set_settings_provider(lambda: app_config.load_readonly().theme)
"""
from .theme_config import ThemeSettings, get_settings, set_settings_provider
from .theme import Theme, contrast_text
from .rounded_rect import rounded_rect_path, round_pixmap_corners
from .press_pulse import PressPulse, scaled_cached
from .custom_button import CustomButton
from .segment_button import SegmentButton
from .custom_checkbox import CustomCheckBox
from .custom_radio_button import CustomRadioButton
from .custom_spinbox import CustomSpinBox, CustomDoubleSpinBox
from .custom_line_edit import CustomLineEdit
from .custom_group_box import CustomGroupBox
from .custom_combo_style import combo_box_stylesheet
from .custom_scrollbar import CustomScrollBar
from .smooth_scroll_area import SmoothScrollArea
from .custom_message_dialog import CustomMessageDialog, show_message
from .outlined_label import OutlinedLabel
from .collapse_toggle_button import CollapseToggleButton
from .page_outline import paint_page_outline
from .scaling import compute_scale
from .scale_reveal import crossfade_to_index, reveal_from_point, animate_popup_from_point
from .theme_editor import ThemeEditorGroup
