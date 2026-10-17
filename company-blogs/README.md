# AI 公司博客

由上级目录的 `run_digest.sh` 在 RSS 日报之后调用。本地模型还开着时抓取并翻译，全部结束再关机。

流水线：爬取线程只负责抓取并写入 Obsidian 原文；翻译线程并行读取已落盘文件做双语翻译后写回。两者重叠执行，爬取不再被 LLM 拖慢。未译完的笔记下次会自动续译。

文章写到 `/Users/zhou/Documents/PycharmProject/my-kb/raw/ai-company-blogs/<公司名>/`，文件名是 `20261004-中文标题.md`（标题先译成中文，特殊字符处理与日报剪藏一致）。每次爬取后在同目录生成 `YYYYMMDD-AI公司博客简报.md`（格式对齐 RSS 简报），并用本地模型按四维提炼：具体问题、方法与理由、验证与数据条件、面向智能客服（呼入/外呼/文本）在生活服务·金融·保险·支付的小实验。正文图片不下载，Markdown 中保留博客原始图片地址（相对路径会解析为绝对 URL）。进度在本目录 `state/crawl.sqlite`。

单独补跑：

```bash
cd company-blogs
# 依赖装到本机全局环境：pip3 install -r requirements.txt
python3 -m digest run
python3 -m digest run --article-concurrency 3 --llm-concurrency 3
python3 -m digest status
```

站点与并行参数在 `sources.yaml`（`source_concurrency` / `article_concurrency` / `llm_concurrency`）。`llm_concurrency` 是每个翻译模型各自的并发，本地和云端各 3 路。翻译同时使用本地 `LOCAL_LLM_*` 和云端千问 `OPENAI_API_KEY` / `OPENAI_API_BASE` / `OPENAI_MODEL`。云端返回限流（5 小时 6000 次）后，当次和后续请求都改走本地模型；其他失败会改试另一边，两边都失败再用 `GEMINI_API_KEY`。`run_digest.sh` 会带上这些环境变量。
