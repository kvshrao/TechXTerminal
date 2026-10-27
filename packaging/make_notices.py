"""Writes packaging/THIRD_PARTY_NOTICES.txt: the licences of the software that is bundled with the application.

The licences of libraries such as paramiko (LGPL) require that their text goes with every copy that is handed
out. It is gathered from what is installed, so it follows the versions that are really in the build; the
gathering is the application's own (techxterminal/licences.py), which also shows it on the About page.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from techxterminal import licences

target = Path(__file__).with_name(licences.NOTICES_FILE)
target.write_text(licences._gather(), encoding="utf-8")
print(f"wrote {target} ({target.stat().st_size // 1024} KB)")
