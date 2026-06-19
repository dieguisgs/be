#!/usr/bin/env python3
"""
Run the Baltic Exchange TCE scraper as a plain Python application:

    python run.py                       # all routes -> JSON + Excel
    python run.py --routes TD02,TC05    # only these routes
    python run.py --list                # list available routes

It accepts exactly the same arguments as the ``baltic-scraper`` command.
This works without installing the package (it adds ``src/`` to the path).
"""

import sys
from pathlib import Path

# Allow running straight from a checkout without `pip install`.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from baltic_scraper.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
