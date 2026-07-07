#!/bin/bash

# AI Daily Digest Cron Script
# 每日早上7点自动运行，保存到 Obsidian 并通过 Hermes 发送到飞书

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"

# AI API 密钥从系统环境变量读取（GEMINI_API_KEY / OPENAI_API_KEY 等）
# 请在 ~/.zshrc 或 launchd/cron 环境中预先配置

cd /Users/zhou/Documents/PycharmProject/ai-daily-digest

# 获取当天日期格式
DATE=$(date +%Y%m%d)

# 输出文件名
OUTPUT_FILE="/tmp/${DATE}-RSS简报.md"
OBSIDIAN_PATH="/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings/${DATE}-RSS简报.md"

# 生成日报
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
