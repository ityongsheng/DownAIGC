"""
llm_rewriter.py — 并行强重写引擎

核心改动（相比 v1）：
1. 按章节切片 → 多段落合并为一个 chunk → 单次 LLM 调用
2. 所有 chunk 用 asyncio.gather + Semaphore 并行处理
3. 不再做 3 次重试，最多 1 次补救
"""

import re
import os
import asyncio
import traceback
from typing import AsyncGenerator
from difflib import SequenceMatcher

from openai import AsyncOpenAI, AuthenticationError

from app.doc_analyzer import AnalyzedDocument, SectionChunk, group_into_section_chunks
from app.text_humanizer import humanize_text

# ────────────────────── 全新 System Prompt ──────────────────────

SYSTEM_INSTRUCTION = """你是一位经验丰富的本科毕业论文指导教师，正在帮学生手动润色毕业论文。你的目标是让文本读起来完全像人类学者亲手写的，而不是AI生成的。

【核心写作风格——模拟真实人类】
1. 句子长度极度不均匀：每段必须同时包含极短句（5-10字，如"效果显著。""值得关注。"）和嵌套复杂的长句（50字以上）。绝不允许连续两句长度相近。至少30%的句子应在15字以内。
2. 不完美的学术表达：真实本科生的论文不会每句都完美。偶尔使用略显口语化的学术表达（如"笔者认为""据此推断""某种程度上"），模拟真实写作中的犹豫和推敲痕迹。
3. 禁用所有高频AI连接词："首先、其次、再次、最后、一方面/另一方面、总的来说、综上所述、值得注意的是、众所周知"——全部禁止。用"故而、由此、正因如此、就学理而言、退一步说"等低频表达替代。
4. 句式结构多样化：每段内必须混合使用以下至少3种句式：
   - 主谓宾常规句
   - 被动句（"该方法被广泛应用于…"）
   - 无主语句（"研究引入了…"）
   - 倒装/前置状语句（"在此基础上，…"）
   - 插入语句（"——这一点尤为关键——"）
5. 语义忠实：保留所有关键数据、论点、实验结果、参考文献标号。不得新增观点或数据。
6. 参考文献标号（[1],[2]等）必须精准转移到重写后对应论点的末尾。
7. 零废话输出模式：直接输出重写后的正文。绝不输出任何解释、确认、总结性文字。

【反检测关键策略】
- 每段的信息组织方式必须与原文不同：打散原句顺序，重新组织论证逻辑链。
- 将过长的复合句拆成一长一短两句。将过短的连续简单句偶尔合并。
- 动词优先用具体动词（"引入、构建、部署"）替代泛化动词（"进行、实现、采用"）。
- 适当使用反问句或设问句（每段最多1处）打破平铺直叙。

【边界意识】
标题、目录、文献引用项、致谢、声明类文本，不改写，原样返回。"""

# ────────────────────── 段落分隔符 ──────────────────────

PARA_SEPARATOR = "\n◆◆PARA_BREAK◆◆\n"

# ────────────────────── 质量控制（简化版） ──────────────────────

FORBIDDEN_PHRASES = [
    "综上所述", "值得注意的是", "总的来说", "接下来",
    "本文将", "我认为", "其实", "顺便", "简而言之",
    "众所周知", "在此大环境下", "一方面", "另一方面",
    "好的", "遵从您的指令", "重构如下", "前情提要",
]

MIN_LENGTH_RATIO = 0.65
MAX_LENGTH_RATIO = 1.35
SIMILARITY_FALLBACK_THRESHOLD = 0.995


def text_similarity_ratio(source: str, rewritten: str) -> float:
    return SequenceMatcher(None, source.strip(), rewritten.strip()).ratio()


def is_result_acceptable(source: str, rewritten: str) -> bool:
    """简化的质量检查：只做一次判断"""
    cleaned = (rewritten or "").strip()
    if not cleaned:
        return False
    source_len = max(len(source.strip()), 1)
    ratio = len(cleaned) / source_len
    if ratio < MIN_LENGTH_RATIO or ratio > MAX_LENGTH_RATIO:
        return False
    if any(phrase in cleaned for phrase in FORBIDDEN_PHRASES):
        return False
    if text_similarity_ratio(source, cleaned) > SIMILARITY_FALLBACK_THRESHOLD:
        return False
    return True


def format_error_message(err: Exception) -> str:
    try:
        return str(err)
    except Exception:
        return repr(err)


