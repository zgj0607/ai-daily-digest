#!/bin/bash
# AI Daily Digest：爬取 → 按需启 Ubuntu/LLM → 生成日报 → 关机 → 发飞书
set -euo pipefail

# .zshrc removed: causes unbound variable errors under set -euo pipefail
# Environment is set up explicitly below instead
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"

PIPELINE_DIR="${PIPELINE_DIR:-$HOME/Documents/PycharmProject/Video-Article-Generator}"
DIGEST_DIR="/Users/zhou/Documents/PycharmProject/ai-daily-digest"
UBUNTU_HOST="${AI_UBUNTU_HOST:-192.168.110.38}"

# 云端千问读 OPENAI_*。交互式 shell 没带上时，从 ~/.zshrc 补齐。
_zshrc_assign() {
  local name="$1" line
  [ -f "$HOME/.zshrc" ] || return 0
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$HOME/.zshrc" | tail -1 || true)
  [ -n "$line" ] || return 0
  printf '%s' "$line" | sed -E "s/^[^=]*=//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^[\"']//; s/[\"']$//"
}
[ -n "${OPENAI_API_KEY:-}" ] || OPENAI_API_KEY="$(_zshrc_assign OPENAI_API_KEY)"
[ -n "${OPENAI_API_BASE:-}" ] || OPENAI_API_BASE="$(_zshrc_assign OPENAI_API_BASE)"
[ -n "${OPENAI_MODEL:-}" ] || OPENAI_MODEL="$(_zshrc_assign OPENAI_MODEL)"
[ -n "${DEEPSEEK_API_KEY:-}" ] || DEEPSEEK_API_KEY="$(_zshrc_assign DEEPSEEK_API_KEY)"
[ -n "${MOONSHOT_API_KEY:-}" ] || MOONSHOT_API_KEY="$(_zshrc_assign MOONSHOT_API_KEY)"
[ -n "${MOONSHOT_API_BASE:-}" ] || MOONSHOT_API_BASE="$(_zshrc_assign MOONSHOT_API_BASE)"
[ -n "${MOONSHOT_MODEL:-}" ] || MOONSHOT_MODEL="$(_zshrc_assign MOONSHOT_MODEL)"
[ -n "${DASHSCOPE_API_KEY:-}" ] || DASHSCOPE_API_KEY="$(_zshrc_assign DASHSCOPE_API_KEY)"
[ -n "${DASHSCOPE_API_BASE:-}" ] || DASHSCOPE_API_BASE="$(_zshrc_assign DASHSCOPE_API_BASE)"
[ -n "${DASHSCOPE_MODELS:-}" ] || DASHSCOPE_MODELS="$(_zshrc_assign DASHSCOPE_MODELS)"
export OPENAI_API_KEY OPENAI_API_BASE OPENAI_MODEL DEEPSEEK_API_KEY MOONSHOT_API_KEY MOONSHOT_API_BASE MOONSHOT_MODEL DASHSCOPE_API_KEY DASHSCOPE_API_BASE DASHSCOPE_MODELS
_cloud_key="$OPENAI_API_KEY"
_cloud_base="$OPENAI_API_BASE"
_cloud_model="$OPENAI_MODEL"
_deepseek_key="$DEEPSEEK_API_KEY"
_moonshot_key="$MOONSHOT_API_KEY"
_moonshot_base="$MOONSHOT_API_BASE"
_moonshot_model="$MOONSHOT_MODEL"
_dashscope_key="$DASHSCOPE_API_KEY"
_dashscope_base="$DASHSCOPE_API_BASE"
_dashscope_models="$DASHSCOPE_MODELS"

if [ -f "$PIPELINE_DIR/mac/.env" ]; then
  # shellcheck disable=SC1091
  set -a; source "$PIPELINE_DIR/mac/.env"; set +a
  KEY=$(grep '^LITELLM_MASTER_KEY=' "$PIPELINE_DIR/mac/.env" | cut -d= -f2- || true)
fi
# mac/.env 里的 OPENAI_API_KEY 是本地 LiteLLM，云端配置以启动前的 OPENAI_* 为准
[ -n "$_cloud_key" ] && export OPENAI_API_KEY="$_cloud_key"
[ -n "$_cloud_base" ] && export OPENAI_API_BASE="$_cloud_base"
[ -n "$_cloud_model" ] && export OPENAI_MODEL="$_cloud_model"
[ -n "$_deepseek_key" ] && export DEEPSEEK_API_KEY="$_deepseek_key"
[ -n "$_moonshot_key" ] && export MOONSHOT_API_KEY="$_moonshot_key"
[ -n "$_moonshot_base" ] && export MOONSHOT_API_BASE="$_moonshot_base"
[ -n "$_moonshot_model" ] && export MOONSHOT_MODEL="$_moonshot_model"
[ -n "$_dashscope_key" ] && export DASHSCOPE_API_KEY="$_dashscope_key"
[ -n "$_dashscope_base" ] && export DASHSCOPE_API_BASE="$_dashscope_base"
[ -n "$_dashscope_models" ] && export DASHSCOPE_MODELS="$_dashscope_models"
unset _cloud_key _cloud_base _cloud_model _deepseek_key _moonshot_key _moonshot_base _moonshot_model _dashscope_key _dashscope_base _dashscope_models
export DEEPSEEK_API_BASE="${DEEPSEEK_API_BASE:-https://api.deepseek.com}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-flash}"
export MOONSHOT_API_BASE="${MOONSHOT_API_BASE:-https://api.moonshot.ai/v1}"
export MOONSHOT_MODEL="${MOONSHOT_MODEL:-kimi-k2.6}"
export DASHSCOPE_API_BASE="${DASHSCOPE_API_BASE:-https://dashscope.aliyuncs.com/compatible-mode/v1}"

