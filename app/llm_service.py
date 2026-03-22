"""
llm_service.py — 编排层（重构后）

作为 API 层和底层模块之间的粘合剂：
- 调用 file_parser 解析文件
- 调用 doc_analyzer 分析文档结构
- 调用 llm_rewriter 逐段重写
- 调用 doc_builder 构建新文档
- 保留 generate_paper_stream（论文生成功能）
- 保留 API key 工具函数
"""

import re
import os
import json
import asyncio
import traceback
from typing import AsyncGenerator

from openai import AsyncOpenAI, AuthenticationError

from app.doc_analyzer import (
    StructuredParagraph,
    AnalyzedDocument,
    analyze_document,
)
from app.file_parser import parse_docx_structured, parse_text_to_structured
from app.llm_rewriter import (
    rewrite_body_paragraphs,
    format_error_message,
    SYSTEM_INSTRUCTION,
)
from app.doc_builder import build_document

# Ensure directories exist
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

INPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")
os.makedirs(INPUT_DIR, exist_ok=True)


# ────────────────────── API Key 工具 ──────────────────────

def normalize_api_key(raw_key: str) -> str:
    """Normalize API key copied from UI text, handling common full-width punctuation issues."""
    key = (raw_key or '').strip()
    if not key:
        return ''

    # Normalize common full-width punctuation and whitespace from copy/paste.
    key = key.replace('：', ':').replace('，', ',').replace('；', ';').replace('　', ' ')

    # Support pasted forms like "Bearer xxx" or "api_key: xxx".
    lower_key = key.lower()
    if lower_key.startswith('bearer '):
        key = key[7:].strip()
    if ':' in key:
        left, right = key.split(':', 1)
        if any(tag in left.lower() for tag in ['api_key', 'apikey', 'token', 'key']):
            key = right.strip()

    return key


def validate_api_key_ascii(key: str) -> None:
    """Raise a user-friendly error if API key cannot be used as an HTTP header value."""
    try:
        key.encode('ascii')
    except UnicodeEncodeError as exc:
        raise ValueError('API Key 含有非 ASCII 字符（常见是中文冒号"："或中文空格）。请粘贴纯英文 Key。') from exc


# ────────────────────── 核心编排：内容优先模式 ──────────────────────

async def process_paper_stream(
    text: str,
    order_id: str,
    reference_list: str = "",
    api_key: str = "",
) -> AsyncGenerator[str, None]:
    """
    内容优先模式的主编排函数。

    流程：
    1. 解析文件 → 结构化段落列表
    2. 分析文档结构 → 标注段落类型
    3. 逐段重写正文（流式输出）
    4. 构建全新 docx
    """
    # ── API Key 校验 ──
    api_key_to_use = normalize_api_key(api_key if api_key else os.environ.get("OPENAI_API_KEY", ""))
    if not api_key_to_use:
        yield "\n\n[ERROR] API Key not configured. Please fill in the frontend or set OPENAI_API_KEY env var.\n"
        return
    try:
        validate_api_key_ascii(api_key_to_use)
    except ValueError as e:
        yield f"\n\n[ERROR] {format_error_message(e)}\n"
        return

    client = AsyncOpenAI(
        api_key=api_key_to_use,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1"),
    )
    model_name = os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V3")

    try:
        # ── Step 1: 解析文件为结构化段落 ──
        input_filepath = os.path.join(INPUT_DIR, f"input_{order_id}.docx")
        is_docx = os.path.exists(input_filepath)

        if is_docx:
            with open(input_filepath, "rb") as f:
                file_bytes = f.read()
            paragraphs = parse_docx_structured(file_bytes)
            yield "[System] 已解析 docx 文件结构...\n\n"
        else:
            paragraphs = parse_text_to_structured(text)
            yield "[System] 已解析纯文本结构...\n\n"

        # ── Step 2: 分析文档结构 ──
        analyzed_doc = analyze_document(paragraphs)
        body_count = sum(
            1 for ap in analyzed_doc.paragraphs[analyzed_doc.body_start_index:analyzed_doc.body_end_index]
            if ap.para_type == "body"
        )
        yield f"[System] 文档分析完成：共 {len(paragraphs)} 段，其中 {body_count} 段正文待重写...\n\n"

        if body_count == 0:
            yield "\n[NO_BODY_REWRITTEN] 未命中可改写的正文段落，请检查文档结构或正文起始位置。\n"
            return

        # ── Step 3: 流式重写正文 ──
        rewrite_results: dict[int, str] = {}
        rewritten_count = 0
        fallback_count = 0

        async for para_idx, original_text, rewritten_text in rewrite_body_paragraphs(
            analyzed_doc, client, model_name, reference_list
        ):
            rewrite_results[para_idx] = rewritten_text

            if rewritten_text.strip() != original_text.strip():
                rewritten_count += 1
            else:
                fallback_count += 1

            # 流式输出重写后的文本给前端
            yield rewritten_text
            yield "\n\n"

        # ── Step 4: 构建新 docx ──
        output_filepath = os.path.join(OUTPUT_DIR, f"output_{order_id}.docx")
        build_document(analyzed_doc, rewrite_results, output_filepath)
        print(f"[{order_id}] New document built and saved to: {output_filepath}")

        # ── 输出统计 ──
        if rewritten_count == 0:
            yield "\n[NO_BODY_REWRITTEN] 未命中可改写的正文段落，请检查文档结构或正文起始位置。\n"
        else:
            yield f"\n[STATS] rewritten={rewritten_count};fallback={fallback_count}\n"

    except ValueError as e:
        yield f"\n\n[ERROR] {format_error_message(e)}\n"
    except Exception as e:
        err_msg = format_error_message(e)
        print(f"[{order_id}] process_paper_stream error: {err_msg}")
        print(traceback.format_exc())
        yield f"\n\n[Connection Error]: {err_msg}\n"


