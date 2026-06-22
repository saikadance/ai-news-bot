# 游戏选题日报

当前项目是一个面向游戏资讯选题的日报工具，负责抓取多源新闻、筛选 Top5、生成日报页面，并提供页面内的 AI 分析、全文分析、收藏和便利贴能力。

## 当前公开入口

- 对外日报页面统一使用 GitHub Pages：
  [https://saikadance.github.io/ai-news-bot/](https://saikadance.github.io/ai-news-bot/)
- Render 仅承载交互式接口：
  - `AI 分析`
  - `全文分析`
  - `收藏`
  - `便利贴`
- 如果直接打开 Render 根地址，它现在会自动跳转到 GitHub Pages，而不是返回错误 JSON。

## 运行链路

1. `scheduler.py` 抓取新闻并做去重
2. `llm_analyzer.py` 对新增新闻做 Top5 筛选
3. 生成 `latest_news.html`
4. GitHub Actions 将页面部署到 GitHub Pages
5. 页面内交互由 `server.py` / `render_server.py` 提供
6. 飞书和 Slack 推送统一使用 GitHub Pages 公开链接

## 主要文件

- [scheduler.py](/E:/AI%20选题关注/scheduler.py)
- [server.py](/E:/AI%20选题关注/server.py)
- [render_server.py](/E:/AI%20选题关注/render_server.py)
- [gist_uploader.py](/E:/AI%20选题关注/gist_uploader.py)
- [share_url_helper.py](/E:/AI%20选题关注/share_url_helper.py)
- [PROJECT_STRUCTURE.md](/E:/AI%20选题关注/PROJECT_STRUCTURE.md)

## 部署说明

### GitHub Actions

- 定时运行日报任务
- 更新缓存文件
- 部署 `latest_news.html` 到 GitHub Pages

### GitHub Pages

- 当前唯一对外公开访问入口
- 用于飞书 / Slack 中的“查看完整新闻列表”

### Render

- 当前只负责交互式 API
- 不是对外分享页面的主入口

## 环境变量

核心变量包括：

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_FAST_MODEL`
- `FEISHU_WEBHOOK_URL`
- `FEISHU_WEBHOOK_SECRET`
- `SLACK_WEBHOOK_URL`
- `GITHUB_TOKEN`
- `GITHUB_GIST_ID`
- `ANALYZE_API_URL`
- `REPORT_PUBLIC_URL`

说明：

- `REPORT_PUBLIC_URL` 现在用于显式指定对外公开页面地址。
- `GITHUB_GIST_ID` 仍保留，因为收藏、便利贴、研究缓存等交互状态仍依赖 Gist 存储。

## 本地运行

安装依赖：

```powershell
pip install -r requirements.txt
```

立即跑一次：

```powershell
python scheduler.py --now
```

本地带预览服务：

```powershell
python scheduler.py --now --serve
```

## 当前建议

- 公开入口只发 GitHub Pages 链接
- 不再把 Gist raw 地址当作最终浏览地址
- Render 链接只用于服务可用性检查和日志排查
