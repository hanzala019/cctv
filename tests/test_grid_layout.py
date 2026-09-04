"""Tests for core.ui.widgets.grid_layout -- pure math, no GUI."""

import pytest

from core.ui.widgets.grid_layout import grid_dimensions, tile_positions


@pytest.mark.parametrize("n,expected", [
    (0, (1, 1)), (1, (1, 1)), (2, (1, 2)), (3, (2, 2)), (4, (2, 2)),
    (5, (2, 3)), (9, (3, 3)), (10, (3, 4)), (16, (4, 4)), (17, (4, 5)),
])
def test_grid_dimensions(n, expected):
    assert grid_dimensions(n) == expected


def test_grid_always_fits_every_camera():
    for n in range(1, 65):
        rows, cols = grid_dimensions(n)
        assert rows * cols >= n


def test_tile_positions_are_reading_order():
    assert tile_positions(5) == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1)]


def test_tile_positions_unique_and_complete():
    for n in range(1, 33):
        pos = tile_positions(n)
        assert len(pos) == n
        assert len(set(pos)) == n
