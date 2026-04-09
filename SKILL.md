---
name: pdf2md
description: 将 PDF 文件转换为 Markdown 文档，图片保存为独立 PNG 文件并在 Markdown 中内联引用，输出到以 PDF 命名的专属文件夹。当用户要求"PDF 转 Markdown"、"提取 PDF 内容"、"convert PDF to md"、"PDF 转 md"时触发。
user-invocable: true
---

# PDF to Markdown Converter

将 PDF 转换为结构化 Markdown，嵌入图片保存为 PNG 文件并在文档中引用，输出到独立文件夹。

## 输出结构

给定 `report.pdf`，生成：

```
report/
├── report.md
└── images/
    ├── img_001.png
    └── ...
```

## 转换流程

### Step 1：确认 PDF 路径

若用户未指定，询问 PDF 文件路径。接受绝对或相对路径，脚本内部统一用 `pathlib.Path` 处理。

### Step 2：检查依赖

```
python -c "import fitz; print(fitz.__version__)"
```

如缺少 `fitz`，安装：

```
pip install pymupdf
```

Windows 上优先尝试 `python`，其次 `py`，最后 `python3`。

### Step 3：运行转换脚本

脚本随 Skill 打包于 `{baseDir}/scripts/convert.py`，直接运行：

```
python "{baseDir}/scripts/convert.py" "path\to\file.pdf"
```

- 输出文件夹与 PDF **同级目录**（不是当前工作目录）
- Windows 路径含空格时用双引号包裹

### Step 4：汇报结果

转换完成后告知用户：
- 输出文件夹路径
- 处理页数
- 提取图片数量
- 任何警告（如加密页、无文本页）

并展示生成的 `.md` 文件前 30 行供用户验证质量。

## Windows 注意事项

- 使用 `python` 或 `py`，而非 `python3`
- 脚本使用 `pathlib.Path`，无需担心 `\` vs `/` 问题
- 脚本以 `encoding="utf-8"` 写文件，避免 Windows cp1252 乱码
- PowerShell 中路径含空格须用双引号包裹
- **PowerShell 不支持 `&&`**，多条命令请用 `;` 连接，例如：`python convert.py "file.pdf" ; echo done`
- 若控制台出现 `©` 等特殊字符乱码崩溃（GBK），脚本已在 Windows 下自动将 stdout/stderr 切换为 UTF-8，通常无需额外处理
- MuPDF 输出的 `cmsOpenProfileFromMem failed` 为底层 ICC 配置文件警告，可忽略，不影响转换结果

## 边缘情况

详见 `{baseDir}/references/edge-cases.md`，涵盖加密 PDF、扫描件、超大文件、无图片 PDF、中文内容等场景。
