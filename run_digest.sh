#!/bin/bash

# AI Daily Digest Cron Script
# 每日早上5点自动运行并发送到Discord和Obsidian

export PATH="$HOME/.bun/bin:$PATH"
# export OPENAI_API_KEY="sk-cp-vl-MpJmiJDVFWDMv84CL5VX0OE6m4QQInYjkBoh2qURq7e-0JaD-kxpzTJg3WmX5FL_Rf_eXCCmNZmYez9YEeOuBBeCTZcUIVxkZccO2Pd6GqK1fJmEYGyU"
# export OPENAI_API_BASE="https://api.minimaxi.com/v1"
# export OPENAI_MODEL="MiniMax-M2.5"

export OPENAI_API_KEY="sk-sp-9dfa7de313c04f849add58c228e895de"
export OPENAI_API_BASE="https://coding.dashscope.aliyuncs.com/v1"
export OPENAI_MODEL="kimi-k2.5"

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
    openclaw message send --channel feishu --target "ou_fb62627e866640f7b27ee26f38f7cc3b" --message "📰 **AI Daily Digest - ${DATE}**\n\n${content}...\n\n*完整报告已保存到Obsidian: Clippings/${DATE}-RSS简报.md*" 2>&1
fi
