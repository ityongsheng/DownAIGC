"""
doc_analyzer.py — 文档结构识别器

从原始段落列表中识别出各段落的语义类型（标题、正文、参考文献、声明等），
供后续重写引擎和文档构建器使用。
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# ────────────────────── 关键词表 ──────────────────────

START_TRIGGERS = ["摘要", "abstract"]
END_TRIGGERS = ["参考文献", "致谢", "references", "acknowledgment", "acknowledgement"]

HEADING_KEYWORDS = {
    "摘要", "ABSTRACT", "目录", "参考文献", "致谢", "结论", "绪论",
}

DECLARATION_KEYWORDS = {
    "毕业设计原创性声明",
    "毕业设计版权使用授权书",
    "作者签名",
    "指导教师签名",
}

TOC_PATTERNS = [
    r"^第\d+章",
    r"^\d+(\.\d+){0,3}\s+.+\d+$",
]

FIGURE_TABLE_PATTERNS = [
    r"^(图|表)\s*\d",
    r"^(Figure|Table)\s*\d",
    r"^(图注|表注)",
]

REFERENCE_ITEM_PATTERNS = [
    r"^\[\d+\]",
    r"^\d+\.\s",
]


# ────────────────────── 数据类 ──────────────────────

@dataclass
class StructuredParagraph:
    """从文件解析层获得的结构化段落"""
    text: str
    style_name: str = ""
    heading_level: Optional[int] = None  # 1/2/3/None
    is_list_item: bool = False


@dataclass
class AnalyzedParagraph:
    """经过结构识别后的段落，附带类型标签"""
    source: StructuredParagraph
    para_type: str = "body"  # heading | body | reference | acknowledgment | declaration | toc | caption | reference_item | blank


@dataclass
class AnalyzedDocument:
    """完整的结构化文档"""
    paragraphs: list[AnalyzedParagraph] = field(default_factory=list)
    body_start_index: int = 0
    body_end_index: int = -1  # -1 表示到末尾


# ────────────────────── 工具函数 ──────────────────────

def is_trigger_match(text: str, triggers: list[str]) -> bool:
    """检查文本是否匹配触发词（宽松匹配）"""
    normalized = text.replace(" ", "").lower()
    for t in triggers:
        if t in normalized:
            if len(normalized) < 50 or normalized.startswith(t):
                return True
    return False


def is_heading_like_text(text: str) -> bool:
    """基于纯文本内容判断是否像标题"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped in HEADING_KEYWORDS:
        return True
    if len(stripped) <= 30 and not re.search(r"[。！？.!?;；,，:：]", stripped):
        if re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节部分篇]", stripped):
            return True
        if re.match(r"^\d+(\.\d+){0,3}\s*[^0-9]+$", stripped):
            return True
        if re.match(r"^[A-Za-z\s]+$", stripped):
            return True
    return False


def classify_paragraph_type(text: str, style_name: str = "") -> str:
    """判断段落类型"""
    stripped = (text or "").strip()
    style_lower = (style_name or "").lower()

    if not stripped:
        return "blank"

    # 声明类
    if any(keyword in stripped for keyword in DECLARATION_KEYWORDS):
        return "declaration"

    # 目录
    if "toc" in style_lower or stripped.replace(" ", "") == "目录":
        return "toc"

    # 结尾区块标记
    if is_trigger_match(stripped, END_TRIGGERS):
        return "ending_marker"

    # 标题（样式 or 文本特征）
    if (is_heading_like_text(stripped)
        or "heading" in style_lower
        or "标题" in style_lower
            or style_lower.startswith("title")):
        return "heading"

    # 目录项
    if any(re.match(pattern, stripped, re.IGNORECASE) for pattern in TOC_PATTERNS):
        return "toc"

    # 图题/表题
    if any(re.match(pattern, stripped, re.IGNORECASE) for pattern in FIGURE_TABLE_PATTERNS):
        return "caption"

    # 参考文献条目
    if any(re.match(pattern, stripped) for pattern in REFERENCE_ITEM_PATTERNS):
        return "reference_item"

    return "body"


