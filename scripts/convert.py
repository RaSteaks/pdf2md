"""
pdf2md structured document package converter.

Legacy usage remains supported:
    python scripts/convert.py input.pdf

Preferred CLI:
    python scripts/convert.py convert input.pdf --out output/ --doc-id my-doc
    python scripts/convert.py verify output/my-doc/
    python scripts/convert.py serve output/ --host 127.0.0.1 --port 8000
    python scripts/convert.py fetch --base-url https://example.com/documents/my-doc/ --target manifest.json
    python scripts/convert.py search-manifest output/my-doc/manifest.json --query "Table 2"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Iterable

try:
    import fitz  # pymupdf
except ModuleNotFoundError:  # pragma: no cover - exercised by CLI users without deps
    fitz = None


if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


VERSION = "0.2.0"
MAX_NAME_LEN = 80
DEFAULT_CONFIG: dict[str, Any] = {
    "trusted_base_urls": ["https://example.com/documents/"],
    "cache_dir": ".cache/pdf2md-skill",
    "max_file_size_mb": 20,
    "allowed_extensions": [
        ".json",
        ".md",
        ".html",
        ".csv",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
    ],
    "verify_checksums": True,
    "image_dpi": 300,
    "ocr_enabled": False,
    "table_output": "auto",
    "markdown_layout": "semantic",
    "page_output": "md",
}

SPECIAL_SYMBOLS = ("ƒH", "ƒsc", "ER′", "EG′", "EB′", "EY′", "µs", "±", "Ω", "dB", "MHz", "kHz")
SUSPICIOUS_SYMBOL_RE = re.compile(r"(·H|·sc|ER·|EG·|EB·|EY·|\s·\s)")
SECTION_RE = re.compile(r"^\d+(\.\d+)*\s+[A-Z][A-Za-z0-9 ,:()/'\-\u2013\u2014]+$")
TABLE_CAPTION_RE = re.compile(r"^\s*(table\s+\d+[\w.\-:]?.*)$", re.IGNORECASE)
FIGURE_CAPTION_RE = re.compile(r"^\s*(figure\s+\d+[\w.\-:]?.*)$", re.IGNORECASE)
NUMERIC_LINE_RE = re.compile(r"^[+\-\u2013]?\s*\d+(\.\d+)?$")


class Pdf2MdError(RuntimeError):
    """Raised for clear CLI errors."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _load_config(path: str | None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        config.update(loaded)
    return config


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "-", name.strip(), flags=re.UNICODE).strip("-._")
    cleaned = cleaned or "document"
    if len(cleaned) <= MAX_NAME_LEN:
        return cleaned
    return cleaned[:MAX_NAME_LEN].rstrip("-._")


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _markdown_escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _slug(value: str) -> str:
    return _safe_name(value.lower())[:64]


def _is_section_heading(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) >= 180:
        return False
    if re.match(r"^(ANNEX|APPENDIX)\s+\d+\b$", stripped, re.I):
        return True
    if re.match(r"^(TABLE|FIGURE)\s+\d+[\w.\-]*(\s+\(continued\))?$", stripped, re.I):
        return True
    if SECTION_RE.match(stripped) and not stripped.endswith((".", ";")):
        return True
    letters = re.sub(r"[^A-Za-z]", "", stripped)
    return bool(letters) and len(stripped) <= 120 and stripped == stripped.upper()


def _extract_text_lines(page: Any) -> tuple[list[str], list[dict[str, Any]]]:
    text_dict = page.get_text("dict", sort=True)
    lines: list[str] = []
    rich_lines: list[dict[str, Any]] = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            sizes = [span.get("size", 0) for span in spans if span.get("text", "").strip()]
            max_size = max(sizes) if sizes else 0
            lines.append(text)
            rich_lines.append({"text": text, "size": max_size, "bbox": line.get("bbox")})
    return lines, rich_lines


def _center_inside_bbox(bbox: Iterable[float], container: Iterable[float]) -> bool:
    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    tx0, ty0, tx1, ty1 = container
    return tx0 <= cx <= tx1 and ty0 <= cy <= ty1


