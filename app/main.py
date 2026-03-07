from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
import os
import uuid
import math
import re

from app.file_parser import parse_txt, parse_docx, parse_pdf
from app.llm_service import process_paper_stream, OUTPUT_DIR

app = FastAPI(title="Anti-AIGC Rewriter Commercial SaaS")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
static_dir = os.path.join(base_dir, "static")

os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")


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
        if filename.endswith(".txt"):
            extracted_text = parse_txt(file_bytes)
        elif filename.endswith(".docx"):
            extracted_text = parse_docx(file_bytes)
        elif filename.endswith(".pdf"):
            extracted_text = parse_pdf(file_bytes)
        else:
            raise HTTPException(status_code=400, detail="不支持的文件格式。仅支持 .txt, .docx, .pdf")
    elif text:
        extracted_text = text.strip()
    else:
        raise HTTPException(status_code=400, detail="请上传文件或输入文本")

    if not extracted_text:
        raise HTTPException(status_code=400, detail="提取到的文本为空")

    word_count = calculate_word_count(extracted_text)
    price = calculate_price(word_count)
    order_id = str(uuid.uuid4())

    return {
        "text": extracted_text,
        "word_count": word_count,
        "price": price,
        "order_id": order_id
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
    api_key: str = ""
    order_id: str
    references: str = ""

@app.post("/api/rewrite")
async def rewrite_text(req: RewriteRequest):
    """处理降重请求，返回流式响应（Server-Sent Events 思想或纯文本流）"""
    if not req.text or not req.order_id:
        raise HTTPException(status_code=400, detail="Missing text or order_id")

    api_key_to_use = req.api_key.strip()
    if not api_key_to_use:
        api_key_to_use = os.environ.get("GEMINI_API_KEY", "")
        
    if not api_key_to_use:
        raise HTTPException(status_code=400, detail="Missing API Key. Please provide one or set GEMINI_API_KEY environment variable.")
        
    return StreamingResponse(
        process_paper_stream(req.text, api_key_to_use, req.order_id, req.references),
        media_type="text/plain"
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
