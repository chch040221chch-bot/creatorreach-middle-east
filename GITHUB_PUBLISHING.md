# GitHub Publishing Guide

## Repository Name Ideas

Recommended:

```text
creatorreach-ai
```

Alternatives:

```text
ai-creator-outreach-workspace
creator-outreach-crm
kol-outreach-sop
influencer-outreach-workbench
local-creator-crm
```

## GitHub Description

Use this:

```text
Local-first AI creator outreach workspace for influencer/KOL follow-up, Gmail drafts, campaign CRM, reply classification, and Feishu notifications.
```

## GitHub Topics

Add these topics in repository settings:

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

## Before First Push

Check that these files are not committed:

```text
data/workspace.sqlite3
.env
*.db
*.sqlite
*.sqlite3
__pycache__/
.pycache/
.pycache_check/
```

Run a quick secret scan:

```bash
rg -n "sk-|GOCSPX|access_token|refresh_token|FEISHU_WEBHOOK_URL=.*https|open-apis/bot|gmail.com|hotmail.com" .
```

If anything real appears, remove it before pushing.

## First Push

```bash
git init
git add .
git commit -m "Initial open-source release"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/creatorreach-ai.git
git push -u origin main
```

## README SEO / AI Search Notes

The README intentionally includes English and Chinese terms because AI search, code search, and repository summaries may match different user phrases:

- creator outreach
- influencer outreach
- KOL outreach
- creator CRM
- Gmail draft automation
- AI reply classifier
- 达人建联
- KOL 建联
- 红人营销
- 海外达人运营

