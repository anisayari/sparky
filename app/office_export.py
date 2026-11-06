"""Render the visible Markdown reply, tables and citations as editable local Office files."""
import io
import math
import re

MIME = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def cells(line):
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def blocks(text):
    lines, i = text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith(("```", "~~~")):
            fence = line.lstrip()[:3]
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(fence):
                yield "code", lines[i]
                i += 1
        elif ("|" in line and i + 1 < len(lines) and "|" in lines[i + 1]
              and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells(lines[i + 1]))):
            table = [cells(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                table.append(cells(lines[i]))
                i += 1
            if len(table) > 5000 or max(map(len, table)) > 64:
                raise ValueError("Tableau trop volumineux (5 000 lignes et 64 colonnes maximum).")
            yield "table", table
            continue
        elif line.strip():
            yield "text", line
        i += 1


def word(content, out):
    from docx import Document
    doc = Document()
    for kind, value in content:
        if kind == "table":
            width = max(map(len, value))
            if width > 63 or len(value) * width > 20000:
                raise ValueError("Tableau trop volumineux pour Word. Utilisez Excel.")
            table = doc.add_table(rows=0, cols=width)
            table.style = "Light Shading Accent 1"
            for row in value:
                for cell, text in zip(table.add_row().cells, row):
                    cell.text = text
        elif kind == "code":
            doc.add_paragraph().add_run(value).font.name = "Consolas"
        elif heading := re.match(r"^(#{1,6})\s+(.+)", value):
            doc.add_heading(heading[2], len(heading[1]))
        elif re.match(r"^[-*]\s+", value):
            doc.add_paragraph(value[2:], style="List Bullet")
        else:
            doc.add_paragraph(value)
    doc.save(out)


def numeric(cell):
    """Recognize plain numbers and amounts. Keep identifiers, ambiguous separators and formulas literal."""
    match = re.fullmatch(r"([$€£]?)\s*(-?(?:0|[1-9]\d{0,14}|[1-9]\d{0,2}(?:[ \u00a0\u202f]\d{3})+)(?:[.,]\d{1,2})?)\s*([$€£%]?)", cell)
    if not match or (match[1] and match[3]):
        return None
    unit = match[1] or match[3]
    value = float(re.sub(r"\s+", "", match[2]).replace(",", "."))
    if not math.isfinite(value) or abs(value) >= 1e15:
        return None
    return (value / 100 if unit == "%" else value), unit


def excel(content, out):
    import xlsxwriter
    with xlsxwriter.Workbook(out, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False}) as book:
        heading = book.add_format({"bold": True, "font_color": "white", "bg_color": "#334B64", "text_wrap": True})
        units = {u: book.add_format({"num_format": fmt}) for u, fmt in
                 {"": "General", "%": "0.##%", "€": '#,##0.00 "€"', "$": '"$"#,##0.00', "£": '"£"#,##0.00'}.items()}
        count = 0
        notes = []
        for kind, value in content:
            if kind != "table":
                notes.append(value)
                continue
            count += 1
            sheet = book.add_worksheet(f"Table {count}")
            width = max(map(len, value))
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, len(value) - 1, width - 1)
            for col in range(width):
                sheet.set_column(col, col, min(60, max(12, max((len(r[col]) for r in value if col < len(r)), default=0) + 2)))
            totals = [[] for _ in range(width)]
            for row_index, row in enumerate(value):
                is_total = row_index == len(value) - 1 and re.fullmatch(r"(grand total|total(?: général)?)", row[0].strip("* "), re.I)
                for col, text in enumerate(row):
                    number = numeric(text.strip("* ")) if row_index else None
                    if number is None:
                        sheet.write_string(row_index, col, text, None if row_index else heading)
                        continue
                    amount, unit = number
                    previous = totals[col]
                    # Generate a SUM only when every preceding data row agrees with the displayed amount and unit.
                    if (is_total and col > 0 and previous and len(previous) == row_index - 1
                            and all(u == unit for _, u in previous) and math.isclose(sum(n for n, _ in previous), amount, abs_tol=1e-8)):
                        letter = xlsxwriter.utility.xl_col_to_name(col)
                        sheet.write_formula(row_index, col, f"=SUM({letter}2:{letter}{row_index})", units[unit], amount)
                    else:
                        sheet.write_number(row_index, col, amount, units[unit])
                    previous.append(number)
        if notes or not count:
            sheet = book.add_worksheet("Notes")
            wrap = book.add_format({"text_wrap": True, "valign": "top"})
            sheet.set_column(0, 0, 90)
            for ix, line in enumerate(notes):
                sheet.write_string(ix, 0, line, wrap)


def render(fmt, text):
    if fmt not in MIME or not isinstance(text, str) or not text.strip() or len(text) > 200_000:
        raise ValueError("Choisissez une réponse (200 000 caractères maximum) et le format Word ou Excel.")
    clean = CONTROL.sub("", text)
    if fmt == "xlsx" and any(len(line) > 32767 for line in clean.splitlines()):
        raise ValueError("Une ligne dépasse la limite de 32 767 caractères d'Excel. Utilisez Word.")
    out = io.BytesIO()
    content = list(blocks(clean))
    (word if fmt == "docx" else excel)(content, out)
    return out.getvalue()
