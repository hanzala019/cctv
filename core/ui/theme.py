"""
ui_theme.py

Shared dark-theme color palette and common widget styles, used by both
main.py and settings_panel.py. Pulled out into its own module so the
two files don't need to import from each other just to share a button
style -- avoids a circular import between main.py (which will own
SettingsPanel's parent window) and settings_panel.py (which needs the
same look-and-feel).
"""

from PyQt6.QtGui import QColor

# ---- palette ---------------------------------------------------------
# A small, named set of colors used throughout instead of scattering
# hex literals -- keeps the dark theme consistent and easy to retune.
COLOR_BG = "#25282C"            # app background, behind/between tiles
COLOR_TILE_BG = "#000000"       # video tile background (letterboxing)
COLOR_CAPTION_BG = "#1d2024"    # caption bar behind name/status
COLOR_CAPTION_BORDER = "#2a2e33"
COLOR_TEXT_PRIMARY = "#e8e9eb"  # camera name
COLOR_TEXT_MUTED = "#7b818a"    # idle/secondary text
COLOR_STATUS_CONNECTING = "#e8b339"  # amber
COLOR_STATUS_ERROR = "#e0524a"       # red
COLOR_ACCENT = "#4a9eff"        # buttons, focus, links
COLOR_PANEL_BG = "#1a1c1f"      # toolbar / dialogs
COLOR_BORDER = "#2a2e33"

# Zone overlay (passive display in grid tiles + single view) -- distinct
# from zone_editor.py's colors since this is read-only, outlines only,
# no fill (keeps thumbnails legible at small sizes).
COLOR_ZONE_OVERLAY_STROKE = QColor("#4a9eff")
COLOR_ZONE_OVERLAY_LABEL_BG = QColor(0, 0, 0, 170)
COLOR_ZONE_OVERLAY_LABEL_TEXT = QColor("#e8e9eb")

# Phase 3: motion detection indicator -- deliberately a different hue
# from the connection-status colors above (amber=connecting, red=error)
# since all three can appear in the same caption bar and need to read
# as distinct concerns at a glance, not shades of "something's wrong."
# Green reads as "activity detected," not an error state.
COLOR_MOTION_ACTIVE = QColor("#3ddc6f")
COLOR_MOTION_ICON_BG = QColor(0, 0, 0, 0)  # icon is drawn with no chip background in the caption bar (sits on COLOR_CAPTION_BG already)
# Zone outline color when that specific zone currently has motion in it
# (single view only -- see VideoLabel). Distinct from both the resting
# zone-outline blue and zone_editor.py's selection-highlight amber/gold,
# so "this zone has motion right now" doesn't get confused with "this
# zone is selected for editing" if both ever appear at once.
COLOR_ZONE_MOTION_STROKE = QColor("#3ddc6f")
COLOR_ZONE_MOTION_FILL = QColor(61, 220, 111, 60)

# Phase 4: optional object-detection bounding-box overlay (off by
# default, toggled per view -- see GridView/SingleView "Show Boxes").
# Deliberately a warm orange -- distinct from zone blue, zone-detection
# teal, and motion green, so "YOLO found something here, right now" is
# never confusable with any of the passive/motion zone states even
# when several could theoretically be visible at once.
COLOR_DETECTION_BOX_STROKE = QColor("#ff9f45")
COLOR_DETECTION_BOX_LABEL_BG = QColor(0, 0, 0, 170)
COLOR_DETECTION_BOX_LABEL_TEXT = QColor("#ff9f45")


def button_style():
    return f"""
        QPushButton {{
            background-color: {COLOR_PANEL_BG};
            color: {COLOR_TEXT_PRIMARY};
            border: 1px solid {COLOR_BORDER};
            border-radius: 4px;
            padding: 6px 14px;
        }}
        QPushButton:hover {{
            border-color: {COLOR_ACCENT};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_CAPTION_BG};
        }}
    """


def input_style():
    """Shared style for QLineEdit/QComboBox-style input widgets."""
    return f"""
        background-color: {COLOR_CAPTION_BG};
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER};
        border-radius: 3px;
        padding: 5px;
    """


def table_style():
    return f"""
        QTableWidget {{
            background-color: {COLOR_CAPTION_BG};
            color: {COLOR_TEXT_PRIMARY};
            gridline-color: {COLOR_BORDER};
            border: 1px solid {COLOR_BORDER};
        }}
        QHeaderView::section {{
            background-color: {COLOR_PANEL_BG};
            color: {COLOR_TEXT_MUTED};
            border: none;
            padding: 4px;
        }}
        QTableWidget::item:selected {{
            background-color: {COLOR_ACCENT};
            color: white;
        }}
    """


def sidebar_style():
    return f"""
        QListWidget {{
            background-color: {COLOR_PANEL_BG};
            color: {COLOR_TEXT_PRIMARY};
            border: none;
            border-right: 1px solid {COLOR_BORDER};
            outline: none;
            padding-top: 6px;
        }}
        QListWidget::item {{
            padding: 10px 16px;
            border: none;
        }}
        QListWidget::item:selected {{
            background-color: {COLOR_CAPTION_BG};
            color: {COLOR_ACCENT};
            border-left: 3px solid {COLOR_ACCENT};
        }}
        QListWidget::item:hover:!selected {{
            background-color: {COLOR_CAPTION_BG};
        }}
    """
