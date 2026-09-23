# CreatorReach Middle East - 

> 这是基于 CreatorReach AI 的本地改造版，面向沙特、UAE 与 GCC 的美妆、生活方式和眼妆达人定向邀约。
>
> 本项目不会自动发送邮件、社媒私信或 TikTok Shop 定向邀请；可管理草稿、人工审核包与实际发送回填。产品寄样、佣金、报价、广告授权、配送和产品宣称均须经人工确认。

## 本改造版增加内容

- 沙特 / GCC 彩色隐形眼镜定向邀约项目模板；
- 达人阿语、英语与阿英双语邀约偏好；
- “已核验的定制切入点”字段，避免虚构近期内容、受众和合作经历；
- 达人筛选页 `/screening`：按国家、平台、粉丝区间、内容标签、核验状态和竞品冲突筛选已录入候选；第一轮测试默认全球 100–1,000 粉，可输入任意两位国家代码（例如 SA、US、TH）；
- 站内定邀候选页 `/targeted-screening`：按 TikTok Shop 定邀口径（1,000–100,000 粉、成交 ≥100 件、平均播放 ≥100）预览粘贴表格，按 TikTok 账号跨项目查重，记录近 30 天邀约、黑名单、内容与受众核验，显示逐人排除或待补证据原因；候选仅在相同的 28 天或 30 天统计周期内相对排序。录入和核验不会发送邀请；站内发送仍需人工完成。
- FastMoss `.xlsx` 只读预览：标准库直接读取导出文件，按“近 28 天销量”和“近 28 天带货视频平均播放量”映射；默认可选择“有美妆信号且达到数字门槛”的候选。28 天与 30 天分别排序，28 天数据进入数据库后仍显示“代理口径、待人工核验”。
- 定邀审核包 `/targeted-invites`：每批至多 50 人，冻结至多 15 件商品、佣金、期限、样品规则和逐人入选依据；逐人审核文案，批次整体审核后由操作者在 TikTok Shop 实际发送并回填时间与凭据。七天跟进仅依据已回填的实际发送记录。
- 新增“全球美妆达人小规模测试（不发送）”项目模板；原沙特 / GCC 模板仍可使用；
- 针对眼部产品的风险提醒：不承诺医疗功效、注册状态、寄样、库存、运费、佣金或报价；
- 本地运行说明见 [`中东美瞳版_本地运行说明.md`](./中东美瞳版_本地运行说明.md)。

### 无 TikTok Shop 官方 API 时的定邀操作

1. 在 `/targeted-screening` 上传 FastMoss `.xlsx` 或粘贴人工整理的制表符表格、CSV、Markdown 表，先只读预览再确认录入。Excel 上传时要选目标项目及实际导出日期；默认导入范围是“有美妆信号且达到数字门槛”。预览不会写入数据库。
2. 逐人核对主页、统计周期、近期内容、受众、历史邀请、黑名单和竞品关系，并填写有证据的个性化切入点。FastMoss 的 28 天销量和带货视频播放是初筛代理，需人工明确接受该口径；来源国家不等于受众国家。只有资料齐全的候选可加入审核包。
3. 选择至多 50 人，按 TikTok Shop 后台销量顺序填写至多 15 件商品及本批佣金、期限、样品规则；逐人写入并审核站内草稿，最后审核整个批次。
4. 操作者在 TikTok Shop 完成邀请后，逐人回填实际发送时间、操作者和站内邀请编号或截图编号。保存草稿和审核通过都不会标记“已发送”。
5. 在 `/targeted-invites` 查看已发送满七天且仍无结果的待跟进项；完成站内跟进后再回填。接受、回复和拒绝也由操作者核实并记录。

当前实现不连接 TikTok Shop 官方 API，不自动抓取达人、不自动发送邀请。`data/workspace.sqlite3` 是本地工作数据且被 Git 忽略；推送 GitHub 只保存代码与文档，不会上传本地达人资料。审核包数据结构及验收条件见 [`站内定邀第二阶段审核包设计.md`](./docs/站内定邀第二阶段审核包设计.md)。

