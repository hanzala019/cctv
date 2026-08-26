"""
grid_layout.py

Pure logic (no GUI dependencies) for deciding how to arrange N camera
tiles into a roughly square grid -- 1x1, 2x2, 3x3, 4x4, etc. -- the way
a CCTV monitoring wall typically looks.

Kept separate from the Tkinter code so it's trivially testable.
"""

import math


def grid_dimensions(n):
    """Given n cameras, return (rows, cols) for a roughly square grid
    that fits all of them with the minimum number of empty cells.

    Examples:
        0 cameras -> (1, 1)   (empty placeholder grid)
        1  -> (1, 1)
        2  -> (1, 2)
        3  -> (2, 2)
        4  -> (2, 2)
        5  -> (2, 3)
        9  -> (3, 3)
        10 -> (3, 4)
        16 -> (4, 4)
        17 -> (4, 5)
    """
    if n <= 0:
        return 1, 1

    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return rows, cols


def tile_positions(n):
    """Return a list of (row, col) tuples, one per camera index 0..n-1,
    in reading order (left-to-right, top-to-bottom)."""
    rows, cols = grid_dimensions(n)
    positions = []
    for i in range(n):
        r = i // cols
        c = i % cols
        positions.append((r, c))
    return positions
