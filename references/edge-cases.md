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

## 中文 / CJK 内容及控制台编码崩溃（GBK）

**现象**：含 `©`、`™` 等特殊字符时 Python 控制台抛出 `UnicodeEncodeError: 'gbk' codec can't encode`

**原因**：Windows 控制台默认 GBK 编码，无法处理部分 Unicode 字符。

**修复**：脚本在 Windows 下自动将 `sys.stdout` / `sys.stderr` 切换为 UTF-8（`errors='replace'`），无法编码的字符替换为 `?` 而不是崩溃。`.md` 输出文件始终为完整 UTF-8，不受影响。

---

## PowerShell 不支持 `&&`

**现象**：`git clone ... && python convert.py` 在 PowerShell 中报语法错误

**原因**：`&&` 是 Bash/CMD 语法，PowerShell 不支持。

**修复**：改用 `;` 连接命令，或在 CMD（`cmd.exe`）中运行：
```
python convert.py "file.pdf" ; echo done
```

---

## 损坏或截断的 PDF

`fitz.FileDataError` 在 `fitz.open()` 时捕获并退出。

修复建议：
```
mutool clean -ggggz input.pdf repaired.pdf
```
再对 `repaired.pdf` 运行脚本。

---

## CMYK / DeviceN 色彩空间（工业级 PDF 常见）

**现象**：`ValueError: unsupported colorspace for 'png'`

**原因**：PNG 格式不支持 CMYK 或 DeviceN（多通道）色彩空间。FilmLight、印刷行业等专业 PDF 含此类图片。

**修复**：脚本统一通过 `fitz.Pixmap` 处理图片，检测到色彩空间不是 sRGB/灰度时，强制转换为 sRGB 再保存，无需手动干预。

---

## MuPDF ICC 配置文件警告

**现象**：控制台反复输出 `MuPDF error: format error: cmsOpenProfileFromMem failed`

**原因**：PDF 内嵌了老旧或非标准的 ICC 颜色配置文件，MuPDF 底层渲染引擎读取失败。

**影响**：仅为警告，不影响文本提取和图片保存。可安全忽略。

---

## 同一图片在多页重复出现

同一 xref 的图片每次遇到都会被提取并保存为新文件（如 `img_001.png`、`img_002.png`），文件名不同但内容相同。这是有意设计——保持位置信息完整，牺牲少量磁盘空间。