## 上游项目与许可证

本仓库保留上游 CreatorReach AI 的原始 `LICENSE` 文件及其代码基础。请在发布、商用、重新分发或对外提供服务前，阅读并遵守该许可证及所有适用的第三方平台、隐私和广告披露规则。

---

# CreatorReach AI - Open Source AI Creator Outreach CRM

中文名：AI 达人建联工作台

**CreatorReach AI** is an open-source influencer outreach CRM and AI creator outreach tool for KOL outreach, creator campaign management, Gmail draft automation, AI email generation, and AI reply classification.

It is designed for teams searching for an **AI influencer outreach tool**, **creator outreach CRM**, **KOL follow-up system**, **UGC creator outreach workflow**, **TikTok/Instagram/YouTube creator outreach tool**, or **local-first marketing CRM**.

It helps small teams run the core workflow:

**Campaign setup -> add creators -> generate first outreach email -> create Gmail drafts -> record creator replies -> AI summarize/classify replies -> track follow-up progress.**

Built for operators working with TikTok, Instagram, YouTube, Email creators, KOLs, influencers, affiliates, UGC creators, and cross-border ecommerce creator campaigns.

## Search-Friendly Summary

CreatorReach AI is relevant for these GitHub, Google, and AI-search queries:

- open source influencer outreach CRM
- AI creator outreach tool
- AI influencer outreach tool
- AI KOL outreach software
- creator outreach CRM
- KOL outreach CRM
- Gmail draft automation for influencer outreach
- AI cold email generator for creators
- AI email draft generator for KOL outreach
- TikTok creator outreach tool
- Instagram influencer outreach CRM
- YouTube creator outreach email generator
- UGC creator outreach workflow
- affiliate creator outreach system
- local-first SQLite marketing CRM
- 达人建联工具
- AI 达人建联工具
- KOL 建联 CRM
- 红人营销自动化
- 跨境电商达人建联

## Why This Exists

Most creator outreach work is scattered across spreadsheets, Gmail, chat tools, and manual notes. CreatorReach AI gives teams a simple local workspace to manage:

- AI-assisted creator outreach
- KOL / influencer follow-up
- Campaign and product context
- Gmail draft creation without auto-sending
- Creator reply classification
- Manual progress tracking
- Feishu webhook notifications
- SQLite-based local data storage

It is intentionally lightweight: one Python app, one SQLite database, no large framework, easy to run on a teammate's local computer.

## Keywords For AI And Search

This project is relevant to:

- AI influencer outreach tool
- AI creator outreach tool
- AI KOL outreach system
- influencer outreach CRM
- creator outreach CRM
- KOL CRM
- creator relationship management
- influencer relationship management
- influencer marketing automation
- creator marketing automation
- UGC creator outreach
- affiliate creator outreach
- TikTok creator outreach
- Instagram influencer outreach
- YouTube creator outreach
- Gmail draft automation
- Gmail OAuth compose app
- AI email draft generator
- AI reply classifier
- local-first CRM
- SQLite CRM
- campaign management for creators
- cross-border ecommerce influencer marketing
- 达人建联
- AI 达人建联
- KOL 建联
- 红人营销系统
- 达人运营工作台
- 海外达人运营
- 跨境电商达人营销
- 博主合作管理
- 达人邮件草稿
- 达人回复整理

## Features

### Campaign Management

Create campaigns with reusable product context:

- Product name
- Selling points
- Sample policy
- Commission / pricing rules
- Forbidden promises
- Brand tone

Campaign context is used by AI when generating outreach drafts and reply suggestions.

### Creator Progress Board

Add creators into a campaign and track each creator by stage:

- To contact
- First email pending review
- First outreach drafted
- First outreach sent
- First reply
- Negotiation / communication
- Sample stage
- Filming stage
- Completed

Priority and platform can be manually adjusted by operators.

### AI Reply Processing

When a creator replies, the system saves the original reply first, then runs AI processing.

AI output includes:

- Category
- Chinese summary
- Next action
- Reply draft
- Confidence
- Risk flags

