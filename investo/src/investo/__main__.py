"""``python -m investo`` — equivalent to the ``investo`` console script.

Both paths exist because the console script only works after an install, and
``python -m investo`` works from a checkout. They share :func:`investo.cli.main`, so there is
one place where an exit code is decided.
"""

from __future__ import annotations

import sys

from investo.cli import main

if __name__ == "__main__":
    sys.exit(main())