# ── 保留这个函数给测试用 ──
def split_paragraph_into_rewrite_units(
    text: str, min_chars: int = 180, max_chars: int = 320
) -> list[str]:
    """保留兼容性（被测试引用）"""
    stripped = (text or "").strip()
    if not stripped or len(stripped) <= max_chars:
        return [text]
    pattern = r'([。！？.!?]["\'\\]\)]?\s*)'
    parts = re.split(pattern, text)
    sentences = []
    curr_s = ""
    for p in parts:
        curr_s += p
        if re.match(pattern, p):
            sentences.append(curr_s)
            curr_s = ""
    if curr_s:
        sentences.append(curr_s)
    sentences = [s for s in sentences if s]
    if len(sentences) <= 1:
        return [text]
    units = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
            continue
        if len(current) + len(sentence) <= max_chars:
            current += sentence
            continue
        if len(current) >= min_chars:
            units.append(current)
            current = sentence
            continue
        current += sentence
    if current:
        units.append(current)
    return units if len(units) > 1 else [text]


# ────────────────────── Prompt 构造（批量版） ──────────────────────

def build_chunk_prompt(
    texts: list[str],
    reference_context: str = "",
) -> str:
    """
    构造一个包含多段落的 prompt。
    用 PARA_SEPARATOR 分隔各段，要求 LLM 也用同样的分隔符分隔输出。
    """
    lines = [
        "正在执行段落级学术重构。以下输入包含多个段落，段落之间用 ◆◆PARA_BREAK◆◆ 分隔。",
        "请逐段重写，输出时也必须用 ◆◆PARA_BREAK◆◆ 分隔各段，数量必须与输入一致。",
        "",
        "硬性边界约束：",
        "1. 每段输出长度必须控制在该段输入字数的 0.65 倍至 1.35 倍之间。",
        "2. 不得新增段落、不得合并段落、不得删除段落。输入几段就输出几段。",
        "3. 禁止在段首或段尾添加承上启下的衔接句。",
        "4. 不得补充原文未出现的新事实、新结论。",
        "5. 优先改写句法层次与信息组织方式，打散原句顺序，而不是只替换近义词。",
        "6. 刻意打破句式对称，交替使用长短句，增加从句嵌套深度。",
        "7. 禁止使用排比结构和对称句式。",
    ]
    if reference_context:
        lines.extend(["", f"参考文献信息参考：{reference_context}"])
    lines.extend(["", "待处理正文：", PARA_SEPARATOR.join(texts)])
    return "\n".join(lines)


# ────────────────────── LLM 调用 ──────────────────────

async def _request_rewrite(client: AsyncOpenAI, model: str, prompt: str) -> str:
    """单次 LLM 调用"""
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            temperature=1.2,  # 高 temperature 增加词汇多样性
            stream=False,
        )
    except AuthenticationError:
        raise ValueError(
            "API Key 无效，或与当前接口平台 / 模型不匹配。请检查你填写的是当前平台的有效 Key。"
        )
    except (asyncio.TimeoutError, OSError, ConnectionError):
        # 网络抖动，重试一次
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            temperature=1.2,
            stream=False,
        )
    return (response.choices[0].message.content or "").strip()


# ────────────────────── 核心：处理单个 chunk ──────────────────────