# ────────────────────── 论文生成功能（保留） ──────────────────────

async def generate_paper_stream(
    outline: list[str], references: list[str]
) -> AsyncGenerator[str, None]:
    """
    通过 Prompt Chaining 模式，分块生成 AIGC 检测率极低的学术论文。
    返回 Server-Sent Events (SSE) 格式的数据流。
    """
    api_key_to_use = os.environ.get("OPENAI_API_KEY")
    if not api_key_to_use:
        yield f"data: {json.dumps({'status': 'error', 'error': 'OPENAI_API_KEY not configured'}, ensure_ascii=False)}\n\n"
        return

    client = AsyncOpenAI(
        api_key=api_key_to_use,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1"),
    )
    model_name = os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V3")

    references_text = "\n".join(references)

    for section in outline:
        # Step 1: Draft Generation
        yield f"data: {json.dumps({'status': 'generating_draft', 'section': section}, ensure_ascii=False)}\n\n"

        draft_prompt_text = f"""你是一名大四本科生，正在撰写毕业论文。请根据提供的章节标题撰写学术正文。你必须且只能从我提供的参考文献中提取信息来支撑论点。当引用某篇文献时，必须在句子末尾准确标注上标，例如 [1]。绝对不允许捏造引用。确保逻辑严密、数据准确，避免无意义扩写与口语化表达。

当前章节标题：{section}

参考文献列表：
{references_text}
"""
        try:
            draft_response = await client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": draft_prompt_text}],
                stream=False,
            )
            draft_text = draft_response.choices[0].message.content
        except Exception as e:
            err_msg = format_error_message(e)
            yield f"data: {json.dumps({'status': 'error', 'section': section, 'error': f'Draft generation failed: {err_msg}'}, ensure_ascii=False)}\n\n"
            continue

        # Step 2: Humanization
        yield f"data: {json.dumps({'status': 'humanizing', 'section': section}, ensure_ascii=False)}\n\n"

        humanization_prompt_text = f"""请对以下包含参考文献标注的论文段落执行"学术语域重构与信息密度提升"。
在重写时，请严格遵守以下规则：
1. 保留原有事实、数据、术语和结论，不得篡改。
2. 绝对保留原有参考文献标号 [X]，并确保其仍对应原来的论点，不得错位。
3. 语言保持正式、克制、书面化，不要写成口语说明文。
4. 句式允许长短交错，但不要故意写得晦涩，也不要出现机械整齐的套话结构。
5. 避免套话、空话和机械连接词堆积，不要扩写，整体长度与原文接近。
6. 只输出正文，不要输出解释、确认、总结或提示语。

待重写段落：
{draft_text}
"""
        try:
            human_response = await client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": humanization_prompt_text}],
                stream=False,
            )
            final_text = human_response.choices[0].message.content
        except Exception as e:
            err_msg = format_error_message(e)
            yield f"data: {json.dumps({'status': 'error', 'section': section, 'error': f'Humanization failed: {err_msg}'}, ensure_ascii=False)}\n\n"
            continue

        yield f"data: {json.dumps({'status': 'done', 'section': section, 'content': final_text}, ensure_ascii=False)}\n\n"

        # Optional delay to prevent rate limiting
        await asyncio.sleep(1)
