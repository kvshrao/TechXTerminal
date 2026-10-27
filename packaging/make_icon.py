"""Writes packaging/techx.ico from the application's own painted icon."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from techxterminal.theme import app_icon, load_fonts

app = QApplication(sys.argv)
load_fonts()
target = Path(__file__).with_name("techx.ico")
if not app_icon().pixmap(256, 256).toImage().save(str(target), "ICO"):
    sys.exit("Could not write the icon file")
print(f"wrote {target}")
