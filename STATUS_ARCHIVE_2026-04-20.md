# 状态归档：2026-04-20

## 本次归档目的

记录 2026-04 中后期围绕“页面内 AI 分析稳定性”所做的结构调整与结论，作为后续继续做深度分析、AI 撰稿、结构化文章生成的基础。

## 当前结论

### 1. 标题分析原先存在三类问题

- 不同文章只按标题缓存，导致结果串用
- 模型偶尔返回 Markdown 样式字段，解析不稳
- 模型偶尔返回半成品，前端仍会展示并缓存

### 2. 已完成的修复

#### 缓存键修复

- 标题分析缓存由“只按 title”改为“title + link”
- 收藏区预填缓存也改为同样规则

#### 解析增强

- 兼容 Markdown 粗体、列表符号、标题符号
- 提升字段抽取稳定性

#### 不完整结果保护

- 服务端自动识别不完整分析
- 首轮不完整时自动重试一次
- 若两轮后仍不完整，不再展示半成品，而是提示稍后再试
- 前端只缓存完整结果

#### 诊断日志

Render 日志已加入：

- `[analysis]`
- `[full-analysis]`

可观察字段包括：

- `stage`
- `model`
- `max_tokens`
- `finish_reason`
- `content_len`
- `complete`

## 从 Render 日志得到的确认

已经观察到：

- 首轮快模型请求存在 `finish_reason = "length"` 的情况
- 说明第一轮确实有被 token/输出上限截断的真实情况
- 但服务端重试后可以拿到 `finish_reason = "stop"` 的完整返回

因此当前更合理的策略不是单纯继续猜，而是：

- 把不完整结果挡住
- 提升重试时的输出上限
- 持续通过日志确认 provider 的真实返回状态

## 当前代码状态

### 交互分析端

- `server.py`
- `render_server.py`

### 页面交互端

- `scheduler.py`
- `latest_news.html`

### 缓存与状态

- `interactive_analysis_cache.json`
- `analysis_cache.json`
- `seen_urls.json`

## 现在适合继续做的事

### 短期

- 继续观察 Render 中 `[analysis]` 日志
- 看是否还频繁出现 `finish_reason = "length"`
- 如果频繁出现，可进一步考虑切换更稳的模型或更固定的输出格式

### 中期

将标题分析输出改成更稳定的结构化协议，例如：

- JSON 对象
- 字段固定：判断、评分、价值分析、角度数组、建议标题

这样后面进入 AI 撰稿时，链路会更稳。

### 后续延展

在标题分析稳定后，再扩展到：

- 文章提纲生成
- 选题角度转写稿
- 多版本标题候选
- 基于全文分析的成稿草案

## 当前判断

项目现在已经可以作为一个“稳定可维护的 AI 选题工具”继续往下迭代。下一阶段最值得投入的是“结构化输出”和“AI 撰稿模板层”，而不是再回头堆更多抓取源。

## 2026-06-22 补充

为了避免旧的 Gist raw 链接和 Render 根地址继续被误当成公开入口，当前链路已经补充为：

- GitHub Pages 作为唯一公开浏览地址
- workflow 中显式注入 `REPORT_PUBLIC_URL`
- Render 根地址自动 302 跳转到 GitHub Pages

当前应统一使用：

- [https://saikadance.github.io/ai-news-bot/](https://saikadance.github.io/ai-news-bot/)
