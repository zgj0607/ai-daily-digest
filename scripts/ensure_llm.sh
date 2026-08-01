#!/bin/bash
# 唤醒 Ubuntu 并启动本地 vLLM（仅在真正需要大模型时调用）

set -euo pipefail

PIPELINE_DIR="${PIPELINE_DIR:-$HOME/Documents/PycharmProject/Video-Article-Generator}"
LLM_HEALTH_URL="${LLM_HEALTH_URL:-http://192.168.110.38:8000/health}"
LLM_MODELS_URL="${LLM_MODELS_URL:-http://192.168.110.38:8000/v1/models}"

cd "$PIPELINE_DIR"

# 若模型已就绪则直接返回，避免重复启停
http_code=$(curl -sS -o /dev/null -w "%{http_code}" "$LLM_HEALTH_URL" 2>/dev/null || true)
if [ "$http_code" = "200" ]; then
  model_check=$(curl -sS "$LLM_MODELS_URL" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null || echo 0)
  if [ "${model_check:-0}" -gt 0 ] 2>/dev/null; then
    echo "[digest] LLM 已就绪，跳过启动"
    exit 0
  fi
fi

# 确保 Mac Worker 可调用
python3 skill/scripts/py/call_mac_api.py /health > /dev/null 2>&1 || {
  echo "[digest] Mac Worker 未运行，尝试启动..."
  conda run -n py313 --no-banner python -m mac.worker &
  sleep 5
}

echo "[digest] 唤醒 Ubuntu..."
python3 skill/scripts/py/call_mac_api.py /gpu/pc/on --method POST > /dev/null 2>&1

echo "[digest] 等待 Ubuntu 上线..."
for i in {1..12}; do
  status=$(python3 skill/scripts/py/call_mac_api.py /gpu/pc/status 2>/dev/null | grep -o '"online"' || true)
  if [ -n "$status" ]; then
    echo "[digest] Ubuntu 已上线"
    break
  fi
  if [ "$i" -eq 12 ]; then
    echo "[digest] 错误: Ubuntu 未能上线"
    exit 1
  fi
  sleep 10
done

echo "[digest] 启动 LLM 服务..."
python3 skill/scripts/py/call_mac_api.py /gpu/up/llm --method POST > /dev/null 2>&1

echo "[digest] 等待 LLM 模型加载..."
for i in {1..18}; do
  http_code=$(curl -sS -o /dev/null -w "%{http_code}" "$LLM_HEALTH_URL" 2>/dev/null || true)
  if [ "$http_code" = "200" ]; then
    model_check=$(curl -sS "$LLM_MODELS_URL" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null || echo 0)
    if [ "${model_check:-0}" -gt 0 ] 2>/dev/null; then
      echo "[digest] LLM 模型已就绪"
      exit 0
    fi
  fi
  if [ "$i" -eq 18 ]; then
    echo "[digest] 错误: LLM 模型加载超时"
    exit 1
  fi
  sleep 10
done
