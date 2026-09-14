#!/usr/bin/env python3
"""Run the preview-first installer without needing installed dependencies."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omakron.manage import main  # noqa: E402

raise SystemExit(main())
