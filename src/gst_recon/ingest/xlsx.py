"""A read-only XLSX reader, built on the standard library.

Two reasons this is not `openpyxl`.

**The stored text, not a float.** A cell holding ``1234.56`` is stored in the
sheet XML as the literal string ``1234.56``. Every spreadsheet library parses
that into a Python float, and a float is the one type this system forbids for
money. ``domain.money`` defends against that by routing floats back through
``str()``, which round-trips for ordinary currency values -- but a defence you
never have to mount is better than one that usually works. Reading the stored
text hands ``Decimal`` exactly what the file says, and the float never exists.

**A dependency you do not add cannot break.** An XLSX is a zip of XML. For
reading values -- which is all an ingest path needs -- ``zipfile`` and
``xml.etree`` are sufficient, and the format's read side is stable in a way its
write side is not.

What this deliberately does not do: formulas are not evaluated (the value Excel
last cached is returned, and a file written by a tool that cached none yields an
empty cell), styling and merged cells are ignored, and one worksheet is read at
a time. Anything past reading values belongs to a library, not here.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# The formats Excel ships with that mean "this is a date". A date cell holds a
# serial number and nothing else; the only thing separating it from a quantity
# is the format applied to it, so the format has to be read.
BUILTIN_DATE_FORMATS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47})

# Excel reproduces a Lotus 1-2-3 bug: it believes 1900 was a leap year. Serial
# 60 is a 29 February that never existed. Dates from serial 61 onward are
# therefore one day further from the epoch than arithmetic suggests, which is
# why the branches below use different origins rather than one.
_EPOCH_1900_AFTER_BUG = date(1899, 12, 30)
_EPOCH_1900_BEFORE_BUG = date(1899, 12, 31)
_EPOCH_1904 = date(1904, 1, 1)


class XlsxError(ValueError):
    """The file is not a workbook this reader can make sense of."""


def serial_to_date(serial: int, *, date_system_1904: bool = False) -> date:
    """Convert an Excel date serial to a real date."""
    if date_system_1904:
        return _EPOCH_1904 + timedelta(days=serial)
    if serial >= 61:
        return _EPOCH_1900_AFTER_BUG + timedelta(days=serial)
    if serial == 60:
        raise XlsxError("serial 60 is Excel's non-existent 29 February 1900")
    return _EPOCH_1900_BEFORE_BUG + timedelta(days=serial)


def column_index(reference: str) -> int:
    """``"A1"`` -> 0, ``"AB7"`` -> 27.

    Sparse rows omit empty cells entirely, so a cell's position has to come
    from its reference rather than from the order it arrived in.
    """
    index = 0
    for char in reference:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    if index == 0:
        raise XlsxError(f"cell reference {reference!r} has no column")
    return index - 1


@dataclass(frozen=True, slots=True)
class Sheet:
    """One worksheet as text, with date-formatted cells already resolved.

    Every value is a string, numbers included: the caller decides what a figure
    means, and handing it the stored text keeps that decision reversible.
    """

    name: str
    rows: list[list[str]]

    @property
    def header(self) -> list[str]:
        return self.rows[0] if self.rows else []

    @property
    def body(self) -> list[list[str]]:
        return self.rows[1:]


def _text(node: ElementTree.Element | None) -> str:
    return "".join(node.itertext()) if node is not None else ""


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(raw)
    return [_text(item) for item in root.findall(f"{MAIN_NS}si")]


def _strip_literals(code: str) -> str:
    """Remove quoted text and bracketed tokens from a number format.

    A currency format can legitimately contain ``[$R-1009]``, and the ``d`` in
    it says nothing about dates. Looking for date letters before stripping
    would classify that column as dates and silently turn every amount in it
    into a day in 1902.
    """
    for opening, closing in (("[", "]"), ('"', '"')):
        while True:
            start = code.find(opening)
            if start == -1:
                break
            end = code.find(closing, start + 1)
            if end == -1:
                break
            code = code[:start] + code[end + 1 :]
    return code


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    """Style indices whose number format renders as a date."""
    try:
        root = ElementTree.fromstring(archive.read("xl/styles.xml"))
    except KeyError:
        return set()

    custom_date_formats = set()
    for entry in root.iter(f"{MAIN_NS}numFmt"):
        code = _strip_literals((entry.get("formatCode") or "").lower()).split(";")[0]
        if any(letter in code for letter in "ymd"):
            identifier = entry.get("numFmtId")
            if identifier is not None:
                custom_date_formats.add(int(identifier))

    styles = set()
    cell_xfs = root.find(f"{MAIN_NS}cellXfs")
    if cell_xfs is None:
        return styles
    for position, xf in enumerate(cell_xfs.findall(f"{MAIN_NS}xf")):
        format_id = int(xf.get("numFmtId", "0"))
        if format_id in BUILTIN_DATE_FORMATS or format_id in custom_date_formats:
            styles.add(position)
    return styles


def _sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name to its part path, in workbook order."""
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        node.get("Id"): node.get("Target", "")
        for node in relationships.findall(f"{RELS_NS}Relationship")
    }
    paths: dict[str, str] = {}
    for sheet in workbook.iter(f"{MAIN_NS}sheet"):
        target = targets.get(sheet.get(f"{DOC_REL_NS}id", ""), "")
        if not target:
            continue
        # A relationship target is either absolute from the package root
        # ("/xl/worksheets/sheet1.xml") or relative to the part that declared
        # it ("worksheets/sheet1.xml"). Both are written in the wild.
        normalised = target.lstrip("/") if target.startswith("/") else "xl/" + target
        paths[sheet.get("name") or ""] = normalised
    return paths