def _extract_layout_text(page: Any, exclude_bboxes: list[Iterable[float]] | None = None) -> str:
    exclude_bboxes = exclude_bboxes or []
    if not exclude_bboxes:
        return page.get_text("text", sort=False).strip()

    chunks: list[str] = []
    for block in page.get_text("blocks", sort=False):
        if block[6] != 0:
            continue
        if any(_center_inside_bbox(block[:4], table_bbox) for table_bbox in exclude_bboxes):
            continue
        text = block[4].strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def _fenced_text_block(text: str) -> str:
    if not text:
        return "*[No extractable text - OCR is disabled by default.]*"
    return "```text\n" + text.replace("```", "'''") + "\n```"


def _format_text_lines(rich_lines: list[dict[str, Any]], page_num: int, sections: list[dict[str, Any]]) -> str:
    if not rich_lines:
        return "*[No extractable text - OCR is disabled by default.]*"

    out: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = paragraph[0]
        for part in paragraph[1:]:
            if text.endswith("-"):
                text = text[:-1] + part
            else:
                text += " " + part
        out.append(text)
        paragraph.clear()

    for line in rich_lines:
        text = line["text"]
        is_heading = _is_section_heading(text)
        if is_heading:
            flush_paragraph()
            level = 2 if re.match(r"^(chapter|annex|appendix|\d+\s+)", text, re.I) else 3
            sections.append(
                {
                    "id": _slug(text),
                    "title": text,
                    "level": level - 1,
                    "page_start": page_num,
                    "page_end": page_num,
                    "file": "full.md",
                }
            )
            out.append(f"\n{'#' * level} {text}\n")
            continue

        if re.match(r"^(NOTE\s+\d+|_{3,}|\*)", text):
            flush_paragraph()
            out.append(text)
            continue

        if re.match(r"^([a-z]\)|\d+)$", text.strip(), re.I):
            flush_paragraph()
            paragraph.append(text)
            continue

        paragraph.append(text)

    flush_paragraph()
    return "\n\n".join(out)


def _split_table_line(line: str) -> list[str]:
    if "|" in line:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
    elif "\t" in line:
        cells = [cell.strip() for cell in line.split("\t")]
    else:
        cells = [cell.strip() for cell in re.split(r"\s{2,}", line)]
    return [cell for cell in cells if cell]


def _detect_text_tables(lines: list[str]) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    expected_cols = 0

    for line in lines:
        cells = _split_table_line(line)
        is_table_line = len(cells) >= 2 and (
            "|" in line or "\t" in line or len(re.findall(r"\s{2,}", line)) >= 1
        )
        if is_table_line and (expected_cols in (0, len(cells)) or len(cells) >= 3):
            current.append(cells)
            expected_cols = max(expected_cols, len(cells))
        else:
            if len(current) >= 2:
                tables.append(current)
            current = []
            expected_cols = 0

    if len(current) >= 2:
        tables.append(current)
    return tables


def _find_pdf_tables(page: Any) -> list[Any]:
    if not hasattr(page, "find_tables"):
        return []
    try:
        return list(page.find_tables().tables)
    except Exception:
        return []


def _clean_table_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[ \t]+\n", "\n", str(value)).strip()


def _extract_pdf_table_rows(table: Any) -> list[list[str]]:
    try:
        rows = table.extract()
    except Exception:
        return []
    return [[_clean_table_cell(cell) for cell in row] for row in rows if any(_clean_table_cell(cell) for cell in row)]


def _should_suppress_layout_text(page: Any, pdf_tables: list[Any]) -> bool:
    if not pdf_tables:
        return False

    page_area = max(page.rect.width * page.rect.height, 1)
    table_area = 0.0
    for table in pdf_tables:
        bbox = getattr(table, "bbox", None)
        if bbox:
            x0, y0, x1, y1 = bbox
            table_area += max(x1 - x0, 0) * max(y1 - y0, 0)
        rows = getattr(table, "row_count", 0) or 0
        cols = getattr(table, "col_count", 0) or 0
        if rows >= 5 and cols >= 5:
            return True
    return (table_area / page_area) >= 0.18


