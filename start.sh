#!/bin/bash

# 切换到脚本所在目录，无论从哪里执行都可以正确定位到文件
cd "$(dirname "$0")" || exit 1

echo "======================================"
echo " Anti-AIGC Rewriter (SaaS) Local Start"
echo "ID： 214078202181"
echo "======================================"

# 注意：不要在此处硬编码任何敏感密码。由于已经切换到 Vertex AI ADC 机制，此脚本绝对安全。
# 如果想在本地方便使用，可以在终端手动运行 `gcloud auth application-default login`
# 并在系统环境变量中 `export GCP_PROJECT="你的_GCP项目ID"`
# export GCP_PROJECT=""

# 如果当前没有 venv 目录，就自动帮用户创建一个
if [ ! -d "venv" ]; then
    echo "=> 正在初始化 Python 虚拟环境..."
    python3 -m venv venv
fi

echo "=> 激活虚拟环境..."
source venv/bin/activate

echo "=> 检查并安装依赖..."
pip install -r requirements.txt -q

find_available_port() {
    local start_port="${1:-8000}"
    local port="$start_port"
    while lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; do
        port=$((port + 1))
    done
    echo "$port"
}

PORT="${AIGC_PORT:-$(find_available_port 8000)}"
APP_URL="http://127.0.0.1:${PORT}/static/index.html"

echo "=> 启动核心后台服务..."
echo "=> 本次实例端口: ${PORT}"
echo "=> 即将在浏览器中自动打开: ${APP_URL}"

# 强制 UTF-8 编码，解决 macOS Python stdout 中文输出报错
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export PYTHONIOENCODING=utf-8
echo ""

cleanup() {
    if [ -n "${UVICORN_PID:-}" ]; then
        # Kill the uvicorn process group (reloader + workers)
        kill -- -"$UVICORN_PID" >/dev/null 2>&1 || true
    fi
}

trap cleanup INT TERM EXIT

# 后台等待 2 秒后自动用 Mac 默认浏览器打开网页
(sleep 2 && open "${APP_URL}") &

# 使用 uvicorn 启动服务端，并开启热更新
./venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT}" &
UVICORN_PID=$!
wait "$UVICORN_PID"
