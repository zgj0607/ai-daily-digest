#!/bin/bash

# AI Daily Digest Cron Script
# 每日早上7点自动运行，保存到 Obsidian 并通过 Hermes 发送到飞书
# 使用本地 litellm + Ubuntu 千问3.6-27b 模型
#
# 流程：先爬取 RSS（不启 GPU）→ digest 进入评分前再唤醒 Ubuntu/LLM
#      → digest 一结束立刻关 Ubuntu → 再发飞书（飞书超时不再拖住关机）

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

GPU_CLEANED=0
cleanup_gpu() {
    if [ "$GPU_CLEANED" -eq 1 ]; then
        return 0
    fi
    GPU_CLEANED=1
    echo "[digest] digest 已结束，正在关闭 Ubuntu / GPU（不等待飞书发送）..."
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
# digest 中途异常退出时仍关机；正常路径会在 digest 后立刻调用并置幂等标记
trap cleanup_gpu EXIT

# 生成日报：Step1-2 爬取无需 LLM；Step3 起由 DIGEST_ENSURE_LLM_CMD 按需启动
echo "[digest] 开始生成 AI 日报（爬取阶段不启动本地大模型）..."
# 本地 4090/Qwen 参数由 digest.ts 根据 OPENAI_API_BASE 自动选用（并发3 / 评分批10 / 摘要批6）
set +e
npx -y bun run scripts/digest.ts --hours 24 --top-n 30 --lang zh --output "$OUTPUT_FILE"
DIGEST_STATUS=$?
set -e

# 飞书发送经常超时；关机必须紧跟 digest，不能挂在 EXIT 上拖到飞书之后
cleanup_gpu

# 如果生成成功，复制到Obsidian并发送到飞书（失败/超时不影响 Ubuntu 已关机）
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
    HERMES_SEND_TIMEOUT_SEC="${HERMES_SEND_TIMEOUT_SEC:-90}"
    echo "[digest] 发送飞书（超时 ${HERMES_SEND_TIMEOUT_SEC}s）..."
    set +e
    if command -v timeout >/dev/null 2>&1; then
        timeout "$HERMES_SEND_TIMEOUT_SEC" hermes send \
            --to "$HERMES_FEISHU_TARGET" \
            "${content}

...

*完整报告已保存到 Obsidian: Clippings/${DATE}-RSS简报.md*" \
            -q 2>&1
        HERMES_STATUS=$?
    else
        hermes send \
            --to "$HERMES_FEISHU_TARGET" \
            "${content}

...

*完整报告已保存到 Obsidian: Clippings/${DATE}-RSS简报.md*" \
            -q 2>&1
        HERMES_STATUS=$?
    fi
    set -e
    if [ "$HERMES_STATUS" -ne 0 ]; then
        echo "[digest] 警告: 飞书发送失败或超时 (exit=$HERMES_STATUS)，报告已在 Obsidian"
    fi
elif [ "$DIGEST_STATUS" -ne 0 ]; then
    echo "[digest] 警告: digest 失败 (exit=$DIGEST_STATUS)，跳过飞书发送"
fi

echo "[digest] 所有任务完成"
exit "$DIGEST_STATUS"
