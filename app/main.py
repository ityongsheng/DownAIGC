from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from pydantic import BaseModel
from typing import List
import os
import uuid
import math
import re
import shutil
from openai import AsyncOpenAI, AuthenticationError

from app.file_parser import parse_txt, parse_docx, parse_pdf
from app.llm_service import process_paper_stream, OUTPUT_DIR, generate_paper_stream, normalize_api_key, validate_api_key_ascii
from app.search_service import search_and_answer


class ForceUTF8Middleware(BaseHTTPMiddleware):
    """Force text/event-stream responses to use UTF-8 encoding."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Override Content-Type to always include charset=utf-8 for streaming responses
        if (
            response.headers.get("content-type", "").startswith("text/event-stream")
            or response.headers.get("content-type", "").startswith("text/plain")
        ):
            response.headers["Content-Type"] = "text/plain; charset=utf-8"
        return response


app = FastAPI(title="Anti-AIGC Rewriter Commercial SaaS")

INPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")
os.makedirs(INPUT_DIR, exist_ok=True)
EXPORT_DIR = os.path.expanduser(os.environ.get("AIGC_EXPORT_DIR", "~/Downloads/AntiAIGCExports"))
os.makedirs(EXPORT_DIR, exist_ok=True)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Force UTF-8 for streaming / plain-text responses (fixes macOS ASCII codec errors)
app.add_middleware(ForceUTF8Middleware)

# Mount static files
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
static_dir = os.path.join(base_dir, "static")

os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav_path = os.path.join(static_dir, "favicon.png")
    if os.path.exists(fav_path):
        return FileResponse(fav_path)
    return HTTPException(status_code=404)

@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon():
    icon_path = os.path.join(static_dir, "favicon.png")
    if os.path.exists(icon_path):
        return FileResponse(icon_path)
    return HTTPException(status_code=404)


def calculate_word_count(text: str) -> int:
    """计算字数：中文字符算1个字，英文/数字序列算1个字"""
    # 提取中文
    chinese_chars = len(re.findall(r'[\u4e00-\u9fa5]', text))
    # 提取连续英文字母或数字作为一个单词
    english_words = len(re.findall(r'[a-zA-Z0-9]+', text))
    return chinese_chars + english_words

def calculate_price(word_count: int) -> float:
    """按每 1000 字 3 元计算，不足 1000 字按 1000 算"""
    if word_count == 0:
        return 0.0
    units = math.ceil(word_count / 1000)
    return float(units * 3.0)


@app.post("/api/upload_and_calculate")
async def upload_and_calculate(
        file: UploadFile = File(None),
        text: str = Form(None)
):
    """接收文件或文本，提取内容，计算字数和价格，返回订单信息"""
    extracted_text = ""
    
    if file:
        file_bytes = await file.read()
        filename = file.filename.lower()
        try:
            if filename.endswith(".txt"):
                extracted_text = parse_txt(file_bytes)
            elif filename.endswith(".docx"):
                extracted_text = parse_docx(file_bytes)
                # We delay saving until we have the order_id below.
            elif filename.endswith(".pdf"):
                extracted_text = parse_pdf(file_bytes)
            else:
                raise HTTPException(status_code=400, detail="不支持的文件格式。仅支持 .txt, .docx, .pdf")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"文件解析失败: {str(e)}")
    elif text:
        extracted_text = text.strip()
    else:
        raise HTTPException(status_code=400, detail="请上传文件或输入文本")

    if not extracted_text:
        raise HTTPException(status_code=400, detail="提取到的文本为空")

    word_count = calculate_word_count(extracted_text)
    price = calculate_price(word_count)
    order_id = str(uuid.uuid4())
    
    # [新增] 如果是 docx，保存原件到 uploads 目录以供原位处理
    if file and filename.endswith(".docx"):
        input_filepath = os.path.join(INPUT_DIR, f"input_{order_id}.docx")
        with open(input_filepath, "wb") as f:
            f.write(file_bytes)

    return {
        "text": extracted_text,
        "word_count": word_count,
        "price": price,
        "order_id": order_id,
        "preserve_format_supported": bool(file and filename.endswith(".docx"))
    }

class VerifyPaymentRequest(BaseModel):
    order_id: str

@app.post("/api/verify_payment")
async def verify_payment(request: VerifyPaymentRequest):
    """验证支付状态 (本阶段模拟直接成功)"""
    # 当前阶段 (MVP): 直接返回成功，模拟支付完成。
    return {"status": "success", "message": "支付验证成功"}


class RewriteRequest(BaseModel):
    text: str
    order_id: str
    references: str = ""
    api_key: str = ""

@app.post("/api/rewrite")
async def rewrite_text(req: RewriteRequest):
    """处理降重请求，返回流式响应（Server-Sent Events 思想或纯文本流）"""
    if not req.text or not req.order_id:
        raise HTTPException(status_code=400, detail="Missing text or order_id")

    return StreamingResponse(
        process_paper_stream(req.text, req.order_id, req.references, req.api_key),
        media_type="text/plain"
    )


class ValidateApiKeyRequest(BaseModel):
    api_key: str


@app.post("/api/validate_api_key")
async def validate_api_key(req: ValidateApiKeyRequest):
    api_key = normalize_api_key(req.api_key)
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写 API Key")
    try:
        validate_api_key_ascii(api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    model_name = os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V3")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
    )

    try:
        await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            stream=False
        )
        return {"status": "success", "model": model_name, "message": f"API Key 验证通过，可用于 {model_name}"}
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="API Key 无效，或与当前平台 / 模型不匹配")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"验证失败: {str(e)}")


class GeneratePaperRequest(BaseModel):
    outline: List[str]
    references: List[str]


@app.post("/api/generate_paper")
async def generate_paper(req: GeneratePaperRequest):
    """处理论文生成请求，采用 Prompt Chaining 模式分段生成，并返回 SSE 流式响应"""
    if not req.outline:
        raise HTTPException(status_code=400, detail="Missing outline")
    if not req.references:
        raise HTTPException(status_code=400, detail="Missing references")

    return StreamingResponse(
        generate_paper_stream(req.outline, req.references),
        media_type="text/event-stream"
    )

@app.get("/api/download/{order_id}")
async def download_word_document(order_id: str):
    """前端下载重写后生成的 Word 文档"""
    file_path = os.path.join(OUTPUT_DIR, f"output_{order_id}.docx")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Word document not found for this order ID.")
    
    return FileResponse(
        path=file_path,
        filename=f"Anti_AIGC_Rewrite_{order_id}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


class SaveOutputRequest(BaseModel):
    order_id: str


@app.post("/api/save_output")
async def save_output_document(req: SaveOutputRequest):
    source_path = os.path.join(OUTPUT_DIR, f"output_{req.order_id}.docx")
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="未找到可导出的 Word 文件")

    target_filename = f"Anti_AIGC_Rewrite_{req.order_id}.docx"
    target_path = os.path.join(EXPORT_DIR, target_filename)
    shutil.copy2(source_path, target_path)
    return {
        "status": "success",
        "path": target_path,
        "message": f"文件已保存到: {target_path}",
    }

class SearchRequest(BaseModel):
    query: str
    project_id: str = ""
    session_id: str = "-"

@app.post("/api/search")
async def search_knowledge_base(req: SearchRequest):
    """请求 Vertex AI Discovery Engine 获取问答结果"""
    project_id_to_use = req.project_id.strip() or os.environ.get("GCP_PROJECT", "")
    if not project_id_to_use:
        raise HTTPException(status_code=400, detail="Missing GCP Project ID.")
        
    if not req.query:
        raise HTTPException(status_code=400, detail="查询内容不能为空")
        
    try:
        result = search_and_answer(project_id_to_use, req.query, req.session_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"检索失败: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
