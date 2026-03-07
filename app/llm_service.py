import re
import os
import docx
import google.generativeai as genai
from typing import AsyncGenerator

SYSTEM_INSTRUCTION = """核心目标是大幅降低 AIGC 检测率。在重写时，请严格遵守以下 6 条规则：
1.极高文本波动率：交替使用极短句和长复句，绝不使用排比。
2.提升困惑度：禁用“首先、其次”等连接词，替换为“不可否认的是、结合实际情况来看”。
3.注入生涩感：术语准确，模仿真实本科生毕设的主观推导口吻。
4.核心语义不变：保留所有关键数据和论点。
5.精准锚定并保留参考文献：原文中的标号（如[1],[2]）必须精准转移到重写后对应论点的末尾。
6.【绝对禁止废话】直接输出重写后的正文内容！绝不允许包含任何解释性、确认性或过渡性的废话（严禁出现“好的，遵从您的指令”、“重构如下”、“前情提要”等词汇）！"""

# Ensure output directory exists for docx files
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def smart_chunking(text: str, min_chars: int = 800, max_chars: int = 1200) -> list[str]:
    """
    智能分块：按换行符和标点符号切分，确保尽量只在一个段落或长句子结束时截断。
    每个 chunk 尽量在 800 - 1200 字符。
    """
    lines = [line for line in re.split(r'(\n+)', text) if line]
    chunks = []
    current_chunk = ""

    def split_into_sentences(text_block: str) -> list[str]:
        # 匹配中文和英文的句号、感叹号、问号，允许后面跟着右引号或括号，以及空白符
        pattern = r'([。！？.!?][”\'\]\)]?\s*)'
        parts = re.split(pattern, text_block)
        sentences = []
        curr_s = ""
        for p in parts:
            curr_s += p
            if re.match(pattern, p):
                sentences.append(curr_s)
                curr_s = ""
        if curr_s:
            sentences.append(curr_s)
        return sentences

    for line in lines:
        if len(current_chunk) + len(line) <= max_chars:
            current_chunk += line
        else:
            if len(current_chunk) >= min_chars:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                sentences = split_into_sentences(line)
                for s in sentences:
                    if len(current_chunk) + len(s) > max_chars:
                        if current_chunk.strip():
                            chunks.append(current_chunk)
                        current_chunk = s
                    else:
                        current_chunk += s

    if current_chunk.strip():
        chunks.append(current_chunk)

    return chunks

async def process_paper_stream(text: str, api_key: str, order_id: str, reference_list: str = "") -> AsyncGenerator[str, None]:
    """
    接收长文本，智能分块，并用滑动窗口上下文流式重写
    并在最终将结果保存为 Word 文档。支持全局参考文献注入。
    """
    genai.configure(api_key=api_key)
    
    # 优先采用 gemini-2.5-pro 模型处理复杂的长文本重写任务
    model = genai.GenerativeModel(
        model_name="gemini-2.5-pro",
        system_instruction=SYSTEM_INSTRUCTION
    )

    chunks = smart_chunking(text)
    last_output = ""
    full_rewritten_text = ""

    try:
        for i, chunk in enumerate(chunks):
            # 构造带全局背景的提示词头部
            global_context_prompt = ""
            if reference_list.strip():
                global_context_prompt = f"【全局知识库】：以下是本文的完整参考文献列表，供你理解引用上下文（你不需要重写此列表，仅作参考）：\n{reference_list}\n\n---\n"

            if i == 0:
                prompt = f"{global_context_prompt}请重写以下学术论文的首个段落/部分：\n\n{chunk}"
            else:
                # 滑动窗口：获取上一段返回结果的最后 150 个字符
                context = last_output[-150:] if len(last_output) >= 150 else last_output
                prompt = f"{global_context_prompt}请阅读上一段结尾作为前情提要：“{context}”\n\n请重写以下后续的学术论文段落，并确保与新段落的前文过渡自然连贯：\n\n{chunk}"

            try:
                response = await model.generate_content_async(prompt, stream=True)
            except Exception as e:
                # Fallback to flash if pro is not found (404)
                if "404" in str(e) and "not found" in str(e).lower():
                    yield f"\n[系统提示：检测到当前 API Key 没有 gemini-2.5-pro 权限，自动降级为 gemini-2.5-flash 继续尝试...]\n"
                    model = genai.GenerativeModel(
                        model_name="gemini-2.5-flash",
                        system_instruction=SYSTEM_INSTRUCTION
                    )
                    response = await model.generate_content_async(prompt, stream=True)
                else:
                    raise e
            
            current_chunk_output = ""
            async for chunk_resp in response:
                chunk_text = ""
                try:
                    if hasattr(chunk_resp, "text") and chunk_resp.text:
                        chunk_text = chunk_resp.text
                except Exception as text_e:
                    # Some chunks might not have text (e.g. safety blocks, citations)
                    # or the library might throw TypeError on malformed parts
                    try:
                        if chunk_resp.candidates and chunk_resp.candidates[0].content.parts:
                            part = chunk_resp.candidates[0].content.parts[0]
                            if hasattr(part, "text"):
                                chunk_text = part.text
                    except Exception:
                        pass
                
                if chunk_text:
                    current_chunk_output += chunk_text
                    full_rewritten_text += chunk_text
                    yield chunk_text
                    
            last_output = current_chunk_output

            # 在每个 chunk 结束后生成特定分隔符
            if i < len(chunks) - 1:
                full_rewritten_text += "\n\n"
                yield "\n\n"
    except Exception as e:
        yield f"\n\n[连线中断或报错]: {str(e)}\n"

    # [新增需求] 重写完成后，将其保存为 DOCX 文件
    try:
        doc = docx.Document()
        for paragraph in full_rewritten_text.split('\n\n'):
            if paragraph.strip():
                doc.add_paragraph(paragraph.strip())
        
        output_filepath = os.path.join(OUTPUT_DIR, f"output_{order_id}.docx")
        doc.save(output_filepath)
        print(f"[{order_id}] Word document saved to: {output_filepath}")
    except Exception as e:
        print(f"[{order_id}] Failed to save word document: {str(e)}")

