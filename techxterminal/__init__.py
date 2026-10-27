"""Tech X Terminal - a modern tabbed SSH terminal."""

import sys

# paramiko imports `invoke`, when it is installed, for one purpose: running the commands of `Match exec` lines in an
# ssh_config. This program never evaluates those, and loading invoke takes about a tenth of a second at start.
sys.modules.setdefault("invoke", None)

__version__ = "0.3.10"
__author__ = "Venkata S Kuntamukkala"
AUTHOR = __author__
CAPTION = "Modern Shell Terminal"  # under the name in the top bar
LICENSE_NAME = "GNU GPL v3"
LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"
APP_NAME = "Tech X Terminal"  # how the name is shown; ORG_NAME, SETTINGS_NAME and file names stay as they are
ORG_NAME = "TechX"
SETTINGS_NAME = "TechXTerminal"
