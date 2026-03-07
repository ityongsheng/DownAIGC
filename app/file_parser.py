import os
import io
import docx
import PyPDF2

def parse_txt(file_bytes: bytes) -> str:
    # Try utf-8 first, fallback to gbk (common in Chinese environments)
    try:
        return file_bytes.decode('utf-8')
    except UnicodeDecodeError:
        try:
            return file_bytes.decode('gbk')
        except UnicodeDecodeError:
            return file_bytes.decode('utf-8', errors='ignore')

def parse_docx(file_bytes: bytes) -> str:
    doc = docx.Document(io.BytesIO(file_bytes))
    return "\n".join([paragraph.text for paragraph in doc.paragraphs])

def parse_pdf(file_bytes: bytes) -> str:
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        return f"PDF解析错误: {str(e)}"
