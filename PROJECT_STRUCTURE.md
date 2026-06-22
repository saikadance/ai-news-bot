# 项目结构说明

最后更新：2026-06-22

这份文档描述当前仓库的真实结构、主运行链路、关键状态文件，以及最近一轮与 AI 分析稳定性相关的调整。

## 项目定位

当前项目已经从早期的单一 Slack 机器人，演进为一个面向游戏资讯选题的日报生产工具，核心能力包括：

- 聚合多源游戏资讯
- 用 LLM 对新增新闻做 TopN 选题筛选
- 生成 HTML 日报页面
- 推送到飞书和 Slack
- 在页面内提供收藏、便利贴、单篇 AI 分析、全文深度分析

## 当前公开入口

- GitHub Pages 公开日报页：
  [https://saikadance.github.io/ai-news-bot/](https://saikadance.github.io/ai-news-bot/)
- Render 当前只承担交互接口，不作为最终对外浏览入口

## 顶层目录

```text
.
├─ .github/
│  └─ workflows/
│     └─ daily_news.yml          # GitHub Actions 定时任务
├─ api/
│  └─ analyze.py                 # Vercel Serverless 分析接口
├─ .env                          # 本地环境变量
├─ .gitignore
├─ README.md                     # 旧版说明，部分内容已落后
├─ PROJECT_STRUCTURE.md          # 当前项目结构说明
├─ STATUS_ARCHIVE_2026-04-20.md  # 2026-04-20 状态归档
├─ config.py                     # 配置读取与默认值
├─ scheduler.py                  # 主流程入口：抓取 -> 分析 -> 生成 -> 推送
├─ news_fetcher.py               # RSS/HTML 新闻抓取
├─ llm_analyzer.py               # TopN 分析逻辑
├─ analysis_cache.py             # 历史文章缓存与恢复
├─ url_cache.py                  # URL 去重缓存
├─ feishu_sender.py              # 飞书推送
├─ slack_sender.py               # Slack 推送
├─ gist_uploader.py              # Gist 更新与状态存储
├─ share_url_helper.py           # 公开页面 URL 推导与兜底
├─ server.py                     # 页面交互接口：收藏、笔记、AI 分析、全文分析
├─ render_server.py              # Render 启动入口
├─ latest_news.html              # 最近一次生成的 HTML 页面
├─ analysis_cache.json           # 历史新闻缓存
├─ seen_urls.json                # 已推送 URL 缓存
├─ top5_cache.json               # 最近一次 Top5 结果缓存
├─ interactive_analysis_cache.json # 交互式 AI 分析缓存
├─ slack_reader.py               # 旧 Slack 读取模块，非当前主链路
├─ social_fetcher.py             # 旧社媒抓取模块，当前已弱化
├─ topic_clusterer.py            # 旧热点聚类模块，当前已弱化
└─ 部署方案.html                  # 历史部署方案文档
```

## 当前主链路

当前实际运行主链路以 `scheduler.py` 为准：

1. 从配置里的 RSS 与部分 HTML 页面抓新闻
2. 用 `seen_urls.json` 做去重
3. 用 `analysis_cache.json` 恢复历史文章
4. 将“收藏过的文章”重新并入展示列表，避免被普通缓存清理掉
5. 仅对当天新增新闻做 TopN 选题分析
6. 生成 `latest_news.html`
7. 推送到飞书、Slack，并写入公开页面链接

## 交互式分析链路

页面内交互主要通过 `server.py` 提供：

- `GET /favorites`
- `POST /favorites`
- `GET /notes`
- `POST /notes`
- `POST /analyze`
- `POST /analyze_full`

当前交互式 AI 分析链路有这些保护：

- 普通标题分析缓存按 `title + link` 区分，避免不同文章串结果
- 缓存带版本号，便于在解析规则变化后自动失效旧缓存
- 服务端会记录 `finish_reason`、`content_len`、`complete` 到 Render 日志
- 如果模型返回不完整，服务端会自动重试一次
- 重试后仍不完整时，不再展示半成品，而是明确提示重试
- 前端只缓存 `complete !== false` 的结果，避免坏结果留在浏览器内存里

## 关键模块说明

### `config.py`

负责读取环境变量和默认值，包括：

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_FAST_MODEL`
- `FEISHU_WEBHOOK_URL`
- `SLACK_WEBHOOK_URL`
- `GITHUB_TOKEN`
- `GITHUB_GIST_ID`
- `ANALYZE_API_URL`
- `REPORT_PUBLIC_URL`

### `scheduler.py`

当前主流程入口，负责：

- 抓取与合并新闻
- 调用 TopN 分析
- 生成 HTML 页面
- 触发推送
- 在 HTML 中嵌入前端交互逻辑

### `server.py`

当前交互核心，负责：

- 收藏与取消收藏
- 笔记读写
- 普通标题分析
- 全文深度分析
- 本地/Render 预览服务

### `analysis_cache.py`

负责：

- 保存历史文章缓存
- 恢复缓存文章用于补全日报页面
- 合并收藏新闻与历史缓存新闻
- 控制普通历史新闻的保留期

### `slack_sender.py` 与 `feishu_sender.py`

负责日报推送。当前 Slack 推送已经与飞书并行接入。

## 部署结构

当前仓库涉及三条部署/运行链路：

### GitHub Actions

文件： `.github/workflows/daily_news.yml`

作用：

- 定时运行日报任务
- 安装依赖并执行 `python scheduler.py --now`
- 回写缓存文件
- 将 HTML 部署到 GitHub Pages
- 推送时统一使用 GitHub Pages 公开链接

### Render

启动入口： `render_server.py`

作用：

- 承载交互式服务端接口
- 让 HTML 页面里的 `AI 分析`、`全文分析`、`收藏`、`便利贴` 可在线使用
- 日志中可搜索 `[analysis]` 与 `[full-analysis]` 排查模型返回情况
- 访问 Render 根地址时会自动跳转到 GitHub Pages 公开页

### Vercel

当前仓库仍保留：

- `api/analyze.py`
- `vercel.json`

主要作为保留的在线分析接口结构，不是当前日报主流程核心。

## 关键状态文件

### 页面与内容

- `latest_news.html`
- `top5_cache.json`

### 去重与历史缓存

- `seen_urls.json`
- `analysis_cache.json`

### 交互缓存

- `interactive_analysis_cache.json`

说明：

- 这是交互式分析缓存
- 仅用于页面内点击 AI 分析后的结果缓存
- 当前已经通过版本号机制支持旧缓存自动失效

## 已弱化或历史遗留部分

以下模块仍在仓库里，但不是当前主链路核心：

- `slack_reader.py`
- `social_fetcher.py`
- `topic_clusterer.py`

它们更多是历史演进痕迹，后续可以视情况继续整理或拆分。

## 当前建议

如果接下来继续做“深度文章分析”和“AI 撰稿”，建议按这个顺序推进：

1. 先保持当前交互式分析链路稳定
2. 再把标题分析与全文分析的输出统一成更严格的结构化格式
3. 在结构化结果稳定后，再引入撰稿模板与自动生成正文
4. 后续把 README 更新到与当前实现一致
