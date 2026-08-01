#!/bin/bash

# AI Daily Digest Cron Script
# 每日早上7点自动运行，保存到 Obsidian 并通过 Hermes 发送到飞书
# 使用本地 litellm + Ubuntu 千问3.6-27b 模型

# 加载环境变量（cron 环境不会自动加载 .zshrc）
if [ -f "$HOME/.zshrc" ]; then
    source "$HOME/.zshrc"
fi

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"

# AI-Creator-Pipeline 路径
PIPELINE_DIR="$HOME/Documents/PycharmProject/Video-Article-Generator"

# LiteLLM 配置（本地 Mac 上的 AI-Creator-Pipeline）
# 直接使用 vLLM 端点以避免 litellm 代理延迟和参数传递问题
export OPENAI_API_BASE="http://192.168.110.38:8000/v1"
export OPENAI_MODEL="qwen3.6-27b"

# 从 Video-Article-Generator 的 .env 读取 LITELLM_MASTER_KEY 作为 API key
if [ -f "$PIPELINE_DIR/mac/.env" ]; then
    LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$PIPELINE_DIR/mac/.env" | cut -d'=' -f2-)
    if [ -n "$LITELLM_KEY" ]; then
        export OPENAI_API_KEY="$LITELLM_KEY"
    else
        export OPENAI_API_KEY="sk-litellm-local"
    fi
else
    export OPENAI_API_KEY="sk-litellm-local"
fi

# 确保 Mac Worker 服务运行
echo "[digest] 检查 Mac Worker 服务..."
cd "$PIPELINE_DIR"
python3 skill/scripts/py/call_mac_api.py /health > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "[digest] Mac Worker 未运行，尝试启动..."
    conda run -n py313 --no-banner python -m mac.worker &
    sleep 5
fi

# 唤醒 Ubuntu 并启动 LLM 服务
echo "[digest] 唤醒 Ubuntu..."
python3 skill/scripts/py/call_mac_api.py /gpu/pc/on --method POST > /dev/null 2>&1

# 等待 Ubuntu 上线
echo "[digest] 等待 Ubuntu 上线..."
for i in {1..12}; do
    status=$(python3 skill/scripts/py/call_mac_api.py /gpu/pc/status 2>/dev/null | grep -o '"online"')
    if [ -n "$status" ]; then
        echo "[digest] Ubuntu 已上线"
        break
    fi
    if [ $i -eq 12 ]; then
        echo "[digest] 错误: Ubuntu 未能上线"
        exit 1
    fi
    sleep 10
done

# 启动 LLM 服务
echo "[digest] 启动 LLM 服务..."
python3 skill/scripts/py/call_mac_api.py /gpu/up/llm --method POST > /dev/null 2>&1

# 等待 LLM 模型加载
echo "[digest] 等待 LLM 模型加载..."
for i in {1..18}; do
    # vLLM /health returns HTTP 200 with empty body when healthy
    http_code=$(curl -sS -o /dev/null -w "%{http_code}" http://192.168.110.38:8000/health 2>/dev/null)
    if [ "$http_code" = "200" ]; then
        # Also verify model is listed
        model_check=$(curl -sS http://192.168.110.38:8000/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null)
        if [ "$model_check" -gt 0 ] 2>/dev/null; then
            echo "[digest] LLM 模型已就绪"
            break
        fi
    fi
    if [ $i -eq 18 ]; then
        echo "[digest] 错误: LLM 模型加载超时"
        exit 1
    fi
    sleep 10
done

# 验证 vLLM 可用
echo "[digest] 验证 vLLM 服务..."
curl -sS http://192.168.110.38:8000/v1/models > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "[digest] 错误: vLLM 服务不可用"
    exit 1
fi

cd /Users/zhou/Documents/PycharmProject/ai-daily-digest

# 获取当天日期格式
DATE=$(date +%Y%m%d)

# 输出文件名
OUTPUT_FILE="/tmp/${DATE}-RSS简报.md"
OBSIDIAN_PATH="/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings/${DATE}-RSS简报.md"

# 生成日报（使用本地 litellm + 千问3.6）
echo "[digest] 开始生成 AI 日报..."
npx -y bun run scripts/digest.ts --hours 24 --top-n 30 --lang zh --ai-concurrency 5 --output "$OUTPUT_FILE"

# 如果生成成功，复制到Obsidian并发送到飞书
if [ -f "$OUTPUT_FILE" ]; then
    # 复制到Obsidian知识库
    mv "$OUTPUT_FILE" "$OBSIDIAN_PATH"

    # 截取预览文字发送到飞书（预处理为飞书可渲染的 Markdown）
    # Hermes 检测到 Markdown 表格时会降级为纯文本，导致飞书显示源码
    content=$(
        sed '/^## 📊 数据概览/,$d' "$OBSIDIAN_PATH" \
        | sed '/^|/d' \
        | sed '/^```mermaid/,/^```$/d' \
        | sed '/<details>/,/<\/details>/d' \
        | head -c 3500
    )

    HERMES_FEISHU_TARGET="${HERMES_FEISHU_TARGET:-feishu:ou_xxx}"
    hermes send \
        --to "$HERMES_FEISHU_TARGET" \
        "${content}

...

*完整报告已保存到 Obsidian: Clippings/${DATE}-RSS简报.md*" \
        -q 2>&1
fi

# 任务完成后释放 GPU 并关闭 Ubuntu
echo "[digest] 任务完成，正在关闭 Ubuntu..."
cd "$PIPELINE_DIR"

# 停止 GPU 服务
python3 skill/scripts/py/call_mac_api.py /gpu/down --method POST > /dev/null 2>&1

# 关闭 Ubuntu
python3 skill/scripts/py/call_mac_api.py /gpu/pc/off --method POST > /dev/null 2>&1

# 验证关机状态
sleep 5
status=$(python3 skill/scripts/py/call_mac_api.py /gpu/pc/status 2>/dev/null | grep -o '"offline"')
if [ -n "$status" ]; then
    echo "[digest] Ubuntu 已关机"
else
    echo "[digest] 警告: Ubuntu 可能未完全关机"
fi

echo "[digest] 所有任务完成"
