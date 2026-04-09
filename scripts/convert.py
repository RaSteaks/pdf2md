"""
pdf2md/scripts/convert.py
将 PDF 转换为 Markdown，图片保存为 PNG 文件。

用法：
    python convert.py <path/to/file.pdf>

输出（与 PDF 同级目录）：
    <pdf名>/
        <pdf名>.md
        images/
            img_001.png
            ...

依赖：pymupdf  (pip install pymupdf)
"""

import sys
import fitz  # pymupdf
from pathlib import Path


def convert(pdf_path_str: str) -> None:
    pdf_path = Path(pdf_path_str).resolve()

    if not pdf_path.exists():
        print(f"ERROR: File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    base_name = pdf_path.stem
    out_dir   = pdf_path.parent / base_name
    img_dir   = out_dir / "images"
    md_path   = out_dir / f"{base_name}.md"

    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(str(pdf_path))
    except fitz.FileDataError as exc:
        print(f"ERROR: Cannot open PDF — {exc}", file=sys.stderr)
        sys.exit(1)

    if doc.needs_pass:
        password = input("PDF is password-protected. Enter password: ")
        if not doc.authenticate(password):
            print("ERROR: Incorrect password.", file=sys.stderr)
            sys.exit(1)

    total_pages = len(doc)
    img_counter = 0
    md_lines    = [
        f"# {base_name}\n",
        f"*Converted from `{pdf_path.name}` — {total_pages} page(s)*\n",
    ]

    for page_num, page in enumerate(doc, start=1):
        md_lines.append(f"\n---\n\n## Page {page_num}\n")

        # 文本提取（sort=True 按阅读顺序排列块）
        blocks = page.get_text("blocks", sort=True)
        page_texts = [b[4].strip() for b in blocks if b[6] == 0 and b[4].strip()]
        if page_texts:
            md_lines.append("\n\n".join(page_texts))
        else:
            md_lines.append("*[No extractable text — may be a scanned image.]*")

        # 图片提取
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                img_bytes  = base_image["image"]
                img_ext    = base_image["ext"]
            except Exception as exc:
                print(f"  WARNING: Cannot extract image xref={xref}: {exc}")
                continue

            img_counter += 1
            img_filename = f"img_{img_counter:03d}.png"
            img_filepath = img_dir / img_filename

            if img_ext == "png":
                img_filepath.write_bytes(img_bytes)
            else:
                # 非 PNG 格式通过 Pixmap 转换，同时处理 CMYK (n>4) → RGB
                pix = fitz.Pixmap(img_bytes)
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pix.save(str(img_filepath))

            # Markdown 中使用正斜杠（在 Windows 渲染器中也有效）
            md_lines.append(f"\n\n![image](images/{img_filename})")

    doc.close()

    # 显式指定 UTF-8，避免 Windows 默认 cp1252 乱码
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print("Done.")
    print(f"  Output folder : {out_dir}")
    print(f"  Markdown file : {md_path}")
    print(f"  Pages         : {total_pages}")
    print(f"  Images saved  : {img_counter}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convert.py <path/to/file.pdf>", file=sys.stderr)
        sys.exit(1)
    convert(sys.argv[1])
