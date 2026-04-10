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

import io
import sys
import fitz  # pymupdf
from pathlib import Path

# Windows 控制台默认 GBK 编码，遇到版权符号 © 等特殊字符会崩溃
# 强制将 stdout/stderr 切换为 UTF-8，errors='replace' 保证不中断运行
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# Windows MAX_PATH = 260 字符；预留父目录路径 + "\images\img_999.png" 约 30 字符的余量
# 将文件夹/文件名限制在 60 字符以内，超出时截断并附加 "…" 提示
MAX_NAME_LEN = 60


def _safe_name(name: str) -> str:
    """截断过长的文件名，保留前 MAX_NAME_LEN 个字符并加省略号标记。"""
    if len(name) <= MAX_NAME_LEN:
        return name
    truncated = name[:MAX_NAME_LEN].rstrip()
    print(f"  WARNING: PDF filename is too long ({len(name)} chars), "
          f"output folder truncated to: '{truncated}…'")
    return truncated + "…"


def convert(pdf_path_str: str) -> None:
    pdf_path = Path(pdf_path_str).resolve()

    if not pdf_path.exists():
        print(f"ERROR: File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    original_name = pdf_path.stem
    base_name     = _safe_name(original_name)
    out_dir       = pdf_path.parent / base_name
    img_dir       = out_dir / "images"
    md_path       = out_dir / f"{base_name}.md"

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
        f"# {original_name}\n",          # 标题保留完整原始文件名
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

            # 统一通过 Pixmap 处理，不直接写原始字节
            # PNG 不支持 CMYK/DeviceN，必须先转 sRGB，否则抛 ValueError
            pix = fitz.Pixmap(img_bytes)
            if pix.colorspace and pix.colorspace not in (fitz.csGRAY, fitz.csRGB):
                # CMYK、DeviceN、ICCBased 等工业级色彩空间 → sRGB
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
