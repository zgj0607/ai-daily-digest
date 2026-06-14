#!/bin/bash

# AI Daily Digest Cron Script
# 每日早上7点自动运行并发送到Discord和Obsidian

export PATH="$HOME/.bun/bin:$PATH"

export OPENAI_API_KEY="sk-sp-xxx"
export OPENAI_API_BASE="https://openai.com/v1"
export OPENAI_MODEL="gpt-4o-mini"

cd /Users/zhou/Documents/PycharmProject/ai-daily-digest

# 获取当天日期格式
DATE=$(date +%Y%m%d)

# 输出文件名
OUTPUT_FILE="/tmp/${DATE}-RSS简报.md"
OBSIDIAN_PATH="/Users/zhou/Documents/PycharmProject/my-kb/Clippings/${DATE}-RSS简报.md"

# 生成日报
npx -y bun run scripts/digest.ts --hours 24 --top-n 30 --lang zh --output "$OUTPUT_FILE"

# 如果生成成功，复制到Obsidian并发送到feishu
if [ -f "$OUTPUT_FILE" ]; then
    # 读取内容发送Discord（截取前4000字符避免超限）
    content=$(head -c 3500 "$OUTPUT_FILE")

    # 复制到Obsidian知识库
    mv "$OUTPUT_FILE" "$OBSIDIAN_PATH"    
    
    # 发送消息到feishu
    openclaw message send --channel feishu --target "ou_xxx" --message "📰 **AI Daily Digest - ${DATE}**\n\n${content}...\n\n*完整报告已保存到Obsidian: Clippings/${DATE}-RSS简报.md*" 2>&1
fi
