#!/bin/bash

# AI Daily Digest Cron Script
# 每日早上7点自动运行，保存到 Obsidian 并通过 Hermes 发送到飞书
# 使用本地 litellm + Ubuntu 千问3.6-27b 模型
#
# 流程：先爬取 RSS（不启 GPU）→ digest 进入评分前再唤醒 Ubuntu/LLM → 完成后关机

set -euo pipefail

# 加载环境变量（cron 环境不会自动加载 .zshrc）
if [ -f "$HOME/.zshrc" ]; then
    # shellcheck disable=SC1090
    source "$HOME/.zshrc"
fi

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"

# AI-Creator-Pipeline 路径
PIPELINE_DIR="$HOME/Documents/PycharmProject/Video-Article-Generator"
DIGEST_DIR="/Users/zhou/Documents/PycharmProject/ai-daily-digest"
export PIPELINE_DIR

# LiteLLM 配置（本地 Mac 上的 AI-Creator-Pipeline）
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

# 仅保证 Mac Worker 可用（轻量，不启 GPU）。真正启 LLM 推迟到评分阶段。
echo "[digest] 检查 Mac Worker 服务..."
cd "$PIPELINE_DIR"
python3 skill/scripts/py/call_mac_api.py /health > /dev/null 2>&1 || {
    echo "[digest] Mac Worker 未运行，尝试启动..."
    conda run -n py313 --no-banner python -m mac.worker &
    sleep 5
}

# digest.ts 在爬取完成后、AI 评分前会执行该命令
export DIGEST_ENSURE_LLM_CMD="bash \"$DIGEST_DIR/scripts/ensure_llm.sh\""

cd "$DIGEST_DIR"

# 获取当天日期格式
DATE=$(date +%Y%m%d)

# 输出文件名
OUTPUT_FILE="/tmp/${DATE}-RSS简报.md"
OBSIDIAN_PATH="/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings/${DATE}-RSS简报.md"

cleanup_gpu() {
    echo "[digest] 任务结束，正在关闭 Ubuntu / GPU..."
    cd "$PIPELINE_DIR"
    python3 skill/scripts/py/call_mac_api.py /gpu/down --method POST > /dev/null 2>&1 || true
    python3 skill/scripts/py/call_mac_api.py /gpu/pc/off --method POST > /dev/null 2>&1 || true
    sleep 5
    status=$(python3 skill/scripts/py/call_mac_api.py /gpu/pc/status 2>/dev/null | grep -o '"offline"' || true)
    if [ -n "$status" ]; then
        echo "[digest] Ubuntu 已关机"
    else
        echo "[digest] 警告: Ubuntu 可能未完全关机"
    fi
}
trap cleanup_gpu EXIT

# 生成日报：Step1-2 爬取无需 LLM；Step3 起由 DIGEST_ENSURE_LLM_CMD 按需启动
echo "[digest] 开始生成 AI 日报（爬取阶段不启动本地大模型）..."
# 本地 4090/Qwen 参数由 digest.ts 根据 OPENAI_API_BASE 自动选用（并发3 / 评分批10 / 摘要批6）
npx -y bun run scripts/digest.ts --hours 24 --top-n 30 --lang zh --output "$OUTPUT_FILE"

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

echo "[digest] 所有任务完成"
