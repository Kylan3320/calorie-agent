from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from calorie_agent.legacy_app import *  # noqa: F401,F403,E402
from calorie_agent.legacy_app import main  # noqa: E402


if __name__ == "__main__":
    main()