# 本地模型单独配置，不覆盖上面的云端 OPENAI_*
export LOCAL_LLM_MODEL="qwen3.6-27b"
export LOCAL_LLM_API_KEY="${KEY:-${LITELLM_MASTER_KEY:-sk-litellm-local}}"

# .env 里空值不覆盖默认
export AI_UBUNTU_HOST="${AI_UBUNTU_HOST:-$UBUNTU_HOST}"
export AI_UBUNTU_USER="${AI_UBUNTU_USER:-zhou}"
export AI_PC_MAC_ADDRESS="${AI_PC_MAC_ADDRESS:-74:56:3c:3f:2a:4d}"
[ -z "$AI_PC_MAC_ADDRESS" ] && export AI_PC_MAC_ADDRESS="74:56:3c:3f:2a:4d"
UBUNTU_HOST="$AI_UBUNTU_HOST"
export LOCAL_LLM_API_BASE="http://${UBUNTU_HOST}:8000/v1"
_channels="本地 ${LOCAL_LLM_MODEL}"
if [ -n "${OPENAI_API_KEY:-}" ] && [ -n "${OPENAI_API_BASE:-}" ]; then
  _channels="${_channels} + 云端 ${OPENAI_MODEL:-qwen} (${OPENAI_API_BASE})"
fi
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  _channels="${_channels} + DeepSeek ${DEEPSEEK_MODEL} (${DEEPSEEK_API_BASE})"
fi
if [ -n "${MOONSHOT_API_KEY:-}" ]; then
  _channels="${_channels} + Kimi ${MOONSHOT_MODEL} (${MOONSHOT_API_BASE})"
fi
if [ -n "${DASHSCOPE_API_KEY:-}" ]; then
  _channels="${_channels} + 百炼按量 (${DASHSCOPE_API_BASE})"
fi
echo "[digest] 翻译通道: ${_channels}"
unset _channels

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

# 本地模型仍在运行，接着抓 AI 公司博客并翻译，完成后再关机（使用本机全局 python3）
BLOG_OBSIDIAN_DIR="/Users/zhou/Documents/PycharmProject/my-kb/raw/ai-company-blogs"
BLOG_SUMMARY="${BLOG_OBSIDIAN_DIR}/${DATE}-AI公司博客简报.md"
echo "[digest] 开始抓取 AI 公司博客..."
set +e
(cd "$DIGEST_DIR/company-blogs" && python3 -m digest run --summary "$BLOG_SUMMARY")
BLOG_STATUS=$?
set -e
if [ "$BLOG_STATUS" -ne 0 ]; then
  echo "[digest] 警告: 公司博客抓取失败 (exit=$BLOG_STATUS)"
fi

# digest 与博客都结束后再关机，不因飞书超时拖住 Ubuntu
cleanup
trap - EXIT

send_feishu() {
  local body="$1"
  local target="${HERMES_FEISHU_TARGET}"
  if [ -z "$target" ]; then
    echo "[digest] 警告: 未设置 HERMES_FEISHU_TARGET，跳过飞书"
    return 0
  fi
  set +e
  if command -v timeout >/dev/null 2>&1; then
    timeout "${HERMES_SEND_TIMEOUT_SEC:-90}" hermes send --to "$target" "$body" -q
  else
    hermes send --to "$target" "$body" -q
  fi
  local status=$?
  set -e
  return "$status"
}

if [ -f "$OUTPUT_FILE" ]; then
  mv "$OUTPUT_FILE" "$OBSIDIAN_PATH"
  content=$(
    sed '/^## 📊 数据概览/,$d; /^|/d; /^```mermaid/,/^```$/d; /<details>/,/<\/details>/d' "$OBSIDIAN_PATH" \
    | head -c 3500
  )
  echo "[digest] 发送飞书（RSS 简报）..."
  if ! send_feishu "${content}

...

*完整报告已保存到 Obsidian: Clippings/${DATE}-RSS简报.md*"; then
    echo "[digest] 警告: RSS 飞书发送失败或超时（报告已在 Obsidian）"
  fi
elif [ "$DIGEST_STATUS" -ne 0 ]; then
  echo "[digest] 警告: digest 失败 (exit=$DIGEST_STATUS)"
fi

if [ -f "$BLOG_SUMMARY" ]; then
  blog_content=$(
    sed '/^## 📊 数据概览/,$d; /^|/d; /^```mermaid/,/^```$/d; /<details>/,/<\/details>/d' "$BLOG_SUMMARY" \
    | head -c 3500
  )
  echo "[digest] 发送飞书（公司博客简报）..."
  if ! send_feishu "${blog_content}

...

*完整简报已保存到 Obsidian: ai-company-blogs/${DATE}-AI公司博客简报.md*"; then
    echo "[digest] 警告: 公司博客飞书发送失败或超时（简报已在 Obsidian）"
  fi
elif [ "$BLOG_STATUS" -ne 0 ]; then
  echo "[digest] 警告: 公司博客无简报可发送"
fi

echo "[digest] 所有任务完成"
exit "$DIGEST_STATUS"