def _should_render_page_as_figure(page: Any, lines: list[str], pdf_tables: list[Any]) -> bool:
    if pdf_tables:
        return False
    if not any(FIGURE_CAPTION_RE.match(line.strip()) for line in lines):
        return False
    numeric_lines = sum(1 for line in lines if NUMERIC_LINE_RE.match(line.strip()))
    drawing_count = 0
    try:
        drawing_count = len(page.get_drawings())
    except Exception:
        drawing_count = 0
    return drawing_count >= 5 or numeric_lines >= 8


def _render_page_figure(
    page: Any,
    page_num: int,
    assets_dir: Path,
    doc_root: Path,
    fig_state: dict[str, int],
    manifest_figures: list[dict[str, Any]],
    qa_notes: list[str],
    dpi: int,
) -> str:
    fig_state["count"] += 1
    figure_id = f"figure-{fig_state['count']}"
    filename = f"fig-{fig_state['count']:02d}-page-{page_num:03d}.png"
    image_path = assets_dir / filename
    try:
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        pix.save(str(image_path))
    except Exception as exc:
        qa_notes.append(f"- Page {page_num}: failed to render vector figure page: {exc}")
        return "*[Vector figure detected, but page rendering failed.]*"

    rel_path = _rel(image_path, doc_root)
    title = f"Figure page {page_num}"
    for line in page.get_text("text", sort=False).splitlines():
        if FIGURE_CAPTION_RE.match(line.strip()):
            title = line.strip()
            break
    manifest_figures.append({"id": figure_id, "title": title, "page": page_num, "file": rel_path})
    qa_notes.append(f"- Page {page_num}: vector figure rendered to {rel_path}; verify labels and formulas manually.")
    return f"![{title}]({rel_path})"


def _classify_table(rows: list[list[str]], page_num: int, table_output: str) -> tuple[str, list[str]]:
    if table_output == "html":
        return "high", ["Configured table_output=html."]
    if table_output == "markdown":
        return "low", []

    reasons: list[str] = []
    col_counts = {len(row) for row in rows}
    max_cols = max(col_counts) if col_counts else 0
    text = " ".join(cell for row in rows for cell in row)

    if len(col_counts) > 1:
        reasons.append("Rows have inconsistent column counts.")
    if max_cols > 4:
        reasons.append("Table has more than four columns.")
    if any("\n" in cell for row in rows for cell in row):
        reasons.append("Table contains multiline cells.")
    if re.search(r"[±Ωµƒ′]|[A-Z]{1,3}′|\d+\s*(MHz|kHz|dB|µs)", text):
        reasons.append("Table contains units, symbols, or formula-like content.")
    if len(rows) > 20:
        reasons.append("Large table; HTML is safer for review.")

    return ("high", reasons) if reasons else ("low", [])


