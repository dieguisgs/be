"""
Module entry point so the package runs as a Python application:

    python -m baltic_scraper --routes TD02,TC05
"""

import sys

from baltic_scraper.cli import main

if __name__ == "__main__":
    sys.exit(main())
