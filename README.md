# CreatorReach Middle East - 中东达人定向邀约工作台

> 这是基于 CreatorReach AI 的本地改造版，面向沙特、UAE 与 GCC 的美妆、生活方式和眼妆达人定向邀约。
>
> 本项目默认只生成与管理邀约草稿；不会自动发送邮件或社媒私信。产品寄样、佣金、报价、广告授权、配送和产品宣称均须经人工确认。

## 本改造版增加内容

- 沙特 / GCC 彩色隐形眼镜定向邀约项目模板；
- 达人阿语、英语与阿英双语邀约偏好；
- “已核验的定制切入点”字段，避免虚构近期内容、受众和合作经历；
- 达人筛选页 `/screening`：按国家、平台、粉丝区间、内容标签、核验状态和竞品冲突筛选已录入候选，默认沙特 100–1,000 粉；
- 针对眼部产品的风险提醒：不承诺医疗功效、注册状态、寄样、库存、运费、佣金或报价；
- 本地运行说明见 [`中东美瞳版_本地运行说明.md`](./中东美瞳版_本地运行说明.md)。

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
