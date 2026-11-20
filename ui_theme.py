"""Neutral light palette for Qt controls without a dedicated stylesheet."""
from PySide6.QtGui import QColor, QPalette


def light_palette():
    palette = QPalette()
    colors = {
        "Window": "#ffffff", "WindowText": "#0d0d0d",
        "Base": "#ffffff", "AlternateBase": "#fafafa", "Text": "#0d0d0d",
        "Button": "#f4f4f4", "ButtonText": "#0d0d0d",
        "Highlight": "#e5e5e5", "HighlightedText": "#0d0d0d",
        "PlaceholderText": "#696969", "Link": "#262626", "LinkVisited": "#4d4d4d",
        "ToolTipBase": "#171717", "ToolTipText": "#ffffff",
    }
    for role, value in colors.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#767676"))
    return palette