def infer_heading_level(style_name: str, text: str) -> Optional[int]:
    """从样式名推断标题层级"""
    style_lower = (style_name or "").lower()

    # python-docx 样式名如 "Heading 1", "Heading 2"
    match = re.search(r"heading\s*(\d)", style_lower)
    if match:
        level = int(match.group(1))
        return min(level, 3)  # 最多保留到三级

    # 中文样式名
    for cn_level, cn_name in [(1, "一级标题"), (2, "二级标题"), (3, "三级标题")]:
        if cn_name in style_lower or f"标题 {cn_level}" in style_lower:
            return cn_level

    # 从文本内容推断
    stripped = (text or "").strip()
    if re.match(r"^第[一二三四五六七八九十百千万0-9]+章", stripped):
        return 1
    if re.match(r"^\d+\.\d+\.\d+", stripped):
        return 3
    if re.match(r"^\d+\.\d+\s", stripped):
        return 2
    if re.match(r"^\d+\s+[^\d]", stripped):
        return 1

    return None


# ────────────────────── 核心分析函数 ──────────────────────

def analyze_document(paragraphs: list[StructuredParagraph]) -> AnalyzedDocument:
    """
    对结构化段落列表进行语义分析，标注每段类型，
    并确定正文的起止范围。
    """
    analyzed = AnalyzedDocument()

    # 第一轮：逐段分类
    for para in paragraphs:
        para_type = classify_paragraph_type(para.text, para.style_name)

        # 如果 heading_level 已由 file_parser 提取到，优先信任
        if para.heading_level is not None:
            para_type = "heading"

        analyzed.paragraphs.append(AnalyzedParagraph(
            source=para,
            para_type=para_type,
        ))

    # 第二轮：确定正文起止范围
    # 起始：找到 "摘要" / "abstract" 之后开始
    has_start_trigger = False
    for i, ap in enumerate(analyzed.paragraphs):
        if is_trigger_match(ap.source.text, START_TRIGGERS):
            has_start_trigger = True
            analyzed.body_start_index = i + 1
            break

    if not has_start_trigger:
        analyzed.body_start_index = 0

    # 结束：找到 "参考文献" / "致谢" 等
    for i, ap in enumerate(analyzed.paragraphs):
        if ap.para_type == "ending_marker":
            analyzed.body_end_index = i
            break

    if analyzed.body_end_index == -1:
        analyzed.body_end_index = len(analyzed.paragraphs)

    return analyzed


def get_body_paragraphs(analyzed_doc: AnalyzedDocument) -> list[tuple[int, AnalyzedParagraph]]:
    """获取需要重写的正文段落列表（带索引）"""
    result = []
    start = analyzed_doc.body_start_index
    end = analyzed_doc.body_end_index

    for i in range(start, end):
        ap = analyzed_doc.paragraphs[i]
        if ap.para_type == "body":
            result.append((i, ap))
    return result


# ────────────────────── 章节级分组与批量切片 ──────────────────────

@dataclass
class SectionChunk:
    """一个可以被单次 LLM 调用处理的段落批次"""
    paragraph_indices: list[int] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    total_chars: int = 0


def group_into_section_chunks(
    analyzed_doc: AnalyzedDocument,
    max_chars_per_chunk: int = 2000,
) -> list[SectionChunk]:
    """
    将正文段落按章节分组，再按字数切片。

    策略：
    1. 遇到标题 → 当前 chunk 封存，开启新 chunk
    2. 在同一个章节内，连续 body 段落累加到 chunk 中
    3. 当 chunk 字数超过 max_chars_per_chunk → 封存，开启新 chunk
    4. 非 body 段落（图题等）也作为切割点

    返回的 SectionChunk 列表可以被并行处理。
    """
    chunks: list[SectionChunk] = []
    current_chunk = SectionChunk()

    start = analyzed_doc.body_start_index
    end = analyzed_doc.body_end_index

    for i in range(start, end):
        ap = analyzed_doc.paragraphs[i]
        text = ap.source.text.strip()

        # 非 body 段落 → 封存当前 chunk
        if ap.para_type != "body":
            if current_chunk.texts:
                chunks.append(current_chunk)
                current_chunk = SectionChunk()
            continue

        # body 段落：检查加入后是否超限
        text_len = len(text)
        if current_chunk.total_chars + text_len > max_chars_per_chunk and current_chunk.texts:
            # 当前 chunk 已满，封存
            chunks.append(current_chunk)
            current_chunk = SectionChunk()

        current_chunk.paragraph_indices.append(i)
        current_chunk.texts.append(text)
        current_chunk.total_chars += text_len

    # 收尾
    if current_chunk.texts:
        chunks.append(current_chunk)

    return chunks
