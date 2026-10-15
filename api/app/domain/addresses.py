from __future__ import annotations

import re
from dataclasses import dataclass

MAX_EXCEL_ROWS = 1_048_576
MAX_EXCEL_COLUMNS = 16_384

_CELL = re.compile(r"^\$?([A-Z]{1,3})\$?([0-9]{1,7})$")


def column_index(letters: str) -> int:
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index


def column_letters(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


@dataclass(frozen=True)
class A1Range:
    """1-based inclusive rectangle; contains coordinates only, never cell content."""

    top: int
    left: int
    bottom: int
    right: int

    @property
    def rows(self) -> int:
        return self.bottom - self.top + 1

    @property
    def columns(self) -> int:
        return self.right - self.left + 1

    @property
    def cells(self) -> int:
        return self.rows * self.columns

    def intersect(self, other: "A1Range") -> "A1Range | None":
        top, left = max(self.top, other.top), max(self.left, other.left)
        bottom, right = min(self.bottom, other.bottom), min(self.right, other.right)
        if top > bottom or left > right:
            return None
        return A1Range(top, left, bottom, right)

    def to_a1(self) -> str:
        start = f"{column_letters(self.left)}{self.top}"
        if self.cells == 1:
            return start
        return f"{start}:{column_letters(self.right)}{self.bottom}"


def split_sheet(address: str) -> tuple[str | None, str]:
    """Split `'Sheet name'!A1:B2` into the unquoted sheet name and the local address."""
    if "!" not in address:
        return None, address
    sheet, local = address.rsplit("!", 1)
    if len(sheet) >= 2 and sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, local


def _parse_cell(cell: str) -> tuple[int, int]:
    match = _CELL.match(cell)
    if not match:
        raise ValueError("address must use A1 notation")
    column, row = column_index(match.group(1)), int(match.group(2))
    if not 1 <= row <= MAX_EXCEL_ROWS or not 1 <= column <= MAX_EXCEL_COLUMNS:
        raise ValueError("address is outside Excel bounds")
    return row, column


def parse_a1(address: str) -> A1Range:
    _, local = split_sheet(address.strip())
    parts = local.upper().split(":")
    if len(parts) not in (1, 2):
        raise ValueError("address must be a single rectangular range")
    first = _parse_cell(parts[0])
    last = _parse_cell(parts[-1])
    return A1Range(min(first[0], last[0]), min(first[1], last[1]), max(first[0], last[0]), max(first[1], last[1]))
