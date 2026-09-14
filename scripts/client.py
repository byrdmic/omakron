"""Run the local socket client directly from a plugin checkout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omakron.client import main  # noqa: E402 - locate this checkout first

if __name__ == "__main__":
    sys.exit(main())
