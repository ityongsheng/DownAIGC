#!/bin/bash

# 切换到脚本所在目录，无论从哪里执行都可以正确定位到文件
cd "$(dirname "$0")" || exit 1

echo "======================================"
echo " Anti-AIGC Rewriter (SaaS) Local Start"
echo "======================================"

# 注意：绝对不要把真实的 API Key 硬编码在这个文件中并提交到公开代码库（如 GitHub）中！
# 否则会触发安全风控导致 Key 被谷歌立刻封禁作废。
# 如果想在本地方便使用，可以在终端手动执行 export GEMINI_API_KEY="你的_新_Key" 或填入网页栏
export GEMINI_API_KEY=""

# 如果当前没有 venv 目录，就自动帮用户创建一个
if [ ! -d "venv" ]; then
    echo "=> 正在初始化 Python 虚拟环境..."
    python3 -m venv venv
fi

echo "=> 激活虚拟环境..."
source venv/bin/activate

echo "=> 检查并安装依赖..."
pip install -r requirements.txt -q

echo "=> 启动核心后台服务..."
echo "=> 即将在浏览器中自动打开: http://127.0.0.1:8000/static/index.html"
echo ""

# 后台等待 2 秒后自动用 Mac 默认浏览器打开网页
(sleep 2 && open "http://127.0.0.1:8000/static/index.html") &

# 使用 uvicorn 启动服务端，并开启热更新
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
