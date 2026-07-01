# 新闻传播数据试验脚本

这个目录是独立试验区，用来尝试从新闻原文页补充阅读量、评论数、分享数等传播参考数据。

当前边界：

- 不发送飞书或 Slack。
- 不影响 Top5 排序。
- 不修改 `seen_urls.json`、`analysis_cache.json` 等日报状态。
- 只输出 `news_metrics/latest_metrics.json`，供日报页面按钮读取。

运行方式：

```powershell
python news_metrics/collector.py --limit 30
```

输出字段里 `confidence` 的含义：

- `none`：没有抓到公开互动数据。
- `partial`：抓到一部分字段，例如评论数。
- `good`：抓到两个以上字段，可作为较强参考。

