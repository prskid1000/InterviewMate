"""Dark QSS for the settings window — palette + card/sidebar patterns
adapted from voxtype's qt_theme.py."""
BG, BG_ELEV, BG_CARD, BG_ROW, BG_HOVER = "#0c0f14", "#10141c", "#151a24", "#1a2030", "#1d2334"
FG, FG_DIM, FG_MUTE = "#e6ebf2", "#8a96aa", "#4f5a70"
ACCENT, ACCENT_2, OK = "#6ba4ff", "#56e0c2", "#56e0c2"
BORDER, BORDER_SOFT = "#1e2636", "#171d28"

QSS = f"""
* {{ color: {FG}; font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
     font-size: 13px; letter-spacing: 0.2px; }}
QWidget#window_root, QWidget#content {{ background: {BG}; }}
QScrollArea {{ background: {BG}; border: none; }}
QLabel {{ background: transparent; font-weight: 600; }}

/* Sidebar */
QListWidget#sidebar {{
    background: {BG_ELEV}; border: none; border-right: 1px solid {BORDER_SOFT};
    outline: 0; padding: 12px 8px; }}
QListWidget#sidebar::item {{
    padding: 10px 14px; border-radius: 8px; color: {FG_DIM};
    margin-bottom: 3px; min-height: 20px; }}
QListWidget#sidebar::item:hover {{ background: {BG_HOVER}; color: {FG}; }}
QListWidget#sidebar::item:selected {{
    background: {BG_CARD}; color: {FG}; border-left: 2px solid {ACCENT}; font-weight: 500; }}

/* Titlebar */
QWidget#titlebar {{ background: {BG_ELEV}; border-bottom: 1px solid {BORDER_SOFT}; }}
QLabel#titlebar_title {{ font-weight: 600; font-size: 13px; letter-spacing: 0.02em; }}

/* Cards */
QFrame.card {{ background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel.card_title {{ font-size: 13.5px; font-weight: 700; letter-spacing: 0.3px; }}
QLabel.card_sub {{ font-size: 11px; color: {FG_DIM}; font-weight: 600; }}
QLabel.page_title {{ font-size: 18px; font-weight: 800; letter-spacing: 0.3px; }}
QLabel.page_sub {{ font-size: 12px; color: {FG_DIM}; font-weight: 500; letter-spacing: 0.2px; }}
QLabel.section_header {{
    font-size: 10px; font-weight: 800; color: {FG_MUTE};
    text-transform: uppercase; letter-spacing: 0.1em; }}
QLabel.row_label {{ font-size: 12.5px; font-weight: 700; }}
QLabel.row_help {{ color: {FG_MUTE}; font-size: 10.5px; font-weight: 500; }}
QFrame.hsep {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}

/* Collapsible header */
QPushButton.collapse {{
    background: transparent; border: none; text-align: left; padding: 6px 2px;
    color: {FG_DIM}; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.07em; }}
QPushButton.collapse:hover {{ color: {FG}; }}

/* Provider row */
QFrame.prow {{ background: {BG_ROW}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame.prow:hover {{ border: 1px solid #2c3a52; }}

/* Inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
    background: #0d1118; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 6px 9px; selection-background-color: {ACCENT}; selection-color: {BG};
    min-height: 18px; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QComboBox {{
    background: #0d1118; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 6px 26px 6px 9px; min-height: 18px; }}
QComboBox:hover, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {FG_DIM}; margin-right: 8px; }}
QComboBox QAbstractItemView {{
    background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 6px;
    selection-background-color: {BG_ROW}; outline: 0; padding: 3px; }}

/* Checkboxes / switches */
QCheckBox {{ spacing: 8px; font-size: 12.5px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {BORDER};
    border-radius: 4px; background: #0d1118; }}
QCheckBox::indicator:hover {{ border: 1px solid {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border: 1px solid {ACCENT}; }}

/* Buttons */
QPushButton {{ background: {BG_ROW}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 6px 13px; font-size: 12px; min-height: 20px; }}
QPushButton:hover {{ border: 1px solid {ACCENT}; background: {BG_HOVER}; }}
QPushButton:disabled {{ color: {FG_MUTE}; }}
QPushButton.primary {{ background: {ACCENT}; color: {BG}; border: 1px solid {ACCENT}; font-weight: 600; }}
QPushButton.primary:hover {{ background: #7db0ff; }}
QPushButton.iconbtn {{ background: transparent; border: none; color: {FG_DIM};
    font-size: 15px; padding: 2px 8px; min-height: 16px; }}
QPushButton.iconbtn:hover {{ color: {FG}; background: {BG_HOVER}; border-radius: 6px; }}

/* Scrollbars */
QScrollBar:vertical {{ background: {BG_ELEV}; width: 11px; margin: 2px 0; border: none; }}
QScrollBar::handle:vertical {{ background: #3a4563; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #5b6a92; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

QToolTip {{ background: {BG_CARD}; border: 1px solid {BORDER}; color: {FG}; padding: 4px 8px; }}
"""
