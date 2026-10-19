# AI 公司博客

由上级目录的 `run_digest.sh` 在 RSS 日报之后调用。本地模型还开着时抓取并翻译，全部结束再关机。

流水线：爬取线程只负责抓取并写入 Obsidian 原文；翻译线程并行读取已落盘文件做双语翻译后写回。两者重叠执行，爬取不再被 LLM 拖慢。未译完的笔记下次会自动续译。

文章写到 `/Users/zhou/Documents/PycharmProject/my-kb/raw/ai-company-blogs/<公司名>/`，文件名是 `20261004-中文标题.md`（标题先译成中文，特殊字符处理与日报剪藏一致）。爬取和翻译结束后任务即结束，不再生成简报。正文图片不下载，Markdown 中保留博客原始图片地址（相对路径会解析为绝对 URL）。进度在本目录 `state/crawl.sqlite`。

单独补跑：

```bash
cd company-blogs
# 依赖装到本机全局环境：pip3 install -r requirements.txt
python3 -m digest run
python3 -m digest run --article-concurrency 3 --llm-concurrency 3
python3 -m digest status
python3 -m digest titles   # 只把已保存笔记的英文标题译成中文，不改正文
python3 -m digest clippings   # 扫描日报剪藏，英文标题和未译正文一次性译成中文
```

`bash run_digest.sh --titles` 会先启动 Ubuntu 上的本地模型，扫完标题后再关机。

站点与并行参数在 `sources.yaml`（`source_concurrency` / `article_concurrency` / `llm_concurrency`）。`llm_concurrency` 是每个翻译模型各自的并发。翻译同时使用本地 `LOCAL_LLM_*` 和 Coding Plan 云端千问。本地并发看 `llm_concurrency`，上下文 8096 token，按段落切开后再译。云端一篇一次请求，并发从 8 路加到最多 20 路；平台拒绝并发时只降速重试。这样避免把 Coding Plan 每 5 小时 6000 次请求打在分段往返上。5 小时或周/月额度用尽才关闭该模型。标题并进该模型的第一次请求。全部失败再用 `GEMINI_API_KEY`。
