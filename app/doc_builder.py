"""
doc_builder.py — 新文档构建器

根据 AnalyzedDocument + 重写结果，生成全新的 docx 文件。
不再做原位回填，而是从零构建，只保留标题层级和结尾区块。
"""

import os
import docx
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from app.doc_analyzer import AnalyzedDocument


OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs"
)
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _setup_styles(doc: docx.Document) -> None:
    """设置基本的学术论文样式（可选增强）"""
    style = doc.styles["Normal"]
    font = style.font
    font.name = "宋体"
    font.size = Pt(12)
    font.color.rgb = RGBColor(0, 0, 0)
    paragraph_format = style.paragraph_format
    paragraph_format.space_after = Pt(6)
    paragraph_format.line_spacing = 1.5


def build_document(
    analyzed_doc: AnalyzedDocument,
    rewrite_results: dict[int, str],
    output_path: str,
) -> str:
    """
    构建新的 docx 文件。

    Args:
        analyzed_doc: 经过分析的文档结构
        rewrite_results: {段落索引: 重写后文本} 的映射
        output_path: 输出文件路径

    Returns:
        保存的文件路径
    """
    doc = docx.Document()
    _setup_styles(doc)

    for i, ap in enumerate(analyzed_doc.paragraphs):
        text = ap.source.text.strip()
        para_type = ap.para_type

        if not text and para_type == "blank":
            # 保留空行以维持文档结构
            doc.add_paragraph("")
            continue

        if para_type == "heading":
            # 使用标题样式
            level = ap.source.heading_level
            if level is None:
                level = 1
            level = max(1, min(level, 3))
            doc.add_heading(text, level=level)

        elif para_type == "body":
            # 正文段：使用重写后的文本（如有），否则用原文
            rewritten = rewrite_results.get(i, text)
            p = doc.add_paragraph(rewritten)
            p.paragraph_format.first_line_indent = Pt(24)  # 首行缩进

        elif para_type in ("ending_marker", "reference_item", "acknowledgment", "declaration"):
            # 结尾区块：原样保留
            doc.add_paragraph(text)

        elif para_type == "caption":
            # 图题/表题：原样保留
            p = doc.add_paragraph(text)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER

        elif para_type == "toc":
            # 目录项：原样保留
            doc.add_paragraph(text)

        else:
            # 其他未分类段落：原样保留
            doc.add_paragraph(text)

    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    doc.save(output_path)
    return output_path
