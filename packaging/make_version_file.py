"""Writes packaging/version_info.txt: the version resource Windows shows in the properties of the .exe.

Without it the file has no product name, version or publisher, which looks anonymous to people and to
security software. Everything comes from the application's own constants, so it cannot drift.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from techxterminal import APP_NAME, AUTHOR, __version__

numbers = [int(part) for part in re.findall(r"\d+", __version__)][:4]
numbers += [0] * (4 - len(numbers))
as_tuple = ", ".join(str(number) for number in numbers)
as_text = ".".join(str(number) for number in numbers)

text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({as_tuple}), prodvers=({as_tuple}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{AUTHOR}'),
      StringStruct('FileDescription', '{APP_NAME}'),
      StringStruct('FileVersion', '{as_text}'),
      StringStruct('InternalName', 'TechXTerminal'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 {AUTHOR}. GNU GPL v3.'),
      StringStruct('OriginalFilename', 'TechXTerminal.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{as_text}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
target = Path(__file__).with_name("version_info.txt")
target.write_text(text, encoding="utf-8")
print(f"wrote {target}")
