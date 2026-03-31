# 游戏新闻选题 Bot

定时从 Slack 游戏新闻频道拉取内容，通过 LLM 分析热点价值，每天早上自动向飞书推送 Top 5 选题建议。

---

## 目录结构

```
├── .env               ← 填写你的密钥和配置（重要！）
├── config.py          ← 读取 .env，无需修改
├── slack_reader.py    ← 从 Slack 频道拉取消息
├── llm_analyzer.py    ← 调用 LLM 分析选题价值
├── feishu_sender.py   ← 推送飞书卡片消息
├── scheduler.py       ← 主入口 + 定时调度
└── requirements.txt
```

---

## 快速开始

### 第一步：安装依赖

> 需要 Python 3.9+

```powershell
pip install -r requirements.txt
```

---

### 第二步：配置 `.env`

打开 `.env` 文件，填写以下 3 个必填项：

| 配置项 | 说明 | 获取方式 |
|--------|------|---------|
| `SLACK_BOT_TOKEN` | Slack Bot Token | 见下方「Slack 配置」 |
| `LLM_API_KEY` | LLM API 密钥 | 见下方「LLM 配置」 |
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook 地址 | 见下方「飞书配置」 |

---

### 第三步：配置 Slack Bot

1. 前往 [https://api.slack.com/apps](https://api.slack.com/apps) → **Create New App**
2. 选择 **From scratch**，填写应用名称，选择你的 Workspace
3. 左侧菜单 → **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**
   添加以下权限：
   - `channels:history`（读取公开频道消息）
   - `channels:read`
   - `chat:write`（可选，用于调试）
4. 点击 **Install to Workspace**，复制 **Bot User OAuth Token**（`xoxb-` 开头）
5. 将 Bot 添加到游戏新闻频道：在 Slack 频道输入 `/invite @你的Bot名字`
6. 将 Token 填入 `.env` 的 `SLACK_BOT_TOKEN`

---

### 第四步：配置飞书机器人（小龙虾 Bot Webhook）

1. 打开飞书，进入你想接收报告的群聊
2. 点击右上角 **设置（齿轮图标）** → **机器人** → **添加机器人**
3. 选择 **自定义机器人**，填写名称（如「选题助手」），上传头像
4. 复制生成的 **Webhook URL**，填入 `.env` 的 `FEISHU_WEBHOOK_URL`
5. 可选：开启「签名校验」，将密钥填入 `FEISHU_WEBHOOK_SECRET`

---

### 第五步：配置 LLM（推荐 Deepseek）

> Deepseek API 价格极低，兼容 OpenAI 格式，国内可直接访问。

1. 前往 [https://platform.deepseek.com](https://platform.deepseek.com) 注册并充值
2. 创建 API Key，填入 `.env` 的 `LLM_API_KEY`
3. 默认已配置 `LLM_BASE_URL=https://api.deepseek.com`，无需修改

**其他可用 LLM（改 .env 即可切换）：**

| LLM | BASE_URL | MODEL |
|-----|----------|-------|
| Deepseek | `https://api.deepseek.com` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-max` |
| Claude | `https://api.anthropic.com/v1` | `claude-3-5-sonnet-20241022` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |

---

### 第六步：运行

**立即测试一次：**

```powershell
python scheduler.py --now
```

**每天定时自动运行（保持窗口开着）：**

```powershell
python scheduler.py
```

默认每天 `09:00` 运行，可在 `.env` 修改 `SCHEDULE_TIME`。

---

## 可调参数（`.env`）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `SCHEDULE_TIME` | `09:00` | 每天运行时间（24小时制） |
| `LOOKBACK_HOURS` | `24` | 拉取最近多少小时的新闻 |
| `TOP_N` | `5` | 推送 Top N 条选题 |

---

## 设置开机自启（可选，Windows）

1. 按 `Win + R`，输入 `taskschd.msc` 打开任务计划程序
2. 创建基本任务 → 触发器选「登录时」→ 操作选「启动程序」
3. 程序填 `python`，参数填 `"e:\AI 选题关注\scheduler.py"`
4. 工作目录填 `e:\AI 选题关注`

---

## 运行日志

每次运行会在当前目录生成 `bot.log`，记录详细运行信息，方便排查问题。
