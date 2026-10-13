# AI 公司博客

由上级目录的 `run_digest.sh` 在 RSS 日报之后调用。本地模型还开着时抓取并翻译，全部结束再关机。

文章写到 `/Users/zhou/Documents/PycharmProject/my-kb/raw/ai-company-blogs/<公司名>/`，文件名是 `20261004-博客文章标题.md`（特殊字符处理与日报剪藏一致）。每次爬取后在同目录生成 `YYYYMMDD-公司博客简报.md`（格式对齐 RSS 简报）。正文图片下载到 `raw/assets/ai-company-blogs/<公司id>/`，Markdown 内改为相对路径 `../../assets/...`。进度在本目录 `state/crawl.sqlite`。

单独补跑：

```bash
cd company-blogs
# 依赖装到本机全局环境：pip3 install -r requirements.txt
python3 -m digest run
python3 -m digest run --article-concurrency 3 --llm-concurrency 3
python3 -m digest status
```

站点与并行参数在 `sources.yaml`（`source_concurrency` / `article_concurrency` / `llm_concurrency`）。翻译先用本地 `qwen3.6-27b`，失败后再用 `GEMINI_API_KEY`，最后用 `OPENAI_API_KEY` / `OPENAI_API_BASE` / `OPENAI_MODEL`。`run_digest.sh` 会带上这些环境变量。