async def _rewrite_chunk(
    client: AsyncOpenAI,
    model: str,
    chunk: SectionChunk,
    reference_context: str = "",
    semaphore: asyncio.Semaphore | None = None,
) -> dict[int, str]:
    """
    对一个 SectionChunk 执行重写。

    返回 {段落索引: 重写后文本} 的映射。
    """
    sem = semaphore or asyncio.Semaphore(999)

    async with sem:
        prompt = build_chunk_prompt(chunk.texts, reference_context)
        raw_result = await _request_rewrite(client, model, prompt)

        # 清理 LLM 输出中可能的前缀/后缀废话
        cleaned_result = raw_result.strip()
        # 移除可能的 markdown 代码块包裹
        if cleaned_result.startswith("```"):
            cleaned_result = re.sub(r'^```\w*\n?', '', cleaned_result)
            cleaned_result = re.sub(r'\n?```$', '', cleaned_result)
            cleaned_result = cleaned_result.strip()

        result_map: dict[int, str] = {}

        if len(chunk.texts) == 1:
            # ── 单段落 chunk：不管 LLM 怎么拆，全部合并为一段 ──
            para_idx = chunk.paragraph_indices[0]
            original = chunk.texts[0]
            # 移除所有分隔符，合并为一个完整段落
            merged = cleaned_result.replace("◆◆PARA_BREAK◆◆", "").strip()
            # 把多余的连续换行压缩为空格
            merged = re.sub(r'\n{2,}', ' ', merged)
            merged = re.sub(r'\n', '', merged)
            if is_result_acceptable(original, merged):
                result_map[para_idx] = humanize_text(merged)
            else:
                print(f"[Chunk] Single paragraph quality check failed, using original.")
                result_map[para_idx] = original
        else:
            # ── 多段落 chunk：按分隔符拆分 ──
            result_parts = [p.strip() for p in cleaned_result.split("◆◆PARA_BREAK◆◆") if p.strip()]

            if len(result_parts) == len(chunk.texts):
                # 段落数量精确匹配 → 逐段映射
                for idx, (para_idx, rewritten) in enumerate(zip(chunk.paragraph_indices, result_parts)):
                    original = chunk.texts[idx]
                    if is_result_acceptable(original, rewritten):
                        result_map[para_idx] = humanize_text(rewritten)
                    else:
                        result_map[para_idx] = original
            else:
                # 数量不匹配 → 合并全部输出，按原始段落比例重新切分
                print(f"[Chunk] Paragraph count mismatch: expected {len(chunk.texts)}, got {len(result_parts)}. Merging and redistributing.")
                full_text = cleaned_result.replace("◆◆PARA_BREAK◆◆", "\n").strip()
                full_text = re.sub(r'\n{2,}', '\n', full_text)

                # 按原始段落的字数比例切分
                total_original_chars = sum(len(t) for t in chunk.texts)
                sentences = [s for s in re.split(r'(?<=[。！？.!?])', full_text) if s.strip()]

                if sentences and total_original_chars > 0:
                    # 按比例分配句子到各段落
                    para_groups: list[list[str]] = [[] for _ in chunk.texts]
                    char_targets = [len(t) / total_original_chars for t in chunk.texts]
                    current_para = 0
                    current_chars = 0
                    target_chars = char_targets[0] * len(full_text)

                    for sentence in sentences:
                        para_groups[current_para].append(sentence)
                        current_chars += len(sentence)
                        if current_chars >= target_chars and current_para < len(chunk.texts) - 1:
                            current_para += 1
                            current_chars = 0
                            target_chars = char_targets[current_para] * len(full_text)

                    for idx, para_idx in enumerate(chunk.paragraph_indices):
                        rewritten = "".join(para_groups[idx]).strip()
                        original = chunk.texts[idx]
                        if rewritten and is_result_acceptable(original, rewritten):
                            result_map[para_idx] = humanize_text(rewritten)
                        else:
                            result_map[para_idx] = original
                else:
                    # 完全无法切分 → 用原文
                    for idx, para_idx in enumerate(chunk.paragraph_indices):
                        result_map[para_idx] = chunk.texts[idx]

        return result_map


# ────────────────────── 公开接口：并行重写 ──────────────────────

async def rewrite_all_chunks_parallel(
    analyzed_doc: AnalyzedDocument,
    client: AsyncOpenAI,
    model: str,
    reference_list: str = "",
    max_concurrency: int = 5,
    max_chars_per_chunk: int = 2000,
) -> dict[int, str]:
    """
    并行重写文档中所有正文段落。

    流程：
    1. 按章节 + 字数切片 → SectionChunk 列表
    2. 所有 chunk 用 asyncio.gather 并行处理
    3. 合并结果

    Returns:
        {段落索引: 重写后文本} 的完整映射
    """
    chunks = group_into_section_chunks(analyzed_doc, max_chars_per_chunk)

    if not chunks:
        return {}

    semaphore = asyncio.Semaphore(max_concurrency)

    # 并行执行所有 chunk 的重写
    tasks = [
        _rewrite_chunk(client, model, chunk, reference_list, semaphore)
        for chunk in chunks
    ]

    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 合并结果
    all_results: dict[int, str] = {}
    for i, result in enumerate(chunk_results):
        if isinstance(result, Exception):
            print(f"[Chunk {i}] Error: {format_error_message(result)}")
            print(traceback.format_exc())
            # 出错的 chunk → 回退到原文
            chunk = chunks[i]
            for idx, para_idx in enumerate(chunk.paragraph_indices):
                all_results[para_idx] = chunk.texts[idx]
        else:
            all_results.update(result)

    return all_results


# ────────────────────── 兼容接口（保留流式输出能力） ──────────────────────

async def rewrite_body_paragraphs(
    analyzed_doc: AnalyzedDocument,
    client: AsyncOpenAI,
    model: str,
    reference_list: str = "",
) -> AsyncGenerator[tuple[int, str, str], None]:
    """
    兼容旧接口的流式输出包装。
    内部使用并行重写，完成后再逐段 yield。
    """
    from app.doc_analyzer import get_body_paragraphs

    # 并行重写所有段落
    all_results = await rewrite_all_chunks_parallel(
        analyzed_doc, client, model, reference_list,
    )

    # 按原始顺序逐段输出
    body_paragraphs = get_body_paragraphs(analyzed_doc)
    for para_idx, ap in body_paragraphs:
        original_text = ap.source.text.strip()
        rewritten_text = all_results.get(para_idx, original_text)
        yield para_idx, original_text, rewritten_text
