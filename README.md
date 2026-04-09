# pdf2md

一个 [OpenClaw](https://docs.openclaw.ai) Skill，在 Windows 环境下将 PDF 文件转换为 Markdown 文档，并保留 PDF 中的嵌入图片。

## 输出结构

给定 `report.pdf`，转换结果生成于 PDF **同级目录**：

```
report/
├── report.md
└── images/
    ├── img_001.png
    └── img_002.png
```

## 安装

**1. 安装 Python 依赖**

```
pip install -r requirements.txt
```

**2. 部署 Skill（Windows）**

将整个项目文件夹复制到 OpenClaw workspace skills 目录：

```
C:\Users\<用户名>\.openclaw\workspace\skills\pdf2md\
```

## 使用方式

在 OpenClaw 对话中输入 `/pdf2md`，或直接告诉 Claude：

> "帮我把 xxx.pdf 转成 Markdown"

Claude 会自动调用 `scripts/convert.py` 完成转换。

也可以直接运行脚本：

```
python scripts/convert.py "path\to\file.pdf"
```

## 项目结构

```
pdf2md/
├── SKILL.md                  # Skill 定义，指导 Claude 执行转换流程
├── scripts/
│   └── convert.py            # PDF 转换脚本（文本 + 图片提取）
├── references/
│   └── edge-cases.md         # 边缘情况处理指南
├── requirements.txt
└── README.md
```

## 边缘情况

支持以下场景，详见 [`references/edge-cases.md`](references/edge-cases.md)：

- 加密 PDF（交互式输入密码）
- CMYK 色彩图片（自动转 RGB）
- 中文 / CJK 内容（UTF-8 输出）
- 无文本层的扫描件（逐页提示）
- 损坏的 PDF（捕获错误并提示修复方案）

## 依赖

- [pymupdf](https://pymupdf.readthedocs.io) — PDF 解析与图片提取
