#!/bin/bash
# AI Daily Digest：爬取 → 按需启 Ubuntu/LLM → 生成日报 → 关机 → 发飞书
set -euo pipefail

# .zshrc removed: causes unbound variable errors under set -euo pipefail
# Environment is set up explicitly below instead
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"

PIPELINE_DIR="${PIPELINE_DIR:-$HOME/Documents/PycharmProject/Video-Article-Generator}"
DIGEST_DIR="/Users/zhou/Documents/PycharmProject/ai-daily-digest"
UBUNTU_HOST="${AI_UBUNTU_HOST:-192.168.110.38}"

export OPENAI_MODEL="qwen3.6-27b"
export OPENAI_API_KEY="sk-litellm-local"

if [ -f "$PIPELINE_DIR/mac/.env" ]; then
  # shellcheck disable=SC1091
  set -a; source "$PIPELINE_DIR/mac/.env"; set +a
  KEY=$(grep '^LITELLM_MASTER_KEY=' "$PIPELINE_DIR/mac/.env" | cut -d= -f2- || true)
  [ -n "${KEY:-}" ] && export OPENAI_API_KEY="$KEY"
fi

# .env 里空值不覆盖默认
export AI_UBUNTU_HOST="${AI_UBUNTU_HOST:-$UBUNTU_HOST}"
export AI_UBUNTU_USER="${AI_UBUNTU_USER:-zhou}"
export AI_PC_MAC_ADDRESS="${AI_PC_MAC_ADDRESS:-74:56:3c:3f:2a:4d}"
[ -z "$AI_PC_MAC_ADDRESS" ] && export AI_PC_MAC_ADDRESS="74:56:3c:3f:2a:4d"
UBUNTU_HOST="$AI_UBUNTU_HOST"
export OPENAI_API_BASE="http://${UBUNTU_HOST}:8000/v1"

REMOTE_GPU="$PIPELINE_DIR/mac/scripts/remote-gpu.sh"
REMOTE_POWER="$PIPELINE_DIR/mac/scripts/remote-power.sh"
LLM_HEALTH="http://${UBUNTU_HOST}:8000/health"
LLM_MODELS="http://${UBUNTU_HOST}:8000/v1/models"

llm_ready() {
  [ "$(curl -sS -o /dev/null -w '%{http_code}' "$LLM_HEALTH" 2>/dev/null || true)" = "200" ] || return 1
  local n
  n=$(curl -sS "$LLM_MODELS" 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null || echo 0)
  [ "${n:-0}" -gt 0 ]
}

# 唤醒 Ubuntu + 启动 LLM（由 digest.ts 在评分前通过 DIGEST_ENSURE_LLM_CMD 调用）
ensure_llm() {
  if llm_ready; then
    echo "[digest] LLM 已就绪，跳过启动"
    return 0
  fi

  echo "[digest] 唤醒 Ubuntu..."
  bash "$REMOTE_POWER" on

  echo "[digest] 等待 Ubuntu 上线..."
  for _ in $(seq 1 12); do
    if bash "$REMOTE_POWER" status 2>/dev/null | grep -qi online; then
      echo "[digest] Ubuntu 已上线"
      break
    fi
    sleep 10
  done
  bash "$REMOTE_POWER" status 2>/dev/null | grep -qi online || {
    echo "[digest] 错误: Ubuntu 未能上线"
    return 1
  }

  echo "[digest] 启动 LLM..."
  bash "$REMOTE_GPU" start llm

  echo "[digest] 等待 LLM 加载..."
  for _ in $(seq 1 18); do
    if llm_ready; then
      echo "[digest] LLM 模型已就绪"
      return 0
    fi
    sleep 10
  done
  echo "[digest] 错误: LLM 模型加载超时"
  return 1
}

# 停止 LLM + 关闭 Ubuntu
stop_llm() {
  echo "[digest] 停止 GPU/LLM..."
  bash "$REMOTE_GPU" stop || true
  echo "[digest] 关闭 Ubuntu..."
  bash "$REMOTE_POWER" off || true

  echo "[digest] 等待 Ubuntu 关机..."
  for i in $(seq 1 18); do
    # status 在线打印 online；连不上或 offline 视为已关机
    if ! bash "$REMOTE_POWER" status 2>/dev/null | grep -qi online; then
      echo "[digest] Ubuntu 已关机"
      return 0
    fi
    if [ "$i" -eq 18 ]; then
      echo "[digest] 警告: Ubuntu 可能未完全关机"
      return 0
    fi
    sleep 5
  done
}

# 供 digest.ts 子进程调用：bash run_digest.sh --ensure-llm
if [ "${1:-}" = "--ensure-llm" ]; then
  ensure_llm
  exit $?
fi

DATE=$(date +%Y%m%d)
OUTPUT_FILE="/tmp/${DATE}-RSS简报.md"
OBSIDIAN_PATH="/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings/${DATE}-RSS简报.md"
export DIGEST_ENSURE_LLM_CMD="bash \"$DIGEST_DIR/run_digest.sh\" --ensure-llm"

STOPPED=0
cleanup() {
  [ "$STOPPED" -eq 1 ] && return
  STOPPED=1
  stop_llm
}
trap cleanup EXIT

cd "$DIGEST_DIR"
echo "[digest] 开始生成 AI 日报..."
set +e
npx -y bun run scripts/digest.ts --hours 24 --top-n 30 --lang zh --output "$OUTPUT_FILE"
DIGEST_STATUS=$?
set -e

# digest 一结束立刻关机，不因飞书超时拖住 Ubuntu
cleanup
trap - EXIT

if [ -f "$OUTPUT_FILE" ]; then
  mv "$OUTPUT_FILE" "$OBSIDIAN_PATH"
  content=$(
    sed '/^## 📊 数据概览/,$d; /^|/d; /^```mermaid/,/^```$/d; /<details>/,/<\/details>/d' "$OBSIDIAN_PATH" \
    | head -c 3500
  )
  target="${HERMES_FEISHU_TARGET}"
  echo "[digest] 发送飞书..."
  set +e
  if command -v timeout >/dev/null 2>&1; then
    timeout "${HERMES_SEND_TIMEOUT_SEC:-90}" hermes send --to "$target" \
      "${content}

...

*完整报告已保存到 Obsidian: Clippings/${DATE}-RSS简报.md*" -q
  else
    hermes send --to "$target" \
      "${content}

...

*完整报告已保存到 Obsidian: Clippings/${DATE}-RSS简报.md*" -q
  fi
  [ $? -ne 0 ] && echo "[digest] 警告: 飞书发送失败或超时（报告已在 Obsidian）"
  set -e
elif [ "$DIGEST_STATUS" -ne 0 ]; then
  echo "[digest] 警告: digest 失败 (exit=$DIGEST_STATUS)"
fi

echo "[digest] 所有任务完成"
exit "$DIGEST_STATUS"