def _uses_1904(archive: zipfile.ZipFile) -> bool:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    properties = workbook.find(f"{MAIN_NS}workbookPr")
    if properties is None:
        return False
    return properties.get("date1904", "0") in {"1", "true"}


def _text_cell(cell: ElementTree.Element, kind: str, shared: list[str]) -> str:
    """Every cell type whose value is text rather than a number."""
    if kind == "s":
        raw = _text(cell.find(f"{MAIN_NS}v"))
        return shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
    if kind == "inlineStr":
        return _text(cell.find(f"{MAIN_NS}is")).strip()
    return _text(cell.find(f"{MAIN_NS}v")).strip()


def _cell_value(
    cell: ElementTree.Element,
    shared: list[str],
    date_styles: set[int],
    *,
    date_system_1904: bool,
) -> str:
    kind = cell.get("t", "n")
    if kind != "n":
        return _text_cell(cell, kind, shared)

    raw = _text(cell.find(f"{MAIN_NS}v")).strip()
    style = cell.get("s")
    if raw and style is not None and int(style) in date_styles:
        try:
            # A date cell may carry a time fraction. The day is what matters.
            return serial_to_date(int(float(raw)), date_system_1904=date_system_1904).isoformat()
        except (ValueError, OverflowError, XlsxError):
            # An out-of-range serial is data this reader cannot interpret, not
            # data it may discard: the stored text goes through untouched and
            # the row fails later, where the failure can name the column.
            return raw
    return raw


def read_sheet(path: Path | str, sheet_name: str | None = None) -> Sheet:
    """Read one worksheet. Defaults to the first sheet in workbook order."""
    location = Path(path)
    if not location.exists():
        raise XlsxError(f"no such workbook: {location}")
    try:
        archive = zipfile.ZipFile(location)
    except zipfile.BadZipFile as exc:
        raise XlsxError(f"{location.name} is not a zip archive, so it is not an .xlsx") from exc

    with archive:
        try:
            paths = _sheet_paths(archive)
        except KeyError as exc:
            # A zip missing xl/workbook.xml is some other kind of archive that
            # happens to be named .xlsx. Saying so beats a KeyError from three
            # frames down in the standard library.
            raise XlsxError(f"{location.name} is a zip but not a workbook: {exc}") from exc
        if not paths:
            raise XlsxError(f"{location.name} declares no worksheets")
        if sheet_name is None:
            sheet_name = next(iter(paths))
        elif sheet_name not in paths:
            raise XlsxError(f"no sheet named {sheet_name!r}; workbook has: {', '.join(paths)}")

        shared = _shared_strings(archive)
        date_styles = _date_styles(archive)
        uses_1904 = _uses_1904(archive)
        root = ElementTree.fromstring(archive.read(paths[sheet_name]))

    # Rows are placed by the row number the file gives them, not by the order
    # they appear in. An empty row is frequently omitted from the XML entirely,
    # and appending as they arrive would silently close the gap -- shifting
    # every row number after it, which is the number a rejection message sends
    # a reviewer to.
    by_number: dict[int, list[str]] = {}
    for position, row in enumerate(root.iter(f"{MAIN_NS}row"), start=1):
        values: dict[int, str] = {}
        for order, cell in enumerate(row.findall(f"{MAIN_NS}c")):
            reference = cell.get("r")
            index = column_index(reference) if reference else order
            values[index] = _cell_value(cell, shared, date_styles, date_system_1904=uses_1904)
        number = int(row.get("r") or position)
        by_number[number] = (
            [values.get(index, "") for index in range(max(values) + 1)] if values else []
        )

    if not by_number:
        return Sheet(name=sheet_name, rows=[])

    rows = [by_number.get(number, []) for number in range(1, max(by_number) + 1)]
    # A trailing run of blank rows is an artefact of how the file was saved,
    # not data. Interior blanks stay exactly where the file put them.
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    return Sheet(name=sheet_name, rows=rows)
