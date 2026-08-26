"""Shared pytest setup.

Two things happen here, both of which must happen before any test
module is imported:

1. The repository root goes on sys.path, so `from cctv...` works
   without needing an editable install.
2. Qt is forced to the offscreen platform plugin, so GUI tests run on
   a CI runner with no display. This must be set before PyQt6 is first
   imported anywhere, which is why it is here and not in a fixture.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
