import os
from openai import OpenAI
from typing import Dict, Any

def search_and_answer(project_id: str, query: str, session_id: str = "-") -> Dict[str, Any]:
    """
    Search is temporarily mocked because gcloud was removed.
    We just use the standard LLM to answer.
    """
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY", "sk-bfviaezssywvsbruxbluhuwvdxxmjyingvgwcsfjtxntcpmg"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
    )
    MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V3")
    
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是一个有用的助手。根据用户的查询直接回答，不需要知识库。"},
                {"role": "user", "content": query}
            ]
        )
        answer_text = response.choices[0].message.content
    except Exception as e:
        answer_text = f"检索失败，且LLM调用异常: {str(e)}"

    return {
        "answer": answer_text,
        "session_id": session_id,
        "references": []
    }
