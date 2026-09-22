"""Half-block renderer: the doom-ascii color stream becomes ▀/▄/█ cells
with truecolor fg/bg — two vertical pixels per terminal cell."""

from rich.text import Text

from replicanta import tui_views

RED = "\x1b[38;2;255;0;0m"
BLUE = "\x1b[38;2;0;0;255m"
GREEN = "\x1b[38;2;0;255;0m"
BLACK = "\x1b[38;2;0;0;0m"
RESET = "\x1b[0m"
PX = "██"  # one pixel = two identical chars in block mode


def test_halfblock_merges_vertical_pixel_pairs():
    # 2x2 px: top row red/blue, bottom row red/black
    frame = f"{RED}{PX}{BLUE}{PX}\n{RED}{PX}{BLACK}{PX}{RESET}"
    text = tui_views.doom_halfblock_text(frame)
    assert isinstance(text, Text)
    assert text.plain == "█▀"
    # equal pair -> full block in that color
    assert text.spans[0].style.color.triplet == (255, 0, 0)
    # colored over black -> upper half block, fg colored, bg black
    assert text.spans[1].style.color.triplet == (0, 0, 255)
    assert text.spans[1].style.bgcolor.triplet == (0, 0, 0)


def test_halfblock_black_top_colored_bottom():
    frame = f"{BLACK}{PX}\n{GREEN}{PX}{RESET}"
    text = tui_views.doom_halfblock_text(frame)
    assert text.plain == "▄"
    assert text.spans[0].style.color.triplet == (0, 255, 0)
    assert text.spans[0].style.bgcolor.triplet == (0, 0, 0)


def test_halfblock_black_pair_is_plain_space():
    frame = f"{BLACK}{PX}{RESET}"
    text = tui_views.doom_halfblock_text(frame)
    assert text.plain == " "
    assert not text.spans


def test_halfblock_two_distinct_colors_share_one_cell():
    frame = f"{RED}{PX}\n{BLUE}{PX}{RESET}"
    text = tui_views.doom_halfblock_text(frame)
    assert text.plain == "▀"
    assert text.spans[0].style.color.triplet == (255, 0, 0)
    assert text.spans[0].style.bgcolor.triplet == (0, 0, 255)


def test_halfblock_multi_row_height_halves():
    rows = "\n".join(f"{RED}{PX * 4}{RESET}" for _ in range(6))
    text = tui_views.doom_halfblock_text(rows)
    assert len(text.plain.splitlines()) == 3  # 6 pixel rows -> 3 cells


def test_halfblock_unparseable_returns_none():
    assert tui_views.doom_halfblock_text("") is None
    assert tui_views.doom_halfblock_text(RESET) is None
