# KOL 信号抓取与交叉验证功能

最后更新：2026-05-14

这个目录承载一个独立的新功能：围绕微博、X、B 站等头部 KOL 账号，抓取他们近期公开内容，提取疑似事件，再与现有游戏新闻做交叉验证，形成更适合选题判断的“传播信号报告”。

当前阶段仍然保持这些边界：

- 不改现有 `scheduler.py` 日报主链路
- 不影响当前页面、收藏、笔记、AI 分析、全文分析
- 先作为独立入口开发和测试
- 优先支持“少量账号 -> 单独运行 -> 查看结果”

## 目标

相比单纯搜罗媒体新闻，这个功能更关注：

- 哪些事件被头部 KOL 主动提及、转发或放大
- 哪些事件同时被多个游戏媒体报道
- KOL 内容的公开互动数据是否说明传播影响更高
- 同一个事件在不同平台之间是否出现联动扩散

这个模块更像“选题信号层”，不是“新闻替代层”。

## 目录结构

```text
kol_signal/
├─ README.md
├─ runner.py
├─ models.py
├─ storage.py
├─ analyzer.py
├─ accounts.sample.json
└─ fetchers/
   ├─ __init__.py
   ├─ base.py
   ├─ weibo.py
   ├─ x.py
   └─ bilibili.py
```

## 当前边界

本目录目前只做这些事情：

1. 定义账号配置格式
2. 定义抓取后统一数据结构
3. 定义跨平台标准化互动指标
4. 定义与现有新闻数据的交叉验证逻辑
5. 提供一个独立 CLI 入口用于测试

当前还没有做：

- 浏览器自动化抓取
- 登录态托管
- 大规模历史回溯
- 接入主日报评分
- 页面 UI 展示

## 微博抓取说明

微博第一版会优先尝试公开移动端接口：

- `https://m.weibo.cn/api/container/getIndex`

如果公开接口被限流、返回空数据、或者触发风控，再回退到 Cookie 模式。

建议预留环境变量：

- `WEIBO_COOKIE`

如果测试时出现这些现象：

- 只能拿到极少内容
- 直接报风控
- 返回 432 / 418 / 空卡片

我们就切到 Cookie 模式，不需要大改结构。

## 图片识别说明

你提到这类微博账号筛选需要“看图识别”，所以第一版帖子结构里会保留：

- 图片链接列表
- 是否需要图片复核标记

当前先做到：

- 识别帖子是否带图
- 把图链接保留到标准化结构里
- 给后续视觉识别留好接口

后续再接入：

- 图片 OCR
- 图内游戏标题 / 角色名 / 联动信息识别
- 图文联合事件归类

## 建议验证顺序

### 第一步：微博双账号试跑

可以二选一：

- 复制 `kol_signal/accounts.sample.json` 为 `kol_signal/accounts.json` 后自行填写
- 直接让我帮你把测试账号写进 `kol_signal/accounts.json`

当前我已经按你的两个微博账号写好了本地 `kol_signal/accounts.json`，方便直接测试。

### 第二步：单独运行

建议先单独运行，不挂主链路：

```powershell
python kol_signal/runner.py --config kol_signal/accounts.json
```

如果要先建立登录态，先运行：

```powershell
python kol_signal/login.py --platform weibo
python kol_signal/login.py --platform bilibili
```

首次运行前还需要安装浏览器依赖：

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

登录完成后，抓取器会优先复用：

- `kol_signal/browser_state/weibo/`
- `kol_signal/browser_state/bilibili/`

然后你就可以强制走浏览器会话模式测试：

```powershell
python kol_signal/runner.py --config kol_signal/accounts.json --mode browser
```

### 第三步：观察输出

当前输出会写到：

- `kol_signal/output/latest_report.json`

后续真实抓取接上后，这里会看到：

- 原始账号内容摘要
- 标准化互动数据
- 与新闻条目的匹配结果
- 候选事件排序

## 后续阶段

### Phase 1

- 先接微博
- 先支持 2 个测试账号
- 先做最近公开内容抓取和标准化

### Phase 2

- 增加 X、B 站
- 做跨平台事件聚类

### Phase 3

- 接入 `scheduler.py` 作为额外信号层
- 在日报中补充 “KOL 热度信号”

## 注意事项

- 微博、X、B 站公开页面结构可能经常变化
- 真正落地抓取时，平台适配器需要分平台维护
- 部分平台可能需要浏览器访问或登录态，这部分先不在脚手架阶段做死
- 当前阶段仍然先保持独立子系统，避免影响现有主流程稳定性
- 浏览器登录态只建议本地使用，不建议放进云端任务