def _table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    body = normalized[1:]
    lines = [
        "| " + " | ".join(_markdown_escape_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(_markdown_escape_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def _table_inner_html(rows: list[list[str]], caption: str) -> str:
    if not rows:
        return "<table></table>"
    out = [f"<table><caption>{html.escape(caption)}</caption>"]
    for index, row in enumerate(rows):
        tag = "th" if index == 0 else "td"
        out.append("<tr>" + "".join(f"<{tag}>{html.escape(cell)}</{tag}>" for cell in row) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def _write_table_html(
    path: Path,
    table_title: str,
    page_start: int,
    page_end: int,
    table_html: str,
    notes: Iterable[str],
) -> None:
    notes_html = "".join(f"<p class=\"note\">{html.escape(note)}</p>\n" for note in notes)
    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(table_title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #111; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border: 1px solid #999; padding: 6px 8px; vertical-align: top; }}
    th {{ background: #f3f3f3; }}
    caption {{ text-align: left; font-weight: 600; margin-bottom: 8px; }}
    .note {{ font-size: 13px; margin-top: 16px; }}
  </style>
</head>
<body>
  <h1>{html.escape(table_title)}</h1>
  <p>Source pages: {page_start}-{page_end}</p>
  <div class="table-wrap">
    {table_html}
  </div>
  {notes_html}
</body>
</html>
"""
    path.write_text(content, encoding="utf-8")


def _find_caption(lines: list[str], pattern: re.Pattern[str], fallback: str) -> str:
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return fallback


def _extract_tables_for_page(
    page: Any,
    page_num: int,
    lines: list[str],
    pdf_tables: list[Any],
    tables_dir: Path,
    doc_root: Path,
    table_state: dict[str, int],
    manifest_tables: list[dict[str, Any]],
    qa_notes: list[str],
    table_output: str,
    markdown_table_file: str,
) -> list[str]:
    markdown_blocks: list[str] = []
    if pdf_tables:
        for pdf_table in pdf_tables:
            rows = _extract_pdf_table_rows(pdf_table)
            if not rows:
                qa_notes.append(f"- Page {page_num}: PyMuPDF detected a table but could not extract rows.")
                continue

            table_state["count"] += 1
            table_id = f"table-{table_state['count']}"
            title = _find_caption(lines, TABLE_CAPTION_RE, f"Table {table_state['count']}")
            risk, reasons = _classify_table(rows, page_num, table_output)
            if risk == "low":
                markdown_blocks.append(f"\n### {title}\n\n{_table_to_markdown(rows)}\n")
                manifest_tables.append(
                    {
                        "id": table_id,
                        "title": title,
                        "page_start": page_num,
                        "page_end": page_num,
                        "file": markdown_table_file,
                        "format": "markdown",
                        "risk": "low",
                    }
                )
                continue

            table_file = tables_dir / f"{table_id}.html"
            notes = reasons or ["PyMuPDF table extraction preserved as HTML."]
            _write_table_html(table_file, title, page_num, page_num, _table_inner_html(rows, title), notes)
            markdown_blocks.append(
                f"\n### {title}\n\nComplete table: [Open {title}]({_rel(table_file, doc_root)})\n"
            )
            record: dict[str, Any] = {
                "id": table_id,
                "title": title,
                "page_start": page_num,
                "page_end": page_num,
                "file": _rel(table_file, doc_root),
                "format": "html",
                "risk": "high",
            }
            if getattr(pdf_table, "bbox", None):
                record["bbox"] = [round(value, 3) for value in pdf_table.bbox]
            manifest_tables.append(record)
            qa_notes.append(f"- Page {page_num}: {table_id} saved as HTML. " + " ".join(notes))
        return markdown_blocks

    if not any(TABLE_CAPTION_RE.match(line) for line in lines):
        return markdown_blocks

    detected = _detect_text_tables(lines)

    for rows in detected:
        table_state["count"] += 1
        table_id = f"table-{table_state['count']}"
        title = _find_caption(lines, TABLE_CAPTION_RE, f"Table {table_state['count']}")
        risk, reasons = _classify_table(rows, page_num, table_output)

        if risk == "low":
            markdown_blocks.append(f"\n### {title}\n\n{_table_to_markdown(rows)}\n")
            manifest_tables.append(
                {
                    "id": table_id,
                "title": title,
                "page_start": page_num,
                "page_end": page_num,
                "file": markdown_table_file,
                "format": "markdown",
                "risk": "low",
                }
            )
            continue

        table_file = tables_dir / f"{table_id}.html"
        note = reasons or ["High-risk table preserved as HTML."]
        _write_table_html(table_file, title, page_num, page_num, _table_inner_html(rows, title), note)
        markdown_blocks.append(
            f"\n### {title}\n\nComplete table: [Open {title}]({_rel(table_file, doc_root)})\n"
        )
        manifest_tables.append(
            {
                "id": table_id,
                "title": title,
                "page_start": page_num,
                "page_end": page_num,
                "file": _rel(table_file, doc_root),
                "format": "html",
                "risk": "high",
            }
        )
        qa_notes.append(f"- Page {page_num}: {table_id} saved as HTML. " + " ".join(note))

    return markdown_blocks


def _extract_images_for_page(
    doc: Any,
    page: Any,
    page_num: int,
    lines: list[str],
    assets_dir: Path,
    doc_root: Path,
    fig_state: dict[str, int],
    manifest_figures: list[dict[str, Any]],
    qa_notes: list[str],
) -> list[str]:
    markdown_blocks: list[str] = []
    caption = _find_caption(lines, FIGURE_CAPTION_RE, "")

    for img_info in page.get_images(full=True):
        xref = img_info[0]
        fig_state["count"] += 1
        figure_id = f"figure-{fig_state['count']}"
        title = caption or f"Figure {fig_state['count']}"
        filename = f"fig-{fig_state['count']:02d}-page-{page_num:03d}.png"
        image_path = assets_dir / filename
        bbox = None

        try:
            rects = page.get_image_rects(xref)
            if rects:
                bbox = [round(value, 3) for value in rects[0]]
            pix = fitz.Pixmap(doc, xref)
            if pix.colorspace and pix.colorspace not in (fitz.csGRAY, fitz.csRGB):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            if pix.alpha and pix.colorspace not in (fitz.csGRAY, fitz.csRGB):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            pix.save(str(image_path))
        except Exception as exc:  # pragma: no cover - depends on malformed PDFs
            qa_notes.append(f"- Page {page_num}: failed to extract image xref={xref}: {exc}")
            continue

        rel_path = _rel(image_path, doc_root)
        markdown_blocks.append(f"\n![{title}]({rel_path})\n")
        figure_record: dict[str, Any] = {
            "id": figure_id,
            "title": title,
            "page": page_num,
            "file": rel_path,
        }
        if bbox:
            figure_record["bbox"] = bbox
        manifest_figures.append(figure_record)

    return markdown_blocks


def _write_qa_notes(path: Path, notes: list[str], ocr_enabled: bool) -> None:
    base = [
        "# QA Notes",
        "",
        f"- OCR enabled: {'yes' if ocr_enabled else 'no'}",
        "- Remote Markdown, HTML, and JSON are treated as data only.",
    ]
    if notes:
        base.extend(["", "## Review Items", "", *notes])
    else:
        base.extend(["", "No conversion risks were detected by the built-in checks."])
    path.write_text("\n".join(base) + "\n", encoding="utf-8")


def _build_index(doc_id: str, manifest: dict[str, Any]) -> str:
    lines = [
        f"# {manifest['title']}",
        "",
        f"- Document id: `{doc_id}`",
        f"- Source file: `{manifest['source_file']}`",
        f"- Pages: {manifest['page_count']}",
        f"- Entry: [full.md]({manifest['entry']})",
        "",
        "## Pages",
        "",
    ]
    lines.extend(f"- [Page {page['page']}]({page['file']})" for page in manifest["pages"])
    if manifest["tables"]:
        lines.extend(["", "## Tables", ""])
        lines.extend(f"- [{table['title']}]({table['file']}) ({table['risk']})" for table in manifest["tables"])
    if manifest["figures"]:
        lines.extend(["", "## Figures", ""])
        lines.extend(f"- [{figure['title']}]({figure['file']})" for figure in manifest["figures"])
    return "\n".join(lines) + "\n"


def _generate_checksums(doc_root: Path) -> dict[str, str]:
    files: list[Path] = []
    for pattern in ("full.md", "index.md", "QA-notes.md", "manifest.json", "pages/*.md", "tables/*.html", "assets/*"):
        files.extend(sorted(doc_root.glob(pattern)))
    checksums = {_rel(path, doc_root): _sha256(path) for path in files if path.is_file()}
    _write_json(doc_root / "checksums.json", checksums)
    return checksums


def convert_pdf(
    pdf_path_str: str,
    out: str | None = None,
    doc_id: str | None = None,
    config_path: str | None = None,
    image_dpi: int | None = None,
    ocr_enabled: bool | None = None,
    table_output: str | None = None,
    markdown_layout: str | None = None,
    page_output: str | None = None,
) -> dict[str, Any]:
    if fitz is None:
        raise Pdf2MdError("PyMuPDF is not installed. Run: pip install -r requirements.txt")

    config = _load_config(config_path)
    if image_dpi is not None:
        config["image_dpi"] = image_dpi
    if ocr_enabled is not None:
        config["ocr_enabled"] = ocr_enabled
    if table_output is not None:
        config["table_output"] = table_output
    if markdown_layout is not None:
        config["markdown_layout"] = markdown_layout
    if page_output is not None:
        config["page_output"] = page_output
    if config["page_output"] not in ("md", "none"):
        raise Pdf2MdError("page_output must be 'md' or 'none'.")

    pdf_path = Path(pdf_path_str).resolve()
    if not pdf_path.exists():
        raise Pdf2MdError(f"File not found: {pdf_path}")

    original_name = pdf_path.stem
    doc_id = _safe_name(doc_id or original_name)
    root = Path(out).resolve() if out else pdf_path.parent
    doc_root = root / doc_id
    pages_dir = doc_root / "pages"
    assets_dir = doc_root / "assets"
    tables_dir = doc_root / "tables"
    for directory in (doc_root, assets_dir, tables_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if config["page_output"] == "md":
        pages_dir.mkdir(parents=True, exist_ok=True)
    elif pages_dir.exists():
        for stale_page in pages_dir.glob("page-*.md"):
            stale_page.unlink()
        try:
            pages_dir.rmdir()
        except OSError:
            pass

    try:
        doc = fitz.open(str(pdf_path))
    except fitz.FileDataError as exc:
        raise Pdf2MdError(f"Cannot open PDF: {exc}") from exc

    if doc.needs_pass:
        password = input("PDF is password-protected. Enter password: ")
        if not doc.authenticate(password):
            raise Pdf2MdError("Incorrect password.")

    page_count = len(doc)
    sections: list[dict[str, Any]] = []
    manifest_tables: list[dict[str, Any]] = []
    manifest_figures: list[dict[str, Any]] = []
    qa_notes: list[str] = []
    if config["ocr_enabled"]:
        qa_notes.append("- OCR was requested, but this lightweight skill build does not bundle an OCR engine.")
    table_state = {"count": 0}
    fig_state = {"count": 0}
    full_blocks = [
        f"# {original_name}",
        "",
        f"*Converted from `{pdf_path.name}`; {page_count} page(s).*",
        "",
    ]
    page_records: list[dict[str, Any]] = []

    for page_num, page in enumerate(doc, start=1):
        lines, rich_lines = _extract_text_lines(page)
        pdf_tables = _find_pdf_tables(page)
        suppress_layout_text = _should_suppress_layout_text(page, pdf_tables)
        render_page_figure = _should_render_page_as_figure(page, lines, pdf_tables)
        layout_text = "" if suppress_layout_text else _extract_layout_text(
            page, [table.bbox for table in pdf_tables if getattr(table, "bbox", None)]
        )
        page_blocks = [f"<!-- source-page: {page_num} -->", "", f"## Page {page_num}", ""]
        layout_mode = str(config["markdown_layout"])
        if suppress_layout_text and layout_mode in ("fixed", "semantic"):
            page_text = "*[Large structured table content is extracted below; raw table text omitted to avoid column-wise reading order.]*"
        elif render_page_figure and layout_mode == "semantic":
            page_text = _render_page_figure(
                page,
                page_num,
                assets_dir,
                doc_root,
                fig_state,
                manifest_figures,
                qa_notes,
                int(config["image_dpi"]),
            )
        elif layout_mode == "fixed":
            page_text = _fenced_text_block(layout_text)
        else:
            flow_text = _format_text_lines(rich_lines, page_num, sections)
            page_text = flow_text
        page_blocks.append(page_text)

        if not rich_lines:
            qa_notes.append(f"- Page {page_num}: no extractable text layer; OCR was not applied.")
        elif SUSPICIOUS_SYMBOL_RE.search(" ".join(lines)):
            qa_notes.append(f"- Page {page_num}: suspicious symbol substitution detected; verify primes, ƒ, and Ω.")

        page_blocks.extend(
            _extract_tables_for_page(
                page,
                page_num,
                lines,
                pdf_tables,
                tables_dir,
                doc_root,
                table_state,
                manifest_tables,
                qa_notes,
                str(config["table_output"]),
                f"pages/page-{page_num:03d}.md" if config["page_output"] == "md" else "full.md",
            )
        )
        page_blocks.extend(
            _extract_images_for_page(
                doc,
                page,
                page_num,
                lines,
                assets_dir,
                doc_root,
                fig_state,
                manifest_figures,
                qa_notes,
            )
        )

        page_file = pages_dir / f"page-{page_num:03d}.md"
        page_content = "\n".join(page_blocks).rstrip() + "\n"
        if config["page_output"] == "md":
            page_file.write_text(page_content, encoding="utf-8")
            page_records.append({"page": page_num, "file": _rel(page_file, doc_root)})
        else:
            page_records.append({"page": page_num, "file": "full.md", "anchor": f"source-page-{page_num}"})
        full_blocks.append(page_content)

    doc.close()

    for index, section in enumerate(sections):
        next_section = sections[index + 1] if index + 1 < len(sections) else None
        section["page_end"] = (next_section["page_start"] - 1) if next_section else page_count
        if section["page_end"] < section["page_start"]:
            section["page_end"] = section["page_start"]

    manifest = {
        "doc_id": doc_id,
        "title": original_name,
        "source_file": pdf_path.name,
        "source_type": "pdf",
        "page_count": page_count,
        "entry": "full.md",
        "page_dir": "pages/" if config["page_output"] == "md" else "",
        "assets_dir": "assets/",
        "tables_dir": "tables/",
        "sections": sections,
        "pages": page_records,
        "tables": manifest_tables,
        "figures": manifest_figures,
        "created_at": _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat(),
        "generator": {"name": "pdf2md-skill", "version": VERSION},
    }

    (doc_root / "full.md").write_text("\n".join(full_blocks).rstrip() + "\n", encoding="utf-8")
    (doc_root / "index.md").write_text(_build_index(doc_id, manifest), encoding="utf-8")
    _write_qa_notes(doc_root / "QA-notes.md", qa_notes, bool(config["ocr_enabled"]))
    _write_json(doc_root / "manifest.json", manifest)
    checksums = _generate_checksums(doc_root)

    return {
        "output_dir": str(doc_root),
        "manifest": manifest,
        "checksums": checksums,
        "qa_notes": qa_notes,
    }


def verify_package(doc_dir: str) -> list[str]:
    root = Path(doc_dir).resolve()
    checksums_path = root / "checksums.json"
    if not checksums_path.exists():
        raise Pdf2MdError(f"Missing checksums.json: {checksums_path}")

    expected = json.loads(checksums_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for rel_path, expected_hash in expected.items():
        if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
            errors.append(f"{rel_path}: checksum path is not a safe relative path")
            continue
        path = root / rel_path
        if not path.exists():
            errors.append(f"{rel_path}: missing")
            continue
        actual = _sha256(path)
        if actual != expected_hash:
            errors.append(f"{rel_path}: expected {expected_hash}, got {actual}")
    return errors


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _is_trusted_base(base_url: str, trusted_base_urls: Iterable[str]) -> bool:
    normalized = base_url.rstrip("/") + "/"
    return any(normalized.startswith(trusted.rstrip("/") + "/") for trusted in trusted_base_urls)


def fetch_trusted(base_url: str, target: str, config_path: str | None = None) -> Path:
    config = _load_config(config_path)
    if not _is_trusted_base(base_url, config["trusted_base_urls"]):
        raise Pdf2MdError(f"Base URL is not trusted: {base_url}")

    parsed_target = urllib.parse.urlparse(target)
    if parsed_target.scheme or parsed_target.netloc:
        raise Pdf2MdError("Target must be a relative path under base-url.")
    target_path = Path(urllib.parse.unquote(parsed_target.path))
    if target_path.is_absolute() or ".." in target_path.parts:
        raise Pdf2MdError("Target must not be absolute or contain '..'.")
    if target_path.suffix.lower() not in config["allowed_extensions"]:
        raise Pdf2MdError(f"Extension is not allowed: {target_path.suffix}")

    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", target_path.as_posix())
    opener = urllib.request.build_opener(_NoRedirect)
    max_bytes = int(config["max_file_size_mb"]) * 1024 * 1024
    try:
        with opener.open(url, timeout=20) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise Pdf2MdError(f"Fetch failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise Pdf2MdError(f"Fetch failed: {exc.reason}") from exc

    if len(data) > max_bytes:
        raise Pdf2MdError(f"Fetched file exceeds max_file_size_mb={config['max_file_size_mb']}.")

    base_parts = urllib.parse.urlparse(base_url)
    cache_root = Path(config["cache_dir"]).resolve() / _safe_name(base_parts.netloc or "local")
    dest = cache_root / target_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def search_manifest(manifest_path: str, query: str) -> list[dict[str, Any]]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    needle = query.casefold()
    results: list[dict[str, Any]] = []
    for key in ("sections", "tables", "figures", "pages"):
        for item in manifest.get(key, []):
            haystack = json.dumps(item, ensure_ascii=False).casefold()
            if needle in haystack:
                result = dict(item)
                result["_type"] = key[:-1] if key.endswith("s") else key
                results.append(result)
    return results


def serve(root: str, host: str, port: int) -> None:
    directory = Path(root).resolve()
    if not directory.exists():
        raise Pdf2MdError(f"Serve root does not exist: {directory}")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving {directory} at http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdf2md", description="Convert PDFs into structured Markdown packages.")
    sub = parser.add_subparsers(dest="command")

    convert_p = sub.add_parser("convert", help="Convert a PDF into a structured document package.")
    convert_p.add_argument("input_pdf")
    convert_p.add_argument("--out", default=None)
    convert_p.add_argument("--doc-id", default=None)
    convert_p.add_argument("--config", default=None)
    convert_p.add_argument("--image-dpi", type=int, default=None)
    convert_p.add_argument("--ocr-enabled", action="store_true")
    convert_p.add_argument("--table-output", choices=["auto", "markdown", "html"], default=None)
    convert_p.add_argument("--markdown-layout", choices=["semantic", "fixed", "flow"], default=None)
    convert_p.add_argument("--page-output", choices=["md", "none"], default=None)
    convert_p.add_argument("--no-pages", action="store_true", help="Do not write pages/page-XXX.md files.")

    verify_p = sub.add_parser("verify", help="Verify checksums for a generated package.")
    verify_p.add_argument("doc_dir")

    serve_p = sub.add_parser("serve", help="Serve generated packages over local HTTP.")
    serve_p.add_argument("root")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)

    fetch_p = sub.add_parser("fetch", help="Fetch a trusted remote package file into cache.")
    fetch_p.add_argument("--base-url", required=True)
    fetch_p.add_argument("--target", required=True)
    fetch_p.add_argument("--config", default=None)

    search_p = sub.add_parser("search-manifest", help="Search sections, tables, figures, and pages in a manifest.")
    search_p.add_argument("manifest")
    search_p.add_argument("--query", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].lower().endswith(".pdf"):
        argv = ["convert", *argv]

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        if args.command == "convert":
            result = convert_pdf(
                args.input_pdf,
                out=args.out,
                doc_id=args.doc_id,
                config_path=args.config,
                image_dpi=args.image_dpi,
                ocr_enabled=args.ocr_enabled,
                table_output=args.table_output,
                markdown_layout=args.markdown_layout,
                page_output="none" if args.no_pages else args.page_output,
            )
            manifest = result["manifest"]
            print("Done.")
            print(f"  Output folder : {result['output_dir']}")
            print(f"  Manifest      : manifest.json")
            print(f"  Pages         : {manifest['page_count']}")
            print(f"  Tables        : {len(manifest['tables'])}")
            print(f"  Figures       : {len(manifest['figures'])}")
            print(f"  QA notes      : {len(result['qa_notes'])}")
            return 0
        if args.command == "verify":
            errors = verify_package(args.doc_dir)
            if errors:
                print("Checksum verification failed:", file=sys.stderr)
                for error in errors:
                    print(f"  - {error}", file=sys.stderr)
                return 1
            print("Checksum verification passed.")
            return 0
        if args.command == "serve":
            serve(args.root, args.host, args.port)
            return 0
        if args.command == "fetch":
            dest = fetch_trusted(args.base_url, args.target, args.config)
            print(f"Fetched: {dest}")
            return 0
        if args.command == "search-manifest":
            print(json.dumps(search_manifest(args.manifest, args.query), ensure_ascii=False, indent=2))
            return 0
    except Pdf2MdError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