Supported categories:

- interested
- ask_price
- ask_sample
- rejected
- posted
- needs_human
- invalid

### AI Safety Rules

The AI prompt is designed to avoid unsafe business commitments:

- Do not invent prices
- Do not invent commissions
- Do not promise stock
- Do not promise shipping time
- Do not promise sample delivery
- If price, commission, inventory, logistics, or sample shipping is involved, the draft must say manual confirmation is required

### Gmail Drafts

The app supports Gmail OAuth with the minimal compose scope:

```text
https://www.googleapis.com/auth/gmail.compose
```

It can create Gmail drafts, but it does **not** automatically send emails.

This keeps a human-in-the-loop workflow:

1. AI generates or assists with the email.
2. Operator reviews the draft.
3. System creates a Gmail draft.
4. Operator sends manually from Gmail.

### Feishu Notifications

Optional Feishu webhook notifications can alert operators when:

- A creator asks about price / commission
- A creator asks for samples
- A reply needs human handling
- AI processing fails
- Other important exceptions occur

### Error Logs

Errors are stored locally and grouped by source:

- AI
- Gmail
- Feishu
- App

Operators can review and mark errors as resolved.

## Tech Stack

- Python standard library HTTP server
- SQLite
- Gmail OAuth
- Gmail API drafts endpoint
- OpenAI-compatible chat completions API
- Feishu webhook
- Local-first storage

No complex frontend framework is required.

## Quick Start

```bash
python3 app.py
```

Open:

```text
http://127.0.0.1:8000/
```

If port 8000 is occupied:

```bash
PORT=8001 APP_BASE_URL=http://127.0.0.1:8001 python3 app.py
```

## Environment Variables

Create your own environment values. Do not commit secrets.

```bash
export PORT=8000
export APP_BASE_URL="http://127.0.0.1:8000"

export OPENAI_API_KEY="your_openai_compatible_api_key"
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_API_URL="https://api.openai.com/v1/chat/completions"

export FEISHU_WEBHOOK_URL="your_feishu_bot_webhook"

export GOOGLE_CLIENT_ID="your_google_oauth_client_id"
export GOOGLE_CLIENT_SECRET="your_google_oauth_client_secret"
export GOOGLE_REDIRECT_URI="http://127.0.0.1:8000/auth/google/callback"

python3 app.py
```

If `OPENAI_API_KEY` is not configured, the app uses a local fallback classifier for basic testing.

## Gmail OAuth Setup

1. Open Google Cloud Console.
2. Create or select a project.
3. Enable Gmail API.
4. Create OAuth 2.0 credentials.
5. Application type: Web application.
6. Authorized redirect URI:

```text
http://127.0.0.1:8000/auth/google/callback
```

7. Start the app.
8. Open Settings.
9. Click Connect Gmail.
10. Authorize with your Gmail account.

## Typical Workflow

1. Create a campaign.
2. Fill in product context, offer rules, forbidden promises, and brand tone.
3. Add creators with name, platform, homepage, and email.
4. Generate first outreach email.
5. Review and edit the draft.
6. Create a Gmail draft.
7. Send manually from Gmail.
8. Record creator replies.
9. Let AI classify and summarize replies.
10. Update creator progress.

## Security Notes

The local SQLite database can contain sensitive data after real usage:

- Gmail access tokens
- Gmail refresh tokens
- Creator emails
- Email draft body
- Creator replies
- AI-generated summaries
- Error logs

Do **not** commit `data/workspace.sqlite3`.

## Suggested GitHub Repository Description

```text
Local-first AI creator outreach workspace for influencer/KOL follow-up, Gmail drafts, campaign CRM, reply classification, and Feishu notifications.
```

## Suggested GitHub Topics

```text
ai
creator-economy
creator-outreach
influencer-outreach
kol
kol-outreach
influencer-marketing
creator-crm
gmail-api
gmail-oauth
email-drafts
campaign-management
sqlite
local-first
marketing-automation
tiktok
instagram
youtube
cross-border-ecommerce
ugc
```

## License

MIT License.
