# Edge Cases — pdf2md

## 加密 PDF

脚本在 `doc.needs_pass` 时交互式提示输入密码。

若运行环境非交互式（如 CI），建议先用 `qpdf` 解密：
```
qpdf --decrypt --password=<密码> input.pdf decrypted.pdf
```
再对 `decrypted.pdf` 运行脚本。

---

## 扫描件（无文本层）

纯图片扫描的 PDF 无可提取文本，脚本每页输出：
```
*[No extractable text — may be a scanned image.]*
```

升级方案（超出本 Skill 范围）：
1. 安装 `pytesseract` + `pdf2image`
2. 将每页渲染为图片后 OCR 识别
3. 或使用 Adobe Acrobat 的「识别文字」功能预处理

---

## 超大 PDF（>100 页）

脚本顺序处理所有页面，页数越多耗时越长。

如需仅转换部分页，可修改脚本在 `enumerate(doc)` 前加切片：
```python
pages = list(doc)[0:50]  # 只处理第 1-50 页
for page_num, page in enumerate(pages, start=1):
```

---

## 无嵌入图片的 PDF

`images/` 文件夹会被创建但保持为空，`img_counter` 输出为 0，属于正常行为，不报错。

---

## 中文 / CJK 内容

pymupdf 原生支持 Unicode，`write_text(encoding="utf-8")` 确保输出文件编码正确。

Windows 终端可能显示乱码，但 `.md` 文件本身内容无误，用 VS Code 打开可正确显示。

---

## 损坏或截断的 PDF

`fitz.FileDataError` 在 `fitz.open()` 时捕获并退出。

修复建议：
```
mutool clean -ggggz input.pdf repaired.pdf
```
再对 `repaired.pdf` 运行脚本。

---

## CMYK 色彩模式的图片（印刷 PDF 常见）

脚本检测 `pix.n > 4`（CMYK 通道数为 4，加 alpha 则 > 4），自动转换为 RGB 再保存为 PNG，无需手动干预。

---

## 同一图片在多页重复出现

同一 xref 的图片每次遇到都会被提取并保存为新文件（如 `img_001.png`、`img_002.png`），文件名不同但内容相同。这是有意设计——保持位置信息完整，牺牲少量磁盘空间。
