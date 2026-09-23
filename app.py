import base64
import email.message
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "workspace.sqlite3"

CATEGORIES = [
    "interested",
    "ask_price",
    "ask_sample",
    "rejected",
    "posted",
    "needs_human",
    "invalid",
]

PLATFORMS = ["TikTok", "Instagram", "YouTube", "Email", "Other"]
PRIORITY_LEVELS = ["P0 异常", "P1 优先", "P2 推荐", "P3 观察", "P4 补资料"]


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_leading_marker(value):
    return re.sub(r"^\s*(?:[-*•]\s*|\d+[.)]\s*)", "", str(value or "")).strip()


def split_policy_items(value):
    text = str(value or "").replace("\r\n", "\n")
    pieces = []
    for raw_line in text.split("\n"):
        line = strip_leading_marker(raw_line)
        if not line:
            continue
        if line.lower().endswith("terms:") or line.lower().endswith("factors:"):
            pieces.append(line)
            continue
        subparts = re.split(r"\s+-\s+", line)
        pieces.extend(strip_leading_marker(part) for part in subparts if strip_leading_marker(part))
    return pieces


def extract_labeled_sections(value, labels):
    text = str(value or "")
    if not text.strip():
        return {}
    pattern = "|".join(re.escape(label) for label in labels)
    matches = list(re.finditer(rf"(?i)\b({pattern})\s*:", text))
    sections = {}
    for index, match in enumerate(matches):
        label = next((item for item in labels if item.lower() == match.group(1).lower()), match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[label] = clean_text(text[start:end])
    return sections


def project_info_card(title, content, kind="text"):
    if not content:
        content = "未填写"
    if kind == "list":
        items = content if isinstance(content, list) else split_policy_items(content)
        item_html = "".join(f"<li>{esc(item)}</li>" for item in items if item)
        body = f"<ul>{item_html}</ul>" if item_html else "<p>未填写</p>"
    elif kind == "link":
        body = f'<a href="{esc(content)}" target="_blank" rel="noreferrer">{esc(content)}</a>'
    else:
        body = f"<p>{esc(content)}</p>"
    return f'<div class="project-info-card"><span>{esc(title)}</span>{body}</div>'


def campaign_brief_html(campaign):
    selling = campaign["selling_points"] or ""
    sections = extract_labeled_sections(
        selling,
        ["Preferred subject", "Brand intro", "Creator fit", "Email structure", "Content direction", "Create Page"],
    )
    create_page = sections.get("Create Page") or ""
    if not create_page:
        match = re.search(r"https?://\S+", selling)
        create_page = match.group(0).rstrip(").,") if match else ""
    fallback_selling = clean_text(selling)
    cards = [
        project_info_card("首封邮件主题", sections.get("Preferred subject") or "未填写"),
        project_info_card("品牌介绍", sections.get("Brand intro") or fallback_selling),
        project_info_card("适合达人", sections.get("Creator fit")),
        project_info_card("邮件结构", sections.get("Email structure"), "list"),
        project_info_card("内容方向", sections.get("Content direction")),
        project_info_card("Create Page", create_page, "link" if create_page else "text"),
        project_info_card("合作权益", split_policy_items(campaign["commission_policy"] or ""), "list"),
        project_info_card("样品 / 体验政策", campaign["sample_policy"] or ""),
        project_info_card("禁止承诺项", campaign["forbidden_promises"] or default_forbidden_promises()),
        project_info_card("品牌语气", campaign["brand_tone"] or default_brand_tone()),
    ]
    return "".join(cards)


LIFECYCLE_STAGES = {
    "to_contact": ("待首次触达", "准备首封邮件"),
    "draft_generated": ("首封待确认", "检查文案并创建 Gmail 草稿"),
    "gmail_drafted": ("首次触达", "到 Gmail 草稿箱确认发送"),
    "sent": ("已首次触达", "等待达人首次回复"),
    "replied": ("首次回复", "整理达人诉求并继续沟通"),
    "talking": ("沟通环节", "确认报价、内容形式和排期"),
    "sample": ("寄样品", "跟进样品/访问权限和地址"),
    "filming": ("拍摄环节", "跟进内容制作和发布日期"),
    "done": ("项目结束", "归档合作结果"),
    "generating": ("生成中", "等待系统生成首封邮件"),
    "draft_failed": ("草稿异常", "进入详情查看错误并重试"),
}


NEXT_STAGE = {
    "to_contact": "draft_generated",
    "draft_generated": "gmail_drafted",
    "gmail_drafted": "sent",
    "sent": "replied",
    "replied": "talking",
    "talking": "sample",
    "sample": "filming",
    "filming": "done",
}
PREV_STAGE = {value: key for key, value in NEXT_STAGE.items()}


def lifecycle_stage(status):
    return LIFECYCLE_STAGES.get(status or "to_contact", (status or "待首次触达", "进入详情处理"))


def next_stage_value(status):
    return NEXT_STAGE.get(status or "to_contact")


def prev_stage_value(status):
    return PREV_STAGE.get(status or "to_contact")


def gmail_state_label(row):
    if "gmail_error" in row.keys() and row["gmail_error"]:
        return "Gmail 异常"
    return "已进草稿箱" if row["gmail_draft_id"] else "未创建"


def error_state_label(row):
    has_error = False
    for key in ("error_count", "gmail_error", "outreach_error"):
        if key in row.keys() and row[key]:
            has_error = True
    return "有异常" if has_error else "正常"


def short_id(value, head=8, tail=6):
    value = str(value or "")
    if len(value) <= head + tail + 3:
        return value or "-"
    return f"{value[:head]}...{value[-tail:]}"


def email_log_status_label(value):
    labels = {
        "draft_created": "已创建草稿",
        "failed": "创建失败",
        "sent": "已发送",
        "pending": "待处理",
    }
    return labels.get(value or "", value or "-")


def communication_count(creator_id):
    with db() as conn:
        creator = conn.execute("SELECT outreach_status, gmail_draft_id FROM creators WHERE id = ?", (creator_id,)).fetchone()
        reply_count = conn.execute("SELECT COUNT(*) FROM messages WHERE creator_id = ?", (creator_id,)).fetchone()[0]
    touched = bool(creator and (creator["gmail_draft_id"] or creator["outreach_status"] in ("sent", "replied", "talking", "sample", "filming", "done")))
    return reply_count + (1 if touched else 0)


def render_creator_progress_row(row):
    outreach_error = row["outreach_error"] if "outreach_error" in row.keys() else ""
    stage_label, next_action = lifecycle_stage(row["outreach_status"])
    next_value = next_stage_value(row["outreach_status"])
    prev_value = prev_stage_value(row["outreach_status"])
    comms = communication_count(row["id"])
    mail_state = "异常" if outreach_error else ("已生成" if row["outreach_draft"] else "未生成")
    gmail_state = "异常" if row["gmail_error"] else ("已进草稿箱" if row["gmail_draft_id"] else "未创建")
    priority_options = "".join(
        f'<option value="{esc(level)}" {"selected" if (row["priority_level"] or "") == level else ""}>{esc(level)}</option>'
        for level in PRIORITY_LEVELS
    )
    priority_select = f"""
        <form class="priority-form js-inline-action" method="post" action="/creators/{row['id']}/action">
          <input type="hidden" name="action" value="save_priority">
          <select class="priority-select {'manual' if (row['priority_manual'] if 'priority_manual' in row.keys() else 0) else ''}" name="priority_level">
            <option value="" {"selected" if not row["priority_level"] else ""}>未设置</option>
            {priority_options}
          </select>
        </form>
    """
    platform_options = "".join(option_html(platform, platform, row["platform"]) for platform in PLATFORMS)
    platform_select = f"""
        <form class="platform-form js-inline-action" method="post" action="/creators/{row['id']}/action">
          <input type="hidden" name="action" value="save_platform">
          <select class="compact-select" name="platform">
            {platform_options}
          </select>
        </form>
    """
    next_stage_label = lifecycle_stage(next_value)[0] if next_value else "已结束"
    prev_stage_label = lifecycle_stage(prev_value)[0] if prev_value else "无前一步"
    prev_button = (
        f'<button class="quiet-action js-stage-action" type="button" data-url="/creators/{row["id"]}/action" data-action="stage:{prev_value}" title="回到：{esc(prev_stage_label)}">前一步</button>'
        if prev_value
        else '<button class="quiet-action" type="button" disabled>前一步</button>'
    )
    advance_button = (
        f'<button class="primary-action js-stage-action" type="button" data-url="/creators/{row["id"]}/action" data-action="stage:{next_value}" title="推进到：{esc(next_stage_label)}">下一步</button>'
        if next_value
        else badge("已结束")
    )
    return f"""
      <div class="creator-progress-card">
        <div class="creator-progress-main">
          <a class="creator-progress-name" href="/creators/{row['id']}">{esc(row['name'])}</a>
          <div class="muted">{esc(row['email'] or '未填邮箱')}</div>
          <div class="creator-progress-tags">
            {badge(f'第 {comms} 次沟通' if comms else '尚未沟通')}
          </div>
        </div>
        <div class="creator-progress-platform">
          <span>平台</span>
          {platform_select}
        </div>
        <div class="creator-progress-stage">
          <span>当前阶段</span>
          <strong>{esc(stage_label)}</strong>
          <small>{esc(next_action)}</small>
        </div>
        <div class="creator-progress-status">
          <span>资料状态</span>
          <div>{badge(mail_state)} {badge(gmail_state)}</div>
        </div>
        <div class="creator-progress-priority">
          <span>优先级</span>
          {priority_select}
        </div>
        <div class="creator-progress-updated">
          <span>最近更新</span>
          <strong>{esc((row['updated_at'] or '')[:16])}</strong>
        </div>
        <div class="creator-progress-actions">
          <span>下一阶段：{esc(next_stage_label)}</span>
          <div class="row-actions">
            <a class="button secondary" href="/creators/{row['id']}">详情</a>
            {prev_button}
            {advance_button}
            <button class="quiet-action js-stage-action" type="button" data-url="/creators/{row['id']}/action" data-action="stage:done">完成</button>
          </div>
        </div>
      </div>
    """


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS creators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                profile_url TEXT,
                email TEXT,
                campaign_id INTEGER,
                outreach_status TEXT DEFAULT 'to_contact',
                outreach_subject TEXT,
                outreach_draft TEXT,
                outreach_generated_at TEXT,
                outreach_error TEXT,
                match_score INTEGER DEFAULT 0,
                content_fit_score INTEGER DEFAULT 0,
                commerce_potential_score INTEGER DEFAULT 0,
                contactability_score INTEGER DEFAULT 0,
                priority_level TEXT,
                score_reason TEXT,
                scored_at TEXT,
                gmail_draft_id TEXT,
                gmail_draft_created_at TEXT,
                gmail_error TEXT,
                notes TEXT,
                preferred_language TEXT DEFAULT 'English',
                personalization_hook TEXT,
                country TEXT,
                city TEXT,
                followers INTEGER,
                content_tags TEXT,
                audience_country TEXT,
                evidence_url TEXT,
                observed_at TEXT,
                competitor_conflict INTEGER DEFAULT 0,
                screening_status TEXT DEFAULT 'unverified',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                selling_points TEXT,
                sample_policy TEXT,
                commission_policy TEXT,
                forbidden_promises TEXT,
                brand_tone TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                product_name TEXT NOT NULL,
                product_selling_points TEXT,
                sample_policy TEXT,
                commission_policy TEXT,
                raw_reply TEXT NOT NULL,
                history_context TEXT,
                internal_notes TEXT,
                category TEXT,
                summary_zh TEXT,
                next_action TEXT,
                reply_draft TEXT,
                final_reply TEXT,
                confidence REAL,
                risk_flags TEXT,
                status TEXT NOT NULL,
                ai_error TEXT,
                feishu_notified_at TEXT,
                feishu_error TEXT,
                retry_count INTEGER DEFAULT 0,
                creator_email TEXT,
                gmail_draft_id TEXT,
                gmail_draft_created_at TEXT,
                gmail_error TEXT,
                sent_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (creator_id) REFERENCES creators(id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                related_type TEXT,
                related_id INTEGER,
                message TEXT NOT NULL,
                detail TEXT,
                resolved INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gmail_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                access_token TEXT,
                refresh_token TEXT,
                token_expiry TEXT,
                scope TEXT,
                connected_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                creator_id INTEGER,
                gmail_account_id INTEGER,
                to_email TEXT,
                subject TEXT,
                body TEXT,
                gmail_draft_id TEXT,
                gmail_message_id TEXT,
                status TEXT,
                error_message TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS campaign_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                task_name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                total_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );

            CREATE TABLE IF NOT EXISTS campaign_task_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES campaign_tasks(id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (creator_id) REFERENCES creators(id)
            );
            """
        )
        migrate_schema(conn)


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_column_if_missing(conn, table, column, definition):
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_schema(conn):
    add_column_if_missing(conn, "messages", "final_reply", "TEXT")
    add_column_if_missing(conn, "messages", "feishu_notified_at", "TEXT")
    add_column_if_missing(conn, "messages", "feishu_error", "TEXT")
    add_column_if_missing(conn, "messages", "retry_count", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "messages", "creator_email", "TEXT")
    add_column_if_missing(conn, "messages", "gmail_draft_id", "TEXT")
    add_column_if_missing(conn, "messages", "gmail_draft_created_at", "TEXT")
    add_column_if_missing(conn, "messages", "gmail_error", "TEXT")
    add_column_if_missing(conn, "campaigns", "forbidden_promises", "TEXT")
    add_column_if_missing(conn, "campaigns", "brand_tone", "TEXT")
    add_column_if_missing(conn, "creators", "email", "TEXT")
    add_column_if_missing(conn, "creators", "campaign_id", "INTEGER")
    add_column_if_missing(conn, "creators", "outreach_status", "TEXT DEFAULT 'to_contact'")
    add_column_if_missing(conn, "creators", "outreach_subject", "TEXT")
    add_column_if_missing(conn, "creators", "outreach_draft", "TEXT")
    add_column_if_missing(conn, "creators", "outreach_generated_at", "TEXT")
    add_column_if_missing(conn, "creators", "outreach_error", "TEXT")
    add_column_if_missing(conn, "creators", "match_score", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "creators", "content_fit_score", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "creators", "commerce_potential_score", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "creators", "contactability_score", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "creators", "priority_level", "TEXT")
    add_column_if_missing(conn, "creators", "priority_manual", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "creators", "score_reason", "TEXT")
    add_column_if_missing(conn, "creators", "scored_at", "TEXT")
    add_column_if_missing(conn, "creators", "gmail_draft_id", "TEXT")
    add_column_if_missing(conn, "creators", "gmail_draft_created_at", "TEXT")
    add_column_if_missing(conn, "creators", "gmail_error", "TEXT")
    add_column_if_missing(conn, "creators", "preferred_language", "TEXT DEFAULT 'English'")
    add_column_if_missing(conn, "creators", "personalization_hook", "TEXT")
    for column, definition in (
        ("country", "TEXT"), ("city", "TEXT"), ("followers", "INTEGER"),
        ("content_tags", "TEXT"), ("audience_country", "TEXT"),
        ("evidence_url", "TEXT"), ("observed_at", "TEXT"),
        ("competitor_conflict", "INTEGER DEFAULT 0"),
        ("screening_status", "TEXT DEFAULT 'unverified'"),
    ):
        add_column_if_missing(conn, "creators", column, definition)
    add_column_if_missing(conn, "email_logs", "creator_id", "INTEGER")
    conn.execute(
        """
        UPDATE creators
        SET outreach_error = gmail_error, gmail_error = NULL
        WHERE (outreach_error IS NULL OR outreach_error = '')
          AND gmail_error LIKE '%timed out%'
          AND (gmail_draft_id IS NULL OR gmail_draft_id = '')
        """
    )
    conn.execute(
        """
        UPDATE creators
        SET gmail_error = NULL
        WHERE gmail_error LIKE '%timed out%'
          AND gmail_draft_id IS NOT NULL
          AND gmail_draft_id != ''
        """
    )


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def default_campaign_href():
    with db() as conn:
        row = conn.execute("SELECT id FROM campaigns ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
    return f"/campaigns/{row['id']}" if row else "/campaigns?edit=0"


def nav_active(title, label):
    mapping = {
        "首页": "今日工作台",
        "录入回复": "达人数据库",
        "回复详情": "达人数据库",
        "达人详情": "达人数据库",
        "Campaign 管理": "达人数据库",
        "设置": "设置",
        "错误日志": "错误日志",
        "达人跟进中心": "达人数据库",
        "Gmail 草稿": "Gmail 草稿",
        "达人筛选": "达人筛选",
    }
    return " active" if mapping.get(title, title) == label else ""


def layout(title, body, extra_head=""):
    nav_items = [
        ("今日工作台", "/", "总览"),
        ("达人筛选", "/screening", "候选筛选"),
        ("达人数据库", "/creators", "达人记录"),
        ("Gmail 草稿", "/gmail", "邮件草稿"),
        ("错误日志", "/errors", "异常处理"),
        ("设置", "/settings", "账号与配置"),
    ]
    nav_html = "".join(
        f"""
        <a class="top-link{nav_active(title, label)}" href="{href}">{label}</a>
        """
        for label, href, subtitle in nav_items
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - CreatorReach AI</title>
  <style>
    :root {{
      --bg: #f5f1e8;
      --panel: #ffffff;
      --text: #1a1a1a;
      --muted: #6f6658;
      --line: #e8ddc2;
      --brand: #ffd400;
      --brand-dark: #1a1a1a;
      --brand-soft: #fff2a8;
      --soft: #fff8df;
      --soft-line: #efe2b8;
      --warn: #8a6500;
      --bad: #b42318;
      --good: #1a1a1a;
      --sidebar: #ffffff;
      --shadow: 0 22px 60px rgba(26, 26, 26, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      overflow-x: hidden;
    }}
    a {{ color: var(--text); text-decoration: none; }}
    a:hover {{ text-decoration: underline; text-decoration-thickness: 2px; text-decoration-color: var(--brand); }}
    .app-shell {{
      display: block;
      min-height: 100vh;
      padding: 18px;
    }}
    .sidebar {{
      width: auto;
      flex: none;
      background: var(--sidebar);
      color: var(--text);
      border: 1px solid #eadfbd;
      border-radius: 28px;
      padding: 20px 14px;
      position: sticky;
      top: 18px;
      height: calc(100vh - 36px);
      box-shadow: var(--shadow);
    }}
    .brand-block {{
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 4px 8px 22px;
    }}
    .logo {{
      width: 44px;
      height: 44px;
      border-radius: 15px;
      background: var(--brand);
      color: var(--text);
      display: grid;
      place-items: center;
      font-weight: 800;
      border: 1px solid rgba(26, 26, 26, .12);
      box-shadow: 0 14px 26px rgba(255, 212, 0, .35);
    }}
    .brand-title {{ font-weight: 900; color: var(--text); letter-spacing: 0; }}
    .brand-subtitle {{ display: block; color: var(--muted); font-size: 12px; margin-top: 1px; }}
    .side-nav {{ display: grid; gap: 8px; }}
    .side-link {{
      display: grid;
      grid-template-columns: 18px 1fr;
      gap: 9px;
      align-items: center;
      padding: 13px 12px;
      border-radius: 18px;
      color: var(--text);
      border: 1px solid transparent;
      transition: background .12s ease, transform .12s ease, box-shadow .12s ease;
    }}
    .side-link strong {{ display: block; font-size: 14px; line-height: 1.2; }}
    .side-link small {{ display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }}
    .side-link:hover {{ background: #fff8df; text-decoration: none; transform: translateX(2px); }}
    .side-link.active {{ background: var(--brand); color: var(--text); border-color: #f1cb00; box-shadow: 0 14px 28px rgba(255, 212, 0, .26); }}
    .side-link.active small {{ color: var(--text); }}
    .side-dot {{ width: 9px; height: 9px; border-radius: 999px; background: #d8cfb8; justify-self: center; }}
    .side-link.active .side-dot {{ background: var(--text); }}
    .main-area {{ min-width: 0; flex: 1; }}
    .mini-brand {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-weight: 950;
      white-space: nowrap;
    }}
    .mini-brand .logo {{
      width: 36px;
      height: 36px;
      border-radius: 13px;
      box-shadow: 0 10px 18px rgba(255, 212, 0, .32);
    }}
    .top-nav {{
      min-height: 64px;
      border: 1px solid #eadfbd;
      border-radius: 28px;
      background: #ffffff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 18px;
      position: sticky;
      top: 18px;
      z-index: 5;
      box-shadow: 0 18px 40px rgba(26, 26, 26, .06);
    }}
    .category-nav {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 850;
      letter-spacing: 0;
      white-space: nowrap;
      overflow-x: auto;
    }}
    .category-nav a {{
      padding: 8px 10px;
      border-radius: 999px;
    }}
    .category-nav a:hover {{
      background: var(--soft);
      text-decoration: none;
    }}
    .top-link.active {{
      background: var(--brand);
      color: var(--text);
      box-shadow: 0 10px 22px rgba(255, 212, 0, .28);
    }}
    .category-nav span {{ color: #c3b99f; }}
    .top-actions {{ display: flex; gap: 10px; align-items: center; flex: 0 0 auto; }}
    .wrap {{ max-width: 1480px; margin: 0 auto; padding: 24px 8px 52px; }}
    .page-head {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 20px;
      padding: 26px;
      border-radius: 30px;
      background: #1a1a1a;
      color: #ffffff;
      box-shadow: 0 26px 70px rgba(26, 26, 26, .14);
    }}
    .page-head .button.secondary {{ background: #ffffff; }}
    .page-kicker {{ color: #e8dec5; margin: 8px 0 0; max-width: 760px; }}
    h1 {{ margin: 0; font-size: 34px; line-height: 1.08; letter-spacing: 0; font-weight: 950; }}
    h2 {{ margin: 0 0 14px; font-size: 20px; letter-spacing: 0; font-weight: 900; }}
    .panel {{
      background: var(--panel);
      border: 1px solid #efe6c8;
      border-radius: 28px;
      padding: 24px;
      margin-bottom: 20px;
      box-shadow: var(--shadow);
    }}
    .grid {{ display: grid; gap: 14px; }}
    .stats {{ grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); margin-bottom: 22px; }}
    .stat {{
      background: #ffffff;
      border: 1px solid #efe6c8;
      border-radius: 26px;
      padding: 22px;
      min-height: 118px;
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      justify-content: flex-end;
    }}
    .stat::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      right: 0;
      height: 10px;
      background: var(--brand);
      border-bottom: 0;
    }}
    .stat::after {{
      content: "";
      position: absolute;
      right: 16px;
      top: 18px;
      width: 34px;
      height: 34px;
      border-radius: 14px;
      background: #1a1a1a;
      opacity: .08;
    }}
    .stat strong {{ display: block; font-size: 34px; line-height: 1.05; font-weight: 950; }}
    .stat span {{ color: var(--muted); font-size: 14px; font-weight: 750; }}
    label {{ display: block; font-weight: 750; margin-bottom: 7px; }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid #e2d7b9;
      border-radius: 16px;
      padding: 12px 14px;
      font: inherit;
      background: #fffdf7;
    }}
    input:focus, select:focus, textarea:focus {{
      outline: 3px solid var(--brand);
      outline-offset: 1px;
    }}
    textarea {{ min-height: 112px; resize: vertical; }}
    .form-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .full {{ grid-column: 1 / -1; }}
    .required {{ color: var(--bad); }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 16px; }}
    button, .button {{
      appearance: none;
      border: 1px solid rgba(26, 26, 26, .18);
      background: var(--brand);
      color: var(--text);
      border-radius: 999px;
      padding: 10px 18px;
      min-height: 42px;
      font: inherit;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      font-weight: 800;
      box-shadow: 0 8px 18px rgba(255, 212, 0, .28);
      transition: transform .12s ease, box-shadow .12s ease, background .12s ease;
    }}
    button:hover, .button:hover {{
      background: var(--text);
      color: #ffffff;
      text-decoration: none;
      transform: translateY(-1px);
      box-shadow: 0 12px 24px rgba(26, 26, 26, .16);
    }}
    button.secondary, .button.secondary {{
      background: #ffffff;
      color: var(--text);
      border-color: #d8ceb5;
      box-shadow: 0 8px 20px rgba(26, 26, 26, .06);
    }}
    button.secondary:hover, .button.secondary:hover {{ background: var(--brand); color: var(--text); }}
    button.danger {{ background: var(--text); border-color: var(--text); color: #ffffff; }}
    table {{ width: 100%; border-collapse: separate; border-spacing: 0 10px; background: transparent; }}
    th, td {{ padding: 13px 12px; border-bottom: 0; vertical-align: middle; text-align: left; }}
    th {{ font-size: 12px; color: #786d58; font-weight: 900; background: transparent; text-transform: uppercase; }}
    th:first-child {{ border-radius: 0; }}
    th:last-child {{ border-radius: 0; }}
    thead tr {{ background: transparent; box-shadow: none; }}
    tbody tr {{ background: #ffffff; box-shadow: 0 14px 34px rgba(26, 26, 26, .055); }}
    tbody tr:hover td {{ background: #fff8dc; }}
    tbody td {{ border-top: 1px solid #f0e7cc; border-bottom: 1px solid #f0e7cc; }}
    tbody td:first-child {{ border-left: 1px solid #f0e7cc; border-radius: 22px 0 0 22px; font-weight: 850; }}
    tbody td:last-child {{ border-right: 1px solid #f0e7cc; border-radius: 0 22px 22px 0; }}
    .muted {{ color: var(--muted); }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 13px;
      background: #ffffff;
      color: var(--text);
      border: 1px solid rgba(26, 26, 26, .14);
      white-space: nowrap;
      font-weight: 800;
    }}
    .badge.bad {{ color: #ffffff; background: var(--text); border-color: var(--text); }}
    .badge.warn {{ color: var(--text); background: var(--brand-soft); border-color: #ead577; }}
    .badge.good {{ color: var(--text); background: var(--brand); border-color: #e6bf00; }}
    .pre {{
      white-space: pre-wrap;
      border: 1px solid #efe6c8;
      border-radius: 20px;
      background: #fffdf7;
      padding: 16px;
      min-height: 72px;
    }}
    .notice {{
      padding: 14px 16px;
      border-radius: 20px;
      border: 1px solid #eadfbd;
      background: #fffdf7;
      color: var(--text);
      margin-bottom: 16px;
      box-shadow: 0 14px 34px rgba(26, 26, 26, .05);
    }}
    .ok {{ border-color: #e6bf00; background: var(--brand); color: var(--text); }}
    details {{
      border: 1px solid #efe6c8;
      border-radius: 22px;
      background: #fffdf7;
      padding: 12px 14px;
    }}
    summary {{
      cursor: pointer;
      font-weight: 850;
      color: var(--text);
      list-style-position: inside;
    }}
    details[open] summary {{ margin-bottom: 12px; }}
    .two {{ display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }}
    .three {{ display: grid; gap: 16px; grid-template-columns: 280px minmax(0, 1fr) 360px; align-items: start; }}
    .sticky-panel {{ position: sticky; top: 24px; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; justify-content: flex-end; }}
    .compact-table th, .compact-table td {{ padding: 9px 8px; font-size: 14px; }}
    .table-wrap {{
      width: 100%;
      overflow-x: auto;
    }}
    .table-wrap table {{
      min-width: 960px;
    }}
    .compact-table .button, .compact-table button {{
      min-height: 34px;
      padding: 7px 11px;
      font-size: 13px;
      white-space: nowrap;
      box-shadow: none;
    }}
    .compact-table .primary-action {{
      background: var(--brand);
      min-width: 104px;
    }}
    .compact-table .ghost-action {{
      background: #ffffff;
      border-color: #eadfbd;
      min-width: auto;
    }}
    .compact-table .quiet-action {{
      background: #f7f2e7;
      border-color: transparent;
      min-width: auto;
    }}
    .compact-table td:last-child {{ white-space: nowrap; }}
    .line-clamp-2 {{
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      white-space: normal;
      max-width: 240px;
    }}
    .actions-cell {{
      min-width: 300px;
    }}
    .row-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    .priority-form {{
      min-width: 118px;
    }}
    .priority-select {{
      width: 100%;
      min-height: 34px;
      border: 2px solid var(--brand-dark);
      border-radius: 999px;
      background: var(--brand);
      color: var(--text);
      padding: 0 10px;
      font-weight: 950;
      cursor: pointer;
      font-size: 13px;
    }}
    .priority-select.manual {{
      background: #1a1a1a;
      color: #ffffff;
    }}
    .compact-select {{
      width: 100%;
      min-height: 34px;
      border: 1px solid #e6d8b8;
      border-radius: 999px;
      background: #ffffff;
      color: var(--text);
      padding: 0 10px;
      font-weight: 850;
      cursor: pointer;
      font-size: 13px;
    }}
    .creator-progress-list {{
      display: grid;
      gap: 12px;
    }}
    .creator-progress-card {{
      display: grid;
      grid-template-columns: minmax(210px, 1.25fr) 120px minmax(170px, .9fr) minmax(160px, .95fr) 126px 128px minmax(210px, auto);
      gap: 14px;
      align-items: center;
      padding: 18px;
      border: 1px solid #efdfb7;
      border-radius: 24px;
      background: #ffffff;
      box-shadow: 0 14px 34px rgba(26, 26, 26, .055);
    }}
    .creator-progress-card:hover {{
      background: #fffdf3;
    }}
    .creator-progress-main,
    .creator-progress-platform,
    .creator-progress-stage,
    .creator-progress-status,
    .creator-progress-priority,
    .creator-progress-updated,
    .creator-progress-actions {{
      min-width: 0;
    }}
    .creator-progress-name {{
      display: inline-block;
      font-weight: 950;
      font-size: 16px;
      margin-bottom: 2px;
    }}
    .creator-progress-tags {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 8px;
    }}
    .creator-progress-card span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 5px;
    }}
    .creator-progress-stage strong {{
      display: block;
      font-size: 17px;
      font-weight: 950;
    }}
    .creator-progress-stage small {{
      display: block;
      color: var(--muted);
      margin-top: 3px;
      line-height: 1.35;
    }}
    .creator-progress-status > div {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .creator-progress-updated strong {{
      font-size: 13px;
      color: var(--muted);
    }}
    .creator-progress-actions {{
      display: grid;
      gap: 8px;
      justify-items: end;
    }}
    .creator-progress-actions .row-actions {{
      justify-content: flex-end;
      flex-wrap: nowrap;
    }}
    .creator-progress-actions .button,
    .creator-progress-actions button {{
      min-height: 34px;
      padding: 7px 12px;
      font-size: 13px;
      white-space: nowrap;
    }}
    .creator-progress-actions .primary-action {{
      min-width: 64px;
      background: var(--brand);
    }}
    .creator-progress-actions .quiet-action {{
      background: #f7f2e7;
      border-color: transparent;
    }}
    .inline-command {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border: 1px solid #efe6c8;
      border-radius: 22px;
      background: #fffdf7;
    }}
    .inline-command input {{ border: 0; background: transparent; }}
    .inline-command input:focus {{ outline: 0; }}
    .campaign-grid {{ grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }}
    .overview-grid {{ grid-template-columns: minmax(0, 1.1fr) minmax(280px, .9fr); align-items: stretch; }}
    .overview-card {{
      display: grid;
      align-content: end;
      min-height: 210px;
      border-radius: 32px;
      background: #1a1a1a;
      color: #ffffff;
      padding: 28px;
      box-shadow: 0 26px 70px rgba(26, 26, 26, .16);
    }}
    .overview-card h1 {{ font-size: 40px; }}
    .overview-card p {{ color: #e8dec5; max-width: 620px; margin: 10px 0 0; }}
    .overview-side {{
      background: var(--brand);
      border: 1px solid #ebc500;
      border-radius: 32px;
      padding: 26px;
      display: grid;
      align-content: space-between;
      min-height: 210px;
      box-shadow: 0 26px 70px rgba(255, 212, 0, .24);
    }}
    .overview-number {{ font-size: 56px; line-height: .95; font-weight: 950; }}
    .overview-label {{ color: var(--text); font-weight: 850; }}
    .project-grid {{ grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }}
    .campaign-card {{
      display: block;
      color: inherit;
      transition: transform .12s ease, box-shadow .12s ease;
    }}
    .campaign-card:hover {{
      transform: translateY(-2px);
      text-decoration: none;
      box-shadow: 0 26px 60px rgba(26, 26, 26, .11);
    }}
    .campaign-title {{ font-size: 16px; font-weight: 800; margin-bottom: 14px; }}
    .metric-row {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 14px; color: var(--muted); font-size: 13px; }}
    .metric-row strong {{ color: var(--text); font-size: 16px; }}
    .project-card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 18px; }}
    .project-card-name {{ font-size: 20px; font-weight: 950; line-height: 1.15; }}
    .project-card-sub {{ color: var(--muted); font-size: 13px; margin-top: 5px; }}
    .progress-line {{
      height: 12px;
      border-radius: 999px;
      background: #f4ead0;
      overflow: hidden;
      margin: 18px 0 14px;
      border: 1px solid #ecdfbd;
    }}
    .progress-line > span {{ display: block; height: 100%; background: var(--brand); border-radius: inherit; }}
    .date-group {{
      margin: 22px 0;
    }}
    .date-heading {{
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 900;
      text-transform: uppercase;
      margin: 0 0 10px;
    }}
    .date-heading::after {{
      content: "";
      height: 1px;
      background: #eadfbd;
      flex: 1;
    }}
    .creator-name-cell a {{ font-weight: 950; }}
    .creator-meta-grid {{ grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
    .mini-field {{
      border: 1px solid #efe6c8;
      background: #fffdf7;
      border-radius: 20px;
      padding: 14px;
    }}
    .mini-field span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 800; margin-bottom: 4px; }}
    .mini-field strong {{ display: block; font-size: 16px; }}
    .project-brief-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .project-info-card {{
      border: 1px solid #efe0b8;
      background: #fffdf7;
      border-radius: 22px;
      padding: 16px;
      min-height: 132px;
      box-shadow: inset 0 4px 0 var(--brand);
    }}
    .project-info-card span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      font-weight: 950;
      margin-bottom: 9px;
    }}
    .project-info-card p,
    .project-info-card ul {{
      margin: 0;
      color: var(--text);
      font-size: 15px;
      font-weight: 650;
      line-height: 1.55;
    }}
    .project-info-card ul {{
      padding-left: 18px;
      display: grid;
      gap: 5px;
    }}
    .project-info-card li {{ padding-left: 2px; }}
    .project-info-card a {{
      display: inline-block;
      max-width: 100%;
      color: var(--text);
      font-weight: 850;
      overflow-wrap: anywhere;
      text-decoration: underline;
      text-decoration-color: var(--brand);
      text-decoration-thickness: 3px;
      text-underline-offset: 3px;
    }}
    .email-log-card {{
      border: 1px solid #efe6c8;
      border-radius: 22px;
      padding: 16px;
      background: #fffdf7;
      margin-bottom: 12px;
    }}
    .email-log-card header {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 8px; }}
    .email-log-card h3 {{ margin: 0; font-size: 15px; }}
    .gmail-draft-list {{
      display: grid;
      gap: 12px;
    }}
    .gmail-draft-card {{
      display: grid;
      grid-template-columns: minmax(210px, 1.05fr) minmax(250px, 1.35fr) minmax(210px, .95fr) 150px auto;
      gap: 18px;
      align-items: center;
      padding: 18px;
      border: 1px solid #efdfb7;
      border-radius: 24px;
      background: #ffffff;
      box-shadow: 0 14px 34px rgba(26, 26, 26, .05);
    }}
    .gmail-draft-card:hover {{ background: #fffdf3; }}
    .gmail-draft-card span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 5px;
    }}
    .gmail-draft-person strong,
    .gmail-draft-subject strong {{
      display: block;
      font-size: 16px;
      font-weight: 950;
      line-height: 1.35;
    }}
    .gmail-draft-person small,
    .gmail-draft-meta small {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .gmail-draft-subject p {{
      margin: 4px 0 0;
      color: var(--muted);
      line-height: 1.35;
    }}
    .gmail-draft-id code {{
      display: inline-block;
      max-width: 100%;
      padding: 6px 9px;
      border-radius: 999px;
      background: #f7f2e7;
      font-size: 12px;
      white-space: nowrap;
    }}
    .gmail-draft-action {{
      display: flex;
      justify-content: flex-end;
    }}
    .filters {{ display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 12px; align-items: end; }}
    .section-title {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }}
    @media (max-width: 880px) {{
      .app-shell {{ padding: 12px; }}
      .top-nav {{ position: static; display: grid; padding: 12px 18px; }}
      .top-actions {{ justify-content: flex-start; flex-wrap: wrap; }}
      .category-nav {{ white-space: nowrap; }}
      .wrap {{ padding: 18px; }}
      .stats, .form-grid, .two, .three, .filters, .overview-grid, .project-brief-grid {{ grid-template-columns: 1fr; }}
      .creator-progress-card {{ grid-template-columns: 1fr; }}
      .creator-progress-actions {{ justify-items: start; }}
      .creator-progress-actions .row-actions {{ justify-content: flex-start; flex-wrap: wrap; }}
      .gmail-draft-card {{ grid-template-columns: 1fr; }}
      .gmail-draft-action {{ justify-content: flex-start; }}
      .page-head {{ display: block; }}
      .toolbar {{ justify-content: flex-start; margin-top: 12px; }}
      table {{ display: block; overflow-x: auto; white-space: nowrap; }}
    }}
  </style>
  {extra_head}
</head>
<body>
  <div class="app-shell">
    <main class="main-area">
      <header class="top-nav">
        <a class="mini-brand" href="/">
          <span class="logo">S</span>
          <span>CreatorReach AI</span>
        </a>
        <nav class="category-nav" aria-label="行业分类">
          {nav_html}
        </nav>
        <div class="top-actions">
          <a class="button secondary" href="{default_campaign_href()}">项目</a>
          <a class="button" href="/replies/new">录入回复</a>
        </div>
      </header>
      <div class="wrap">{body}</div>
    </main>
  </div>
</body>
</html>"""


def badge(value):
    cls = ""
    if value in ("ai_failed", "rejected", "invalid", "error", "failed", "completed_with_errors", "draft_failed", "P0 异常", "Gmail 异常", "有异常"):
        cls = " bad"
    elif value in ("ask_price", "ask_sample", "needs_human", "missing", "先补邮箱", "先生成邮件", "生成中", "skipped", "running", "generating", "P3 观察", "P4 补资料", "待首次触达", "首封待确认", "首次触达", "已首次触达", "首次回复", "沟通环节", "寄样品", "拍摄环节", "未创建"):
        cls = " warn"
    elif value in ("done", "sent", "posted", "interested", "draft_ready", "gmail_drafted", "drafted", "已有 Gmail 草稿", "success", "completed", "P1 优先", "P2 推荐", "项目结束", "已进草稿箱", "正常"):
        cls = " good"
    return f'<span class="badge{cls}">{esc(value or "-")}</span>'


def parse_post(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length).decode("utf-8")
    data = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {k: v[0].strip() for k, v in data.items()}


def required(data, fields):
    return [field for field in fields if not data.get(field)]


def app_base_url():
    return os.environ.get("APP_BASE_URL", f"http://127.0.0.1:{os.environ.get('PORT', '8000')}").rstrip("/")


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default


def save_setting(key, value):
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, ts, ts),
        )


def default_brand_tone():
    return get_setting("default_brand_tone", "Friendly, concise, professional")


def default_forbidden_promises():
    return get_setting(
        "default_forbidden_promises",
        "价格、佣金、库存、物流时效、样品寄送安排都需要人工确认后才能承诺。",
    )


def feishu_webhook_url():
    return os.environ.get("FEISHU_WEBHOOK_URL") or get_setting("feishu_webhook_url")


def ai_api_key():
    return os.environ.get("OPENAI_API_KEY") or get_setting("openai_api_key")


def ai_model():
    return os.environ.get("OPENAI_MODEL") or get_setting("openai_model", "gpt-4o-mini")


def ai_api_url():
    raw = os.environ.get("OPENAI_API_URL") or get_setting("openai_api_url", "https://api.openai.com/v1/chat/completions")
    raw = (raw or "").strip()
    if not raw:
        return "https://api.openai.com/v1/chat/completions"
    if raw.rstrip("/").endswith("/chat/completions"):
        return raw
    return raw.rstrip("/") + "/v1/chat/completions"


def feishu_status_text():
    if os.environ.get("FEISHU_WEBHOOK_URL"):
        return "已通过环境变量配置"
    if get_setting("feishu_webhook_url"):
        return "已通过本地设置配置"
    return "未配置"


def ai_status_text():
    if os.environ.get("OPENAI_API_KEY"):
        return f"已通过环境变量配置，模型：{ai_model()}"
    if get_setting("openai_api_key"):
        return f"已通过本地设置配置，模型：{ai_model()}"
    return "未配置，使用本地兜底分类器"


def google_client_id():
    return os.environ.get("GOOGLE_CLIENT_ID") or get_setting("google_client_id")


def google_client_secret():
    return os.environ.get("GOOGLE_CLIENT_SECRET") or get_setting("google_client_secret")


def google_redirect_uri():
    return (
        os.environ.get("GOOGLE_REDIRECT_URI")
        or get_setting("google_redirect_uri")
        or f"{app_base_url()}/auth/google/callback"
    )


def gmail_scope():
    return "https://www.googleapis.com/auth/gmail.compose"


def gmail_config_ready():
    return bool(google_client_id() and google_client_secret() and google_redirect_uri())


def get_gmail_account():
    with db() as conn:
        return conn.execute("SELECT * FROM gmail_accounts ORDER BY id DESC LIMIT 1").fetchone()


def gmail_status_text():
    account = get_gmail_account()
    if account:
        return f"已连接：{account['email'] or '未知邮箱'}"
    if gmail_config_ready():
        return "未授权，Google OAuth 配置已就绪"
    return "未授权，缺少 Google OAuth 配置"


def create_error_log(source, message, detail="", related_type=None, related_id=None):
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO error_logs
              (source, related_type, related_id, message, detail, resolved, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (source, related_type, related_id, str(message), str(detail or ""), ts, ts),
        )
        return cur.lastrowid


def mark_error_resolved(error_id):
    with db() as conn:
        conn.execute(
            "UPDATE error_logs SET resolved = 1, updated_at = ? WHERE id = ?",
            (now_iso(), error_id),
        )


def list_campaigns():
    with db() as conn:
        return conn.execute("SELECT * FROM campaigns ORDER BY updated_at DESC, id DESC").fetchall()


def load_campaign(campaign_id):
    with db() as conn:
        return conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()


def load_creator(creator_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT c.*, ca.name AS campaign_name, ca.selling_points, ca.sample_policy,
                   ca.commission_policy, ca.forbidden_promises, ca.brand_tone
            FROM creators c
            LEFT JOIN campaigns ca ON ca.id = c.campaign_id
            WHERE c.id = ?
            """,
            (creator_id,),
        ).fetchone()


def create_creator_for_campaign(
    campaign_id,
    name,
    email="",
    profile_url="",
    platform="YouTube",
    notes="",
    preferred_language="English",
    personalization_hook="",
):
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO creators
              (name, platform, profile_url, email, campaign_id, outreach_status, notes,
               preferred_language, personalization_hook, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'to_contact', ?, ?, ?, ?, ?)
            """,
            (
                name,
                platform,
                profile_url,
                email,
                campaign_id,
                notes,
                preferred_language,
                personalization_hook,
                ts,
                ts,
            ),
        )
        creator_id = cur.lastrowid
    update_creator_score(creator_id)
    return creator_id


def clamp_score(value):
    return max(0, min(100, int(round(value))))


def text_tokens(value):
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]{3,}", (value or "").lower())
        if token not in {"https", "www", "com", "the", "and", "for", "with", "you", "our", "product"}
    }


def calculate_creator_score(creator):
    product_text = " ".join(
        str(creator[key] or "")
        for key in ("campaign_name", "selling_points", "sample_policy", "commission_policy", "brand_tone")
        if key in creator.keys()
    )
    creator_text = " ".join(
        str(creator[key] or "")
        for key in ("name", "platform", "profile_url", "email", "notes")
        if key in creator.keys()
    )
    product_tokens = text_tokens(product_text)
    creator_tokens = text_tokens(creator_text)
    overlap = len(product_tokens & creator_tokens)
    platform_bonus = 12 if (creator["platform"] or "").lower() == "youtube" else 4
    profile_bonus = 8 if creator["profile_url"] else 0
    email_bonus = 25 if creator["email"] else 0
    draft_bonus = 8 if creator["outreach_draft"] else 0
    gmail_bonus = 10 if creator["gmail_draft_id"] else 0
    error_penalty = 18 if creator["outreach_error"] or creator["gmail_error"] else 0

    content_fit = clamp_score(42 + platform_bonus + profile_bonus + min(overlap * 12, 30))
    commerce = clamp_score(45 + platform_bonus + draft_bonus + gmail_bonus + min(overlap * 8, 20) - error_penalty)
    contactability = clamp_score(35 + email_bonus + profile_bonus + (10 if creator["gmail_draft_id"] else 0) - (10 if creator["gmail_error"] else 0))
    match_score = clamp_score(content_fit * 0.45 + commerce * 0.30 + contactability * 0.25)
    if creator["outreach_error"] or creator["gmail_error"]:
        priority = "P0 异常"
    elif match_score >= 80:
        priority = "P1 优先"
    elif match_score >= 65:
        priority = "P2 推荐"
    elif match_score >= 50:
        priority = "P3 观察"
    else:
        priority = "P4 补资料"
    reason = (
        f"内容匹配 {content_fit}，商业潜力 {commerce}，联系可行性 {contactability}。"
        f"{'已发现异常，需先处理错误。' if priority == 'P0 异常' else '按匹配度与可联系性排序。'}"
    )
    return {
        "match_score": match_score,
        "content_fit_score": content_fit,
        "commerce_potential_score": commerce,
        "contactability_score": contactability,
        "priority_level": priority,
        "score_reason": reason,
    }


def update_creator_score(creator_id):
    creator = load_creator(creator_id)
    if not creator:
        return None
    score = calculate_creator_score(creator)
    priority_manual = bool(creator["priority_manual"] if "priority_manual" in creator.keys() else 0)
    priority_level = creator["priority_level"] if priority_manual and creator["priority_level"] else score["priority_level"]
    score_reason = (
        f"人工设置优先级。系统参考分：匹配分 {score['match_score']}，"
        f"内容匹配 {score['content_fit_score']}，商业潜力 {score['commerce_potential_score']}，联系可行性 {score['contactability_score']}。"
        if priority_manual
        else score["score_reason"]
    )
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE creators
            SET match_score = ?, content_fit_score = ?, commerce_potential_score = ?,
                contactability_score = ?, priority_level = ?, score_reason = ?, scored_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                score["match_score"],
                score["content_fit_score"],
                score["commerce_potential_score"],
                score["contactability_score"],
                priority_level,
                score_reason,
                ts,
                ts,
                creator_id,
            ),
        )
    score["priority_level"] = priority_level
    score["score_reason"] = score_reason
    return score


def ensure_campaign_scores(campaign_id):
    with db() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                """
                SELECT id FROM creators
                WHERE campaign_id = ?
                  AND (scored_at IS NULL OR scored_at = '' OR priority_level IS NULL OR priority_level = '')
                """,
                (campaign_id,),
            ).fetchall()
        ]
    for creator_id in ids:
        update_creator_score(creator_id)


def campaign_creators(campaign_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM creators
            WHERE campaign_id = ?
            ORDER BY
              CASE priority_level
                WHEN 'P0 异常' THEN 0
                WHEN 'P1 优先' THEN 1
                WHEN 'P2 推荐' THEN 2
                WHEN 'P3 观察' THEN 3
                WHEN 'P4 补资料' THEN 4
                ELSE 5
              END,
              COALESCE(match_score, 0) DESC,
              CASE outreach_status
                WHEN 'to_contact' THEN 1
                WHEN 'draft_generated' THEN 2
                WHEN 'gmail_drafted' THEN 3
                WHEN 'sent' THEN 4
                WHEN 'replied' THEN 5
                WHEN 'done' THEN 6
                ELSE 7
              END,
              updated_at DESC
            """,
            (campaign_id,),
        ).fetchall()


def create_campaign_task(campaign_id, task_type, task_name, creators):
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO campaign_tasks
              (campaign_id, task_name, task_type, status, total_count, success_count,
               failed_count, skipped_count, note, created_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, 0, 0, 0, '', ?, ?)
            """,
            (campaign_id, task_name, task_type, len(creators), ts, ts),
        )
        task_id = cur.lastrowid
        for creator in creators:
            conn.execute(
                """
                INSERT INTO campaign_task_records
                  (task_id, campaign_id, creator_id, action, status, result, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', '', '', ?, ?)
                """,
                (task_id, campaign_id, creator["id"], task_type, ts, ts),
            )
    return task_id


def update_task_record(task_id, creator_id, status, result="", error_message=""):
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE campaign_task_records
            SET status = ?, result = ?, error_message = ?, updated_at = ?
            WHERE task_id = ? AND creator_id = ?
            """,
            (status, str(result or ""), str(error_message or ""), ts, task_id, creator_id),
        )


def finalize_campaign_task(task_id):
    ts = now_iso()
    with db() as conn:
        counts = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
              SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
              SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count
            FROM campaign_task_records
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        failed_count = counts["failed_count"] or 0
        status = "completed" if failed_count == 0 else "completed_with_errors"
        conn.execute(
            """
            UPDATE campaign_tasks
            SET status = ?, total_count = ?, success_count = ?, failed_count = ?,
                skipped_count = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                status,
                counts["total"] or 0,
                counts["success_count"] or 0,
                failed_count,
                counts["skipped_count"] or 0,
                ts,
                ts,
                task_id,
            ),
        )


def execute_campaign_action(campaign_id, action):
    creators = campaign_creators(campaign_id)
    if not creators:
        return "项目里还没有达人，先批量添加达人。"
    task_names = {
        "score": "重新计算达人优先级",
        "generate_outreach": "批量生成首封邮件",
        "create_gmail_draft": "批量创建 Gmail 草稿",
        "mark_sent": "批量标记已发送",
        "mark_done": "批量标记完成",
    }
    if action not in task_names:
        return "未识别的批量操作。"
    task_id = create_campaign_task(campaign_id, action, task_names[action], creators)
    for creator in creators:
        creator_id = creator["id"]
        try:
            if action == "score":
                score = update_creator_score(creator_id)
                update_task_record(task_id, creator_id, "success", f"匹配分 {score['match_score']} / {score['priority_level']}")
            elif action == "generate_outreach":
                if creator["outreach_draft"] and not creator["outreach_error"]:
                    update_task_record(task_id, creator_id, "skipped", "已有首封邮件草稿")
                else:
                    ok, result = generate_outreach_for_creator(creator_id)
                    update_task_record(task_id, creator_id, "success" if ok else "failed", result, "" if ok else result)
                    update_creator_score(creator_id)
            elif action == "create_gmail_draft":
                refreshed = load_creator(creator_id)
                if refreshed["gmail_draft_id"]:
                    update_task_record(task_id, creator_id, "skipped", "已有 Gmail 草稿")
                elif not refreshed["email"]:
                    update_task_record(task_id, creator_id, "skipped", "缺少达人邮箱")
                elif not refreshed["outreach_draft"]:
                    update_task_record(task_id, creator_id, "skipped", "缺少首封邮件草稿")
                else:
                    ok, result = create_creator_gmail_draft(creator_id)
                    update_task_record(task_id, creator_id, "success" if ok else "failed", result, "" if ok else result)
                    update_creator_score(creator_id)
            elif action == "mark_sent":
                if not creator["gmail_draft_id"]:
                    update_task_record(task_id, creator_id, "skipped", "未创建 Gmail 草稿")
                else:
                    with db() as conn:
                        conn.execute(
                            "UPDATE creators SET outreach_status = 'sent', updated_at = ? WHERE id = ?",
                            (now_iso(), creator_id),
                        )
                    update_task_record(task_id, creator_id, "success", "已标记发送")
                    update_creator_score(creator_id)
            elif action == "mark_done":
                if creator["outreach_status"] not in ("sent", "replied", "gmail_drafted"):
                    update_task_record(task_id, creator_id, "skipped", "未进入可完成阶段")
                else:
                    with db() as conn:
                        conn.execute(
                            "UPDATE creators SET outreach_status = 'done', updated_at = ? WHERE id = ?",
                            (now_iso(), creator_id),
                        )
                    update_task_record(task_id, creator_id, "success", "已完成")
                    update_creator_score(creator_id)
        except Exception as exc:
            create_error_log("app", f"{task_names[action]}失败", str(exc), "creator", creator_id)
            update_task_record(task_id, creator_id, "failed", "", str(exc))
    finalize_campaign_task(task_id)
    with db() as conn:
        task = conn.execute("SELECT * FROM campaign_tasks WHERE id = ?", (task_id,)).fetchone()
    return (
        f"{task_names[action]}完成：成功 {task['success_count']}，"
        f"跳过 {task['skipped_count']}，失败 {task['failed_count']}。"
    )


def execute_campaign_command(campaign_id, command):
    text = (command or "").strip()
    if not text:
        return "请先输入要执行的任务，例如：给这个项目生成首封邮件。"
    lower = text.lower()
    actions = []
    if any(word in text for word in ("评分", "优先级", "排序")):
        actions.append("score")
    if "gmail" in lower or "草稿箱" in text:
        if "生成" in text or "首封" in text or "邮件" in text:
            actions.append("generate_outreach")
        actions.append("create_gmail_draft")
    elif "首封" in text or ("生成" in text and "邮件" in text):
        actions.append("generate_outreach")
    if "已发送" in text or "标记发送" in text:
        actions.append("mark_sent")
    if "完成" in text or "标记完成" in text:
        actions.append("mark_done")
    if not actions:
        return "我还不能理解这条指令。现在支持：重新评分、生成首封邮件、创建 Gmail 草稿、标记已发送、标记完成。"
    results = []
    for action in dict.fromkeys(actions):
        results.append(execute_campaign_action(campaign_id, action))
    return " / ".join(results)


def create_message(data):
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO creators (name, platform, profile_url, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                data["creator_name"],
                data["platform"],
                data.get("profile_url"),
                data.get("internal_notes"),
                ts,
                ts,
            ),
        )
        creator_id = cur.lastrowid
        campaign_id = int(data.get("campaign_id") or 0)
        campaign = None
        if campaign_id:
            campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if campaign:
            product_name = campaign["name"]
            selling_points = campaign["selling_points"]
            sample_policy = campaign["sample_policy"]
            commission_policy = campaign["commission_policy"]
        else:
            product_name = data["product_name"]
            selling_points = data.get("selling_points")
            sample_policy = data.get("sample_policy")
            commission_policy = data.get("commission_policy")
            cur = conn.execute(
                """
                INSERT INTO campaigns
                  (name, selling_points, sample_policy, commission_policy,
                   forbidden_promises, brand_tone, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_name,
                    selling_points,
                    sample_policy,
                    commission_policy,
                    data.get("forbidden_promises"),
                    data.get("brand_tone"),
                    ts,
                    ts,
                ),
            )
            campaign_id = cur.lastrowid
        conn.execute(
            """
            UPDATE creators
            SET email = ?, campaign_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (data.get("creator_email"), campaign_id, ts, creator_id),
        )
        cur = conn.execute(
            """
            INSERT INTO messages (
              creator_id, campaign_id, platform, product_name, product_selling_points,
              sample_policy, commission_policy, raw_reply, history_context, internal_notes,
              creator_email, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new_reply', ?, ?)
            """,
            (
                creator_id,
                campaign_id,
                data["platform"],
                product_name,
                selling_points,
                sample_policy,
                commission_policy,
                data["raw_reply"],
                data.get("history_context"),
                data.get("internal_notes"),
                data.get("creator_email"),
                ts,
                ts,
            ),
        )
        return cur.lastrowid


def load_message(message_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT m.*, c.name AS creator_name, c.profile_url,
                   ca.forbidden_promises, ca.brand_tone
            FROM messages m
            JOIN creators c ON c.id = m.creator_id
            LEFT JOIN campaigns ca ON ca.id = m.campaign_id
            WHERE m.id = ?
            """,
            (message_id,),
        ).fetchone()


def words_match(text, patterns):
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def local_analyze(message):
    text = f"{message['raw_reply']}\n{message['history_context'] or ''}".strip()
    if not text or len(text) < 2:
        category = "invalid"
    elif words_match(text, [r"\bposted\b", r"\bpublished\b", r"\blive\b", r"\blink\b.*\bvideo\b", r"yesterday.*link"]):
        category = "posted"
    elif words_match(text, [r"not interested", r"\bno thanks\b", r"\bpass\b", r"sorry.*not"]):
        category = "rejected"
    elif words_match(text, [r"how much", r"\bpay\b", r"\bprice\b", r"\brate\b", r"\bcommission\b", r"\bfee\b", r"\bquote\b"]):
        category = "ask_price"
    elif words_match(text, [r"\bsample\b", r"\baddress\b", r"\bship\b", r"\bshipping\b", r"\bsend free\b", r"\bfree sample\b"]):
        category = "ask_sample"
    elif words_match(text, [r"sounds good", r"\binterested\b", r"\bdetails\b", r"\bcollab", r"let'?s do"]):
        category = "interested"
    else:
        category = "needs_human"

    risk_flags = risk_flags_for(text)
    product = message["product_name"]
    summary_map = {
        "interested": "达人表达了合作兴趣，希望了解更多合作细节。",
        "ask_price": "达人在询问报价、付款或佣金相关信息。",
        "ask_sample": "达人在询问样品或寄送相关安排。",
        "rejected": "达人明确表示暂时没有合作兴趣。",
        "posted": "达人表示内容已经发布，并可能提供了视频或内容链接。",
        "needs_human": "达人回复信息不足或语义不够明确，需要人工判断。",
        "invalid": "该回复不是有效的达人沟通内容。",
    }
    action_map = {
        "interested": "发送产品亮点和合作流程，继续确认达人意向。",
        "ask_price": "由人工确认预算、佣金或报价规则后再回复。",
        "ask_sample": "由人工确认样品政策、库存和寄送方式后再回复。",
        "rejected": "记录拒绝原因，本轮无需继续推进。",
        "posted": "打开链接检查内容，记录发布结果并进入后续复盘。",
        "needs_human": "人工阅读上下文后决定分类和回复。",
        "invalid": "人工复核是否录入错误，必要时重新录入。",
    }
    draft_map = {
        "interested": f"Hi, thanks for your interest in {product}. I can share more product details and collaboration information with you. Could you please confirm what format you prefer for the collaboration?",
        "ask_price": f"Hi, thanks for asking. The payment, commission, and pricing details need manual confirmation from our team before we can commit. I will check internally and get back to you with accurate details.",
        "ask_sample": f"Hi, thanks for your message. Sample availability, shipping arrangement, and delivery details need manual confirmation from our team before we can commit. I will check internally and reply with the confirmed information.",
        "rejected": "Hi, thanks for letting us know. We appreciate your reply and hope there may be a chance to work together in the future.",
        "posted": "Hi, thanks for sharing the update. We will review the posted content and follow up with you shortly.",
        "needs_human": "Hi, thanks for your message. I will check the details with our team and get back to you shortly.",
        "invalid": "Hi, thanks for your message. Could you please send a little more detail so we can understand your request correctly?",
    }
    return {
        "category": category,
        "summary_zh": summary_map[category],
        "next_action": action_map[category],
        "reply_draft": draft_map[category],
        "confidence": 0.86 if category not in ("needs_human", "invalid") else 0.55,
        "risk_flags": risk_flags,
    }


def risk_flags_for(text):
    flags = []
    checks = [
        ("price_commitment", [r"how much", r"\bpay\b", r"\bprice\b", r"\brate\b", r"\bcommission\b", r"\bfee\b", r"\bquote\b"]),
        ("sample_commitment", [r"\bsample\b", r"\bfree sample\b"]),
        ("shipping_commitment", [r"\bship\b", r"\bshipping\b", r"\baddress\b", r"\bdelivery\b"]),
        ("inventory_commitment", [r"\bstock\b", r"\binventory\b", r"available"]),
    ]
    for name, patterns in checks:
        if words_match(text, patterns):
            flags.append(name)
    return flags


def build_ai_prompt(message):
    return f"""
你是达人建联运营助理。请只返回一个严格 JSON 对象，不要输出 Markdown、解释、代码块或 JSON 以外的任何文字。

分类只能选择：
interested, ask_price, ask_sample, rejected, posted, needs_human, invalid

分类规则：
- 有合作兴趣：interested
- 问报价、付款、佣金：ask_price
- 要样品、问寄送：ask_sample
- 拒绝合作：rejected
- 已经发布内容：posted
- 看不懂或需要人工判断：needs_human
- 无效内容：invalid

安全要求：
- 语气遵守 Campaign 或默认品牌语气。
- 不允许编造价格。
- 不允许编造佣金。
- 不允许承诺库存。
- 不允许承诺物流时效。
- 不允许承诺一定寄样。
- 禁止承诺 forbidden_promises 中列出的内容。
- 涉及报价、付款、佣金、样品、寄送、物流、库存时，reply_draft 必须明确包含“需要人工确认”，不能直接承诺具体金额、比例、库存、寄送结果或时效。
- 如果达人问题不明确，分类为 needs_human。
- 如果是乱码、无意义内容或明显不是达人沟通内容，分类为 invalid。
- 风险标签可使用 price_commitment, shipping_commitment, sample_commitment, inventory_commitment。

输入：
达人名称：{message['creator_name']}
平台：{message['platform']}
产品名称：{message['product_name']}
产品卖点：{message['product_selling_points'] or ''}
样品政策：{message['sample_policy'] or ''}
佣金/报价规则：{message['commission_policy'] or ''}
Campaign 禁止承诺项：{message['forbidden_promises'] or ''}
Campaign 品牌语气：{message['brand_tone'] or ''}
默认禁止承诺项：{default_forbidden_promises()}
默认品牌语气：{default_brand_tone()}
达人原始回复：{message['raw_reply']}
历史上下文：{message['history_context'] or ''}

返回格式：
{{
  "category": "interested | ask_price | ask_sample | rejected | posted | needs_human | invalid",
  "summary_zh": "中文摘要",
  "next_action": "下一步建议",
  "reply_draft": "可发送回复草稿",
  "confidence": 0.82,
  "risk_flags": ["price_commitment", "shipping_commitment"]
}}
""".strip()


def call_openai(message):
    api_key = ai_api_key()
    if not api_key:
        return local_analyze(message)
    endpoint = ai_api_url()
    model = ai_model()
    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "你只输出严格 JSON。不要编造价格、佣金、库存或物流时效。",
            },
            {"role": "user", "content": build_ai_prompt(message)},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI HTTP {exc.code}: {detail[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"AI 调用失败：{exc}") from exc

    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def call_ai_json(prompt):
    api_key = ai_api_key()
    if not api_key:
        return None
    payload = {
        "model": ai_model(),
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You return strict JSON only. Do not invent pricing, commission, inventory, shipping timeline, or sample commitments.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        ai_api_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI HTTP {exc.code}: {detail[:500]}") from exc
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def creator_outreach_language(creator):
    value = (creator["preferred_language"] or "").strip().lower()
    mapping = {
        "arabic": "Arabic",
        "阿语": "Arabic",
        "arabic + english": "Arabic + English",
        "阿语 + 英语": "Arabic + English",
        "bilingual": "Arabic + English",
        "双语": "Arabic + English",
    }
    return mapping.get(value, "English")


def manual_confirmation_sentence(language):
    if language == "Arabic":
        return "تتطلب تفاصيل الأسعار أو العينات أو الشحن أو العمولة تأكيدًا يدويًا من فريقنا قبل أي التزام."
    if language == "Arabic + English":
        return (
            "تتطلب تفاصيل الأسعار أو العينات أو الشحن أو العمولة تأكيدًا يدويًا من فريقنا قبل أي التزام.\n"
            "Pricing, samples, shipping, and commission details require manual confirmation from our team before any commitment."
        )
    return "Pricing, samples, shipping, and commission details require manual confirmation from our team before any commitment."


def local_outreach_draft(creator, language):
    campaign = creator["campaign_name"] or "our project"
    hook = (creator["personalization_hook"] or "").strip()
    platform = creator["platform"] or "creator platform"
    english_opening = (
        f"We noted {hook}."
        if hook
        else f"We are reaching out because your {platform} profile may be relevant to our campaign."
    )
    arabic_opening = (
        f"اطّلعنا على {hook}."
        if hook
        else f"نتواصل لأن صفحتك على {platform} قد تكون مناسبة لحملتنا."
    )
    english = (
        f"Hi {creator['name']},\n\n"
        f"{english_opening}\n\n"
        f"We are exploring a potential collaboration around {campaign}. Would you be open to sharing the best business contact, "
        f"your availability, and your initial rates or collaboration requirements?\n\n"
        f"{manual_confirmation_sentence('English')}\n\n"
        "Best,\n"
    )
    arabic = (
        f"مرحبًا {creator['name']}،\n\n"
        f"{arabic_opening}\n\n"
        f"نرغب في بحث إمكانية التعاون ضمن مشروع {campaign}. هل يمكن مشاركة أفضل وسيلة للتواصل التجاري، "
        f"إلى جانب المواعيد المتاحة والأسعار المبدئية أو متطلبات التعاون؟\n\n"
        f"{manual_confirmation_sentence('Arabic')}\n\n"
        "مع أطيب التحيات،\n"
    )
    if language == "Arabic":
        return arabic
    if language == "Arabic + English":
        return f"{arabic}\n\n--- English ---\n\n{english}"
    return english


def generate_outreach_for_creator(creator_id):
    creator = load_creator(creator_id)
    if not creator:
        return False, "达人不存在"
    language = creator_outreach_language(creator)
    subject = (
        f"فرصة تعاون - {creator['campaign_name'] or 'مشروعنا'}"
        if language == "Arabic"
        else f"Collaboration Opportunity - {creator['campaign_name'] or 'Our Project'}"
    )
    local_body = local_outreach_draft(creator, language)
    try:
        prompt = f"""
Create a first outreach email for a creator. Return strict JSON only:
{{
  "subject": "email subject",
  "body": "email body"
}}

Creator:
- Name: {creator['name']}
- Platform: {creator['platform']}
- Email: {creator['email'] or ''}
- Profile URL: {creator['profile_url'] or ''}
- Preferred outreach language: {language}
- Verified personalization hook: {creator['personalization_hook'] or 'Not provided'}
- Internal notes: {creator['notes'] or ''}

Campaign:
- Product: {creator['campaign_name'] or ''}
- Selling points: {creator['selling_points'] or ''}
- Sample policy: {creator['sample_policy'] or ''}
- Commission/pricing policy: {creator['commission_policy'] or ''}
- Forbidden promises: {creator['forbidden_promises'] or default_forbidden_promises()}
- Brand tone: {creator['brand_tone'] or default_brand_tone()}

Rules:
- Write in the preferred outreach language. For "Arabic + English", provide Arabic first and then a matching English version.
- This is for {creator['platform'] or 'a creator platform'}, not specifically YouTube.
- Only refer to a creator post, audience, location, product preference, or previous brand partnership when it appears in the verified personalization hook or internal notes. If no hook is provided, use a truthful general outreach opening.
- Follow any "Preferred subject", "Email structure", "What we offer", or creator program details provided in the campaign.
- Use exact offer terms only when they are explicitly provided in the campaign. Do not invent price, commission, inventory, shipping timeline, or guaranteed sample delivery.
- If a price, commission, sample, shipping, or inventory term is not explicitly provided, say it needs manual confirmation before commitment.
- Do not claim the product is registered, available for shipping, medically approved, suitable for everyone, or able to treat eye conditions unless the campaign explicitly supplies verified wording.
- Keep it concise, friendly, and suitable for a one-to-one creator collaboration.
""".strip()
        result = call_ai_json(prompt)
        if result:
            subject = str(result.get("subject") or subject).strip()
            body = str(result.get("body") or local_body).strip()
        else:
            body = local_body
        risk_words = [
            "price", "commission", "sample", "shipping", "inventory", "样品", "物流", "佣金", "价格", "库存",
            "سعر", "عمولة", "عينة", "شحن", "مخزون",
        ]
        explicit_offer_terms = "fixed offer terms" in (creator["commission_policy"] or "").lower()
        if (
            any(word.lower() in body.lower() for word in risk_words)
            and "manual confirmation" not in body.lower()
            and "需要人工确认" not in body
            and "تأكيدًا يدويًا" not in body
            and not explicit_offer_terms
        ):
            body += f"\n\n{manual_confirmation_sentence(language)}"
        ts = now_iso()
        with db() as conn:
            conn.execute(
                """
                UPDATE creators
                SET outreach_subject = ?, outreach_draft = ?, outreach_generated_at = ?,
                    outreach_status = 'draft_generated', outreach_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (subject, body, ts, ts, creator_id),
            )
        return True, "已生成首封建联邮件"
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "UPDATE creators SET outreach_error = ?, updated_at = ? WHERE id = ?",
                (str(exc)[:500], now_iso(), creator_id),
            )
        create_error_log("ai", "首封建联邮件生成失败", str(exc), "creator", creator_id)
        return False, str(exc)


def start_background_creator_outreach(creator_id):
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE creators
            SET outreach_status = 'generating', outreach_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (ts, creator_id),
        )

    def worker():
        ok, _ = generate_outreach_for_creator(creator_id)
        if not ok:
            with db() as conn:
                conn.execute(
                    "UPDATE creators SET outreach_status = 'draft_failed', updated_at = ? WHERE id = ?",
                    (now_iso(), creator_id),
                )
        update_creator_score(creator_id)

    threading.Thread(target=worker, daemon=True).start()


def start_background_campaign_action(campaign_id, action=None, command=None):
    def worker():
        if command is not None:
            execute_campaign_command(campaign_id, command)
        else:
            execute_campaign_action(campaign_id, action)

    threading.Thread(target=worker, daemon=True).start()


def validate_ai_result(result, message):
    if not isinstance(result, dict):
        raise ValueError("AI 返回不是 JSON 对象")
    category = result.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"AI 分类无效：{category}")
    clean = {
        "category": category,
        "summary_zh": str(result.get("summary_zh") or "").strip(),
        "next_action": str(result.get("next_action") or "").strip(),
        "reply_draft": str(result.get("reply_draft") or "").strip(),
        "confidence": float(result.get("confidence") or 0),
        "risk_flags": result.get("risk_flags") if isinstance(result.get("risk_flags"), list) else [],
    }
    if not clean["summary_zh"] or not clean["next_action"] or not clean["reply_draft"]:
        raise ValueError("AI 返回缺少摘要、下一步建议或回复草稿")

    text = f"{message['raw_reply']}\n{message['history_context'] or ''}"
    derived_flags = risk_flags_for(text)
    merged_flags = []
    for flag in [*clean["risk_flags"], *derived_flags]:
        if isinstance(flag, str) and flag and flag not in merged_flags:
            merged_flags.append(flag)
    clean["risk_flags"] = merged_flags

    needs_manual = category in ("ask_price", "ask_sample") or any(
        flag in merged_flags
        for flag in ("price_commitment", "shipping_commitment", "sample_commitment", "inventory_commitment")
    )
    if needs_manual and "需要人工确认" not in clean["reply_draft"]:
        clean["reply_draft"] = "需要人工确认后再回复。 " + clean["reply_draft"]
    clean["confidence"] = max(0.0, min(1.0, clean["confidence"]))
    return clean


def analyze_and_save(message_id):
    if os.environ.get("AI_FORCE_FAIL") == "1":
        raise RuntimeError("AI_FORCE_FAIL 已开启，模拟 AI 失败")
    message = load_message(message_id)
    raw_result = call_openai(message)
    result = validate_ai_result(raw_result, message)
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE messages
            SET category = ?, summary_zh = ?, next_action = ?, reply_draft = ?,
                confidence = ?, risk_flags = ?, status = ?, ai_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                result["category"],
                result["summary_zh"],
                result["next_action"],
                result["reply_draft"],
                result["confidence"],
                json.dumps(result["risk_flags"], ensure_ascii=False),
                result["category"],
                ts,
                message_id,
            ),
        )


def mark_ai_failed(message_id, error):
    with db() as conn:
        conn.execute(
            "UPDATE messages SET status = 'ai_failed', ai_error = ?, updated_at = ? WHERE id = ?",
            (str(error), now_iso(), message_id),
        )
    create_error_log("ai", "AI 整理失败", str(error), "message", message_id)


def should_notify_feishu(row):
    return (row["status"] == "ai_failed") or (row["category"] in ("ask_price", "ask_sample", "needs_human"))


def format_risk_flags(row):
    if not row["risk_flags"]:
        return "无"
    try:
        flags = json.loads(row["risk_flags"])
    except json.JSONDecodeError:
        flags = [row["risk_flags"]]
    return ", ".join(str(flag) for flag in flags) if flags else "无"


def send_feishu_notification(message_id, is_retry=False):
    row = load_message(message_id)
    if not row:
        return False, "回复不存在"
    webhook = feishu_webhook_url()
    detail_url = f"{app_base_url()}/replies/{message_id}"
    status_or_category = row["category"] or row["status"]
    text = "\n".join(
        [
            "CreatorReach AI通知",
            f"达人名称：{row['creator_name']}",
            f"平台：{row['platform']}",
            f"产品：{row['product_name']}",
            f"当前状态：{row['status']}",
            f"AI 分类：{status_or_category}",
            f"中文摘要：{row['summary_zh'] or '-'}",
            f"下一步建议：{row['next_action'] or '-'}",
            f"风险标签：{format_risk_flags(row)}",
            f"详情页：{detail_url}",
        ]
    )
    if not webhook:
        error = "FEISHU_WEBHOOK_URL 未配置"
        with db() as conn:
            conn.execute(
                """
                UPDATE messages
                SET feishu_error = ?, retry_count = retry_count + ?, updated_at = ?
                WHERE id = ?
                """,
                (error, 1 if is_retry else 0, now_iso(), message_id),
            )
        create_error_log("feishu", error, text, "message", message_id)
        return False, error

    payload = {"msg_type": "text", "content": {"text": text}}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
            if body:
                try:
                    result = json.loads(body)
                    if result.get("code") not in (None, 0):
                        raise RuntimeError(body[:500])
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        error = str(exc)
        with db() as conn:
            conn.execute(
                """
                UPDATE messages
                SET feishu_error = ?, retry_count = retry_count + ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:500], 1 if is_retry else 0, now_iso(), message_id),
            )
        create_error_log("feishu", "飞书通知失败", error, "message", message_id)
        return False, error

    with db() as conn:
        conn.execute(
            """
            UPDATE messages
            SET feishu_notified_at = ?, feishu_error = NULL,
                retry_count = retry_count + ?, updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), 1 if is_retry else 0, now_iso(), message_id),
        )
    return True, ""


def notify_if_needed(message_id):
    row = load_message(message_id)
    if row and should_notify_feishu(row):
        send_feishu_notification(message_id)


def token_expiry_from_seconds(seconds):
    try:
        return (datetime.now() + timedelta(seconds=max(0, int(seconds) - 60))).replace(microsecond=0).isoformat(sep=" ")
    except (TypeError, ValueError):
        return (datetime.now() + timedelta(minutes=30)).replace(microsecond=0).isoformat(sep=" ")


def exchange_google_code(code):
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": google_client_id(),
            "client_secret": google_client_secret(),
            "redirect_uri": google_redirect_uri(),
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google OAuth token exchange failed: HTTP {exc.code}: {detail[:500]}") from exc


def fetch_gmail_profile(access_token):
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def save_gmail_account(token_data):
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("Google OAuth did not return access_token")
    existing = get_gmail_account()
    refresh_token = token_data.get("refresh_token") or (existing["refresh_token"] if existing else None)
    email_address = ""
    try:
        email_address = fetch_gmail_profile(access_token).get("emailAddress", "")
    except Exception as exc:
        create_error_log("gmail", "Gmail profile 获取失败", str(exc), "gmail_account", existing["id"] if existing else None)
    ts = now_iso()
    with db() as conn:
        if existing:
            conn.execute(
                """
                UPDATE gmail_accounts
                SET email = ?, access_token = ?, refresh_token = ?, token_expiry = ?,
                    scope = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    email_address or existing["email"],
                    access_token,
                    refresh_token,
                    token_expiry_from_seconds(token_data.get("expires_in")),
                    token_data.get("scope") or gmail_scope(),
                    ts,
                    existing["id"],
                ),
            )
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO gmail_accounts
              (email, access_token, refresh_token, token_expiry, scope, connected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_address,
                access_token,
                refresh_token,
                token_expiry_from_seconds(token_data.get("expires_in")),
                token_data.get("scope") or gmail_scope(),
                ts,
                ts,
            ),
        )
        return cur.lastrowid


def refresh_gmail_token(account):
    if not account or not account["refresh_token"]:
        raise RuntimeError("Gmail refresh_token 缺失，请重新授权 Gmail")
    payload = urllib.parse.urlencode(
        {
            "client_id": google_client_id(),
            "client_secret": google_client_secret(),
            "refresh_token": account["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        error = f"Gmail token 刷新失败: HTTP {exc.code}: {detail[:500]}"
        create_error_log("gmail", "token 刷新失败", error, "gmail_account", account["id"])
        raise RuntimeError(error) from exc
    access_token = token_data.get("access_token")
    if not access_token:
        error = "Gmail token 刷新失败：未返回 access_token"
        create_error_log("gmail", "token 刷新失败", error, "gmail_account", account["id"])
        raise RuntimeError(error)
    with db() as conn:
        conn.execute(
            """
            UPDATE gmail_accounts
            SET access_token = ?, token_expiry = ?, scope = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                access_token,
                token_expiry_from_seconds(token_data.get("expires_in")),
                token_data.get("scope") or account["scope"],
                now_iso(),
                account["id"],
            ),
        )
    return load_gmail_account(account["id"])


def load_gmail_account(account_id):
    with db() as conn:
        return conn.execute("SELECT * FROM gmail_accounts WHERE id = ?", (account_id,)).fetchone()


def ensure_gmail_access_token():
    account = get_gmail_account()
    if not account:
        raise RuntimeError("未授权 Gmail，请先在设置页连接 Gmail")
    if not account["access_token"]:
        raise RuntimeError("Gmail access_token 缺失，请重新授权 Gmail")
    if account["token_expiry"]:
        try:
            if datetime.fromisoformat(account["token_expiry"]) <= datetime.now():
                account = refresh_gmail_token(account)
        except ValueError:
            account = refresh_gmail_token(account)
    return account


def base64url_mime(to_email, subject, body):
    try:
        msg = email.message.EmailMessage()
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body or "", subtype="plain", charset="utf-8")
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    except Exception as exc:
        raise RuntimeError(f"MIME 编码失败：{exc}") from exc


def create_gmail_draft(message_id):
    row = load_message(message_id)
    if not row:
        return False, "回复不存在"
    to_email = row["creator_email"]
    body = row["final_reply"] or row["reply_draft"]
    subject = f"Collaboration Opportunity - {row['product_name']}"
    if not to_email:
        error = "缺少达人邮箱"
        mark_gmail_error(message_id, error, "gmail")
        return False, error
    if not body:
        error = "缺少最终发送稿或 AI 回复草稿"
        mark_gmail_error(message_id, error, "gmail")
        return False, error
    try:
        account = ensure_gmail_access_token()
        raw = base64url_mime(to_email, subject, body)
        payload = json.dumps({"message": {"raw": raw}}).encode("utf-8")
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            data=payload,
            headers={
                "Authorization": f"Bearer {account['access_token']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        draft_id = result.get("id")
        gmail_message_id = (result.get("message") or {}).get("id")
        ts = now_iso()
        with db() as conn:
            conn.execute(
                """
                UPDATE messages
                SET gmail_draft_id = ?, gmail_draft_created_at = ?, gmail_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (draft_id, ts, ts, message_id),
            )
            conn.execute(
                """
                INSERT INTO email_logs
                  (message_id, gmail_account_id, to_email, subject, body, gmail_draft_id,
                   gmail_message_id, status, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft_created', NULL, ?, ?)
                """,
                (message_id, account["id"], to_email, subject, body, draft_id, gmail_message_id, ts, ts),
            )
        return True, draft_id or "created"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        error = f"Gmail API 创建草稿失败: HTTP {exc.code}: {detail[:500]}"
        mark_gmail_error(message_id, error, "gmail")
        return False, error
    except Exception as exc:
        error = str(exc)
        mark_gmail_error(message_id, error, "gmail")
        return False, error


def create_creator_gmail_draft(creator_id):
    creator = load_creator(creator_id)
    if not creator:
        return False, "达人不存在"
    if not creator["email"]:
        error = "缺少达人邮箱"
        mark_creator_gmail_error(creator_id, error)
        return False, error
    if not creator["outreach_draft"]:
        ok, result = generate_outreach_for_creator(creator_id)
        if not ok:
            return False, result
        creator = load_creator(creator_id)
    subject = creator["outreach_subject"] or f"Collaboration Opportunity - {creator['campaign_name'] or 'Our Product'}"
    body = creator["outreach_draft"]
    try:
        account = ensure_gmail_access_token()
        raw = base64url_mime(creator["email"], subject, body)
        payload = json.dumps({"message": {"raw": raw}}).encode("utf-8")
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            data=payload,
            headers={
                "Authorization": f"Bearer {account['access_token']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        draft_id = result.get("id")
        gmail_message_id = (result.get("message") or {}).get("id")
        ts = now_iso()
        with db() as conn:
            conn.execute(
                """
                UPDATE creators
                SET gmail_draft_id = ?, gmail_draft_created_at = ?, gmail_error = NULL,
                    outreach_status = 'gmail_drafted', updated_at = ?
                WHERE id = ?
                """,
                (draft_id, ts, ts, creator_id),
            )
            conn.execute(
                """
                INSERT INTO email_logs
                  (message_id, creator_id, gmail_account_id, to_email, subject, body, gmail_draft_id,
                   gmail_message_id, status, error_message, created_at, updated_at)
                VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 'draft_created', NULL, ?, ?)
                """,
                (creator_id, account["id"], creator["email"], subject, body, draft_id, gmail_message_id, ts, ts),
            )
        return True, draft_id or "created"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        error = f"Gmail API 创建草稿失败: HTTP {exc.code}: {detail[:500]}"
        mark_creator_gmail_error(creator_id, error)
        return False, error
    except Exception as exc:
        error = str(exc)
        mark_creator_gmail_error(creator_id, error)
        return False, error


def mark_creator_gmail_error(creator_id, error):
    ts = now_iso()
    creator = load_creator(creator_id)
    with db() as conn:
        conn.execute(
            "UPDATE creators SET gmail_error = ?, updated_at = ? WHERE id = ?",
            (str(error)[:500], ts, creator_id),
        )
        conn.execute(
            """
            INSERT INTO email_logs
              (message_id, creator_id, gmail_account_id, to_email, subject, body, status,
               error_message, created_at, updated_at)
            VALUES (NULL, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)
            """,
            (
                creator_id,
                get_gmail_account()["id"] if get_gmail_account() else None,
                creator["email"] if creator else None,
                creator["outreach_subject"] if creator else None,
                creator["outreach_draft"] if creator else None,
                str(error),
                ts,
                ts,
            ),
        )
    create_error_log("gmail", "达人 Gmail 草稿创建失败", str(error), "creator", creator_id)


def mark_gmail_error(message_id, error, source="gmail"):
    ts = now_iso()
    row = load_message(message_id)
    with db() as conn:
        conn.execute(
            "UPDATE messages SET gmail_error = ?, updated_at = ? WHERE id = ?",
            (str(error)[:500], ts, message_id),
        )
        conn.execute(
            """
            INSERT INTO email_logs
              (message_id, gmail_account_id, to_email, subject, body, status,
               error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?)
            """,
            (
                message_id,
                get_gmail_account()["id"] if get_gmail_account() else None,
                row["creator_email"] if row else None,
                f"Collaboration Opportunity - {row['product_name']}" if row else None,
                (row["final_reply"] or row["reply_draft"]) if row else None,
                str(error),
                ts,
                ts,
            ),
        )
    create_error_log(source, "Gmail 草稿创建失败", str(error), "message", message_id)


def home_page():
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as conn:
        stats = {
            "reply_pending": conn.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE status NOT IN ('done', 'sent')
                """
            ).fetchone()[0],
            "today_creators": conn.execute(
                """
                SELECT COUNT(*) FROM creators
                WHERE date(updated_at) = ?
                  AND outreach_status IN ('draft_generated', 'gmail_drafted', 'sent', 'done')
                """,
                (today,),
            ).fetchone()[0],
            "creator_pending": conn.execute(
                """
                SELECT COUNT(*) FROM creators
                WHERE outreach_status NOT IN ('sent', 'done')
                """
            ).fetchone()[0],
            "needs_human": conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'needs_human'").fetchone()[0],
            "gmail_created": conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM messages WHERE gmail_draft_id IS NOT NULL AND gmail_draft_id != '') +
                  (SELECT COUNT(*) FROM creators WHERE gmail_draft_id IS NOT NULL AND gmail_draft_id != '')
                """
            ).fetchone()[0],
            "errors": conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM messages
                   WHERE status = 'ai_failed'
                      OR (gmail_error IS NOT NULL AND gmail_error != '')
                      OR (feishu_error IS NOT NULL AND feishu_error != '')) +
                  (SELECT COUNT(*) FROM creators
                   WHERE (gmail_error IS NOT NULL AND gmail_error != '')
                      OR (outreach_error IS NOT NULL AND outreach_error != ''))
                """
            ).fetchone()[0],
        }
        campaign_rows = conn.execute(
            """
            SELECT ca.id, ca.name,
                   ca.selling_points, ca.updated_at,
                   COUNT(DISTINCT cr.id) AS creator_count,
                   SUM(CASE WHEN cr.outreach_status NOT IN ('sent', 'done') THEN 1 ELSE 0 END) AS pending_creators,
                   SUM(CASE WHEN cr.outreach_status = 'gmail_drafted' THEN 1 ELSE 0 END) AS gmail_drafts,
                   SUM(CASE WHEN cr.outreach_status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
                   SUM(CASE WHEN cr.outreach_status = 'done' THEN 1 ELSE 0 END) AS done_count,
                   SUM(CASE WHEN (cr.gmail_error IS NOT NULL AND cr.gmail_error != '')
                             OR (cr.outreach_error IS NOT NULL AND cr.outreach_error != '')
                            THEN 1 ELSE 0 END) AS failed_count
            FROM campaigns ca
            LEFT JOIN creators cr ON cr.campaign_id = ca.id
            GROUP BY ca.id
            ORDER BY pending_creators DESC, failed_count DESC, ca.updated_at DESC
            LIMIT 20
            """
        ).fetchall()

    stat_html = "".join(
        [
            f'<div class="stat"><strong>{stats["today_creators"]}</strong><span>今日处理达人</span></div>',
            f'<div class="stat"><strong>{stats["creator_pending"]}</strong><span>达人待跟进</span></div>',
            f'<div class="stat"><strong>{stats["reply_pending"]}</strong><span>回复待处理</span></div>',
            f'<div class="stat"><strong>{stats["needs_human"]}</strong><span>需要人工处理</span></div>',
            f'<div class="stat"><strong>{stats["gmail_created"]}</strong><span>Gmail 草稿已创建</span></div>',
            f'<div class="stat"><strong>{stats["errors"]}</strong><span>异常待处理</span></div>',
        ]
    )
    project_html = "".join(
        f"""
        <a class="panel campaign-card" href="/campaigns/{row['id']}">
          <div class="project-card-head">
            <div>
              <div class="project-card-name">{esc(row['name'])}</div>
              <div class="project-card-sub">{esc((row['selling_points'] or '进入项目查看达人进度')[:72])}</div>
            </div>
            {badge('异常' if (row['failed_count'] or 0) else '进行中')}
          </div>
          <div class="progress-line"><span style="width:{min(100, int(((row['sent_count'] or 0) + (row['done_count'] or 0)) * 100 / max(1, row['creator_count'] or 0)))}%"></span></div>
          <div class="metric-row">
            <span><strong>{row['creator_count'] or 0}</strong> 达人</span>
            <span><strong>{row['pending_creators'] or 0}</strong> 待跟进</span>
            <span><strong>{row['gmail_drafts'] or 0}</strong> Gmail 草稿</span>
            <span><strong>{row['sent_count'] or 0}</strong> 已发送</span>
            <span><strong>{row['done_count'] or 0}</strong> 已完成</span>
            <span><strong>{row['failed_count'] or 0}</strong> 异常</span>
          </div>
        </a>
        """
        for row in campaign_rows
    ) or '<div class="panel muted">还没有项目。先创建一个 Campaign，填产品情况，再添加达人。</div>'
    body = f"""
    <section class="grid overview-grid">
      <div class="overview-card">
        <h1>今日工作台</h1>
        <p>这里只放总览和项目入口。先选项目，再进项目看达人进度，最后点进达人处理邮件和报价信息。</p>
      </div>
      <div class="overview-side">
        <div>
          <div class="overview-number">{stats["creator_pending"]}</div>
          <div class="overview-label">达人还需要跟进</div>
        </div>
        <p class="muted">今天已处理 {stats["today_creators"]} 位达人，Gmail 草稿累计 {stats["gmail_created"]} 封。</p>
      </div>
    </section>
    <section class="grid stats">{stat_html}</section>
    <section class="panel">
      <div class="section-title">
        <div>
          <h2>项目</h2>
          <p class="muted">每个项目对应一个产品和一批达人。点击项目进入达人进度表。</p>
        </div>
        <a class="button secondary" href="/campaigns?new=1">新建项目</a>
      </div>
      <div class="grid project-grid">{project_html}</div>
    </section>
    """
    return layout("首页", body)


def new_reply_page(error="", old=None):
    old = old or {}
    campaigns = list_campaigns()
    platform_options = "".join(
        f'<option value="{p}" {"selected" if old.get("platform") == p else ""}>{p}</option>' for p in PLATFORMS
    )
    campaign_options = '<option value="">临时输入新产品</option>' + "".join(
        f'<option value="{row["id"]}" {"selected" if str(old.get("campaign_id") or "") == str(row["id"]) else ""}>{esc(row["name"])}</option>'
        for row in campaigns
    )
    campaign_json = json.dumps([dict(row) for row in campaigns], ensure_ascii=False)
    campaign_json_script = campaign_json.replace("</", "<\\/")
    notice = f'<div class="notice">{esc(error)}</div>' if error else ""
    body = f"""
    <h1>录入达人回复</h1>
    {notice}
    <form class="panel" method="post" action="/replies">
      <div class="grid form-grid">
        <div>
          <label>达人名称 <span class="required">*</span></label>
          <input name="creator_name" value="{esc(old.get('creator_name'))}" required>
        </div>
        <div>
          <label>达人主页链接</label>
          <input name="profile_url" value="{esc(old.get('profile_url'))}" placeholder="https://">
        </div>
        <div>
          <label>达人邮箱</label>
          <input name="creator_email" value="{esc(old.get('creator_email'))}" placeholder="name@example.com">
          <p class="muted">平台为 Email 时建议填写，用于创建 Gmail 草稿。</p>
        </div>
        <div>
          <label>平台 <span class="required">*</span></label>
          <select name="platform" required>
            <option value="">请选择平台</option>
            {platform_options}
          </select>
        </div>
        <div>
          <label>选择 Campaign</label>
          <select id="campaignSelect" name="campaign_id">
            {campaign_options}
          </select>
        </div>
        <div>
          <label>产品名称 <span class="required">*</span></label>
          <input id="productName" name="product_name" value="{esc(old.get('product_name'))}">
        </div>
        <div class="full">
          <label>产品卖点</label>
          <textarea id="sellingPoints" name="selling_points">{esc(old.get('selling_points'))}</textarea>
        </div>
        <div>
          <label>样品政策</label>
          <textarea id="samplePolicy" name="sample_policy">{esc(old.get('sample_policy'))}</textarea>
        </div>
        <div>
          <label>佣金/报价规则</label>
          <textarea id="commissionPolicy" name="commission_policy">{esc(old.get('commission_policy'))}</textarea>
        </div>
        <div>
          <label>禁止承诺项</label>
          <textarea id="forbiddenPromises" name="forbidden_promises">{esc(old.get('forbidden_promises') or default_forbidden_promises())}</textarea>
        </div>
        <div>
          <label>品牌语气</label>
          <textarea id="brandTone" name="brand_tone">{esc(old.get('brand_tone') or default_brand_tone())}</textarea>
        </div>
        <div class="full">
          <label>达人原始回复 <span class="required">*</span></label>
          <textarea name="raw_reply" required>{esc(old.get('raw_reply'))}</textarea>
        </div>
        <div>
          <label>历史上下文</label>
          <textarea name="history_context">{esc(old.get('history_context'))}</textarea>
        </div>
        <div>
          <label>内部备注</label>
          <textarea name="internal_notes">{esc(old.get('internal_notes'))}</textarea>
        </div>
      </div>
      <div class="actions">
        <button type="submit">保存并 AI 整理</button>
        <a class="button secondary" href="/">返回首页</a>
      </div>
    </form>
    <script>
      const campaigns = {campaign_json_script};
      const byId = Object.fromEntries(campaigns.map(item => [String(item.id), item]));
      const campaignSelect = document.getElementById('campaignSelect');
      function fillCampaignFields(campaignId) {{
        const item = byId[campaignId];
        if (!item) return;
        document.getElementById('productName').value = item.name || '';
        document.getElementById('sellingPoints').value = item.selling_points || '';
        document.getElementById('samplePolicy').value = item.sample_policy || '';
        document.getElementById('commissionPolicy').value = item.commission_policy || '';
        document.getElementById('forbiddenPromises').value = item.forbidden_promises || {json.dumps(default_forbidden_promises())};
        document.getElementById('brandTone').value = item.brand_tone || {json.dumps(default_brand_tone())};
      }}
      campaignSelect.addEventListener('change', event => fillCampaignFields(event.target.value));
      function fillSelectedCampaign() {{
        if (campaignSelect.value) {{
          fillCampaignFields(campaignSelect.value);
        }}
      }}
      fillSelectedCampaign();
      window.addEventListener('pageshow', fillSelectedCampaign);
      setTimeout(fillSelectedCampaign, 0);
      setTimeout(fillSelectedCampaign, 150);
    </script>
    """
    return layout("录入回复", body)


def detail_page(message_id, flash=""):
    row = load_message(message_id)
    if not row:
        return layout("未找到", '<div class="notice">没有找到这条回复。</div>')
    risk_flags = []
    if row["risk_flags"]:
        try:
            risk_flags = json.loads(row["risk_flags"])
        except json.JSONDecodeError:
            risk_flags = [row["risk_flags"]]
    risk_html = " ".join(badge(flag) for flag in risk_flags) or '<span class="muted">无</span>'
    category_options = "".join(
        f'<option value="{cat}" {"selected" if row["category"] == cat else ""}>{cat}</option>' for cat in CATEGORIES
    )
    notice = ""
    if flash:
        notice = f'<div class="notice ok">{esc(flash)}</div>'
    if row["status"] == "ai_failed":
        notice += f'<div class="notice">AI 整理失败：{esc(row["ai_error"])}</div>'
    feishu_status = "未触发或未发送"
    if row["feishu_notified_at"]:
        feishu_status = f'已通知：{esc(row["feishu_notified_at"])}'
    elif row["feishu_error"]:
        feishu_status = "通知失败"
    final_reply_value = row["final_reply"] or row["reply_draft"] or ""
    body = f"""
    <div class="page-head">
      <div>
        <h1>回复详情</h1>
        <p class="page-kicker">在一个页面完成查看、编辑最终发送稿、创建 Gmail 草稿和状态处理。</p>
      </div>
      <div class="toolbar"><a class="button secondary" href="/creators">返回达人数据库</a></div>
    </div>
    {notice}
    <section class="three">
      <div class="panel">
        <h2>达人与产品</h2>
        <p><strong>达人：</strong>{esc(row['creator_name'])}</p>
        <p><strong>平台：</strong>{esc(row['platform'])}</p>
        <p><strong>达人邮箱：</strong>{esc(row['creator_email'] or '未填写')}</p>
        <p><strong>主页：</strong>{f'<a href="{esc(row["profile_url"])}" target="_blank">{esc(row["profile_url"])}</a>' if row['profile_url'] else '<span class="muted">未填写</span>'}</p>
        <p><strong>产品：</strong>{esc(row['product_name'])}</p>
        <p><strong>Campaign ID：</strong>{esc(row['campaign_id'])}</p>
        <p><strong>当前状态：</strong>{badge(row['status'])}</p>
        <p><strong>创建时间：</strong><span class="muted">{esc(row['created_at'])}</span></p>
        <p><strong>更新时间：</strong><span class="muted">{esc(row['updated_at'])}</span></p>
      </div>
      <div class="panel">
        <h2>沟通内容</h2>
        <label>达人原始回复</label>
        <div class="pre">{esc(row['raw_reply'])}</div>
        <label style="margin-top:14px;">历史上下文</label>
        <div class="pre">{esc(row['history_context'] or '未填写')}</div>
        <label style="margin-top:14px;">AI 摘要</label>
        <div class="pre">{esc(row['summary_zh'] or '暂无')}</div>
        <label style="margin-top:14px;">下一步建议</label>
        <div class="pre">{esc(row['next_action'] or '暂无')}</div>
        <label style="margin-top:14px;">AI 原始回复草稿</label>
        <div class="pre">{esc(row['reply_draft'] or '暂无')}</div>
      </div>
      <form class="panel sticky-panel" method="post" action="/replies/{row['id']}/update">
        <h2>操作面板</h2>
        <p><strong>AI 分类：</strong>{badge(row['category'] or row['status'])}</p>
        <p><strong>当前状态：</strong>{badge(row['status'])}</p>
        <p><strong>AI 置信度：</strong>{esc(row['confidence'] if row['confidence'] is not None else '-')}</p>
        <p><strong>风险标签：</strong>{risk_html}</p>
        <p><strong>飞书：</strong>{feishu_status}</p>
        <p><strong>Gmail：</strong>{esc('已创建' if row['gmail_draft_id'] else '未创建')}</p>
        <p><strong>Gmail 草稿 ID：</strong>{esc(row['gmail_draft_id'] or '无')}</p>
        <p><strong>错误：</strong>{esc(row['gmail_error'] or row['feishu_error'] or row['ai_error'] or '无')}</p>
        <label>修改 AI 分类</label>
        <select name="category">{category_options}</select>
        <label style="margin-top:12px;">达人邮箱</label>
        <input name="creator_email" value="{esc(row['creator_email'] or '')}" placeholder="name@example.com">
        <label style="margin-top:12px;">内部备注</label>
        <input name="internal_notes" value="{esc(row['internal_notes'])}">
        <label style="margin-top:12px;">最终发送稿</label>
        <textarea id="finalReply" name="final_reply" style="min-height:220px;">{esc(final_reply_value)}</textarea>
        <div class="actions">
          <button type="submit">保存修改</button>
          <button class="secondary" type="submit" name="action" value="create_gmail_draft">创建 Gmail 草稿</button>
          <button class="secondary" type="button" onclick="copyFinalReply()">复制最终发送稿</button>
          <button class="secondary" type="submit" name="action" value="sent">标记已发送</button>
          <button class="secondary" type="submit" name="action" value="done">标记完成</button>
          <button class="secondary" type="submit" name="action" value="needs_human">需要人工处理</button>
          <button class="secondary" type="submit" name="action" value="resend_feishu">重发飞书</button>
        </div>
        <p id="copyTip" class="muted"></p>
      </form>
    </section>
    <script>
      async function copyFinalReply() {{
        const el = document.getElementById('finalReply');
        await navigator.clipboard.writeText(el.value || '');
        document.getElementById('copyTip').textContent = '已复制最终发送稿';
      }}
    </script>
    """
    return layout("回复详情", body)


def option_html(value, label, current):
    return f'<option value="{esc(value)}" {"selected" if str(current or "") == str(value) else ""}>{esc(label)}</option>'


def followups_page(query):
    campaign_id = query.get("campaign_id", [""])[0]
    platform = query.get("platform", [""])[0]
    category = query.get("category", [""])[0]
    status = query.get("status", [""])[0]
    gmail_status = query.get("gmail_status", [""])[0]
    has_error = query.get("has_error", [""])[0]
    where = []
    params = []
    if campaign_id.isdigit():
        where.append("m.campaign_id = ?")
        params.append(int(campaign_id))
    if platform in PLATFORMS:
        where.append("m.platform = ?")
        params.append(platform)
    if category in CATEGORIES:
        where.append("m.category = ?")
        params.append(category)
    if status:
        where.append("m.status = ?")
        params.append(status)
    if gmail_status == "drafted":
        where.append("m.gmail_draft_id IS NOT NULL AND m.gmail_draft_id != ''")
    elif gmail_status == "pending":
        where.append("(m.gmail_draft_id IS NULL OR m.gmail_draft_id = '')")
    elif gmail_status == "error":
        where.append("m.gmail_error IS NOT NULL AND m.gmail_error != ''")
    if has_error == "yes":
        where.append("(m.ai_error IS NOT NULL OR m.feishu_error IS NOT NULL OR m.gmail_error IS NOT NULL)")
    elif has_error == "no":
        where.append("(m.ai_error IS NULL AND m.feishu_error IS NULL AND m.gmail_error IS NULL)")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    with db() as conn:
        campaigns = conn.execute("SELECT id, name FROM campaigns ORDER BY name").fetchall()
        rows = conn.execute(
            f"""
            SELECT m.id, m.creator_email, m.platform, m.product_name, m.summary_zh, m.reply_draft,
                   m.category, m.status, m.gmail_draft_id, m.gmail_error, m.feishu_error,
                   m.updated_at, c.name AS creator_name
            FROM messages m
            JOIN creators c ON c.id = m.creator_id
            {where_sql}
            ORDER BY m.updated_at DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
    campaign_options = option_html("", "全部 Campaign", campaign_id) + "".join(
        option_html(row["id"], row["name"], campaign_id) for row in campaigns
    )
    platform_options = option_html("", "全部平台", platform) + "".join(option_html(p, p, platform) for p in PLATFORMS)
    category_options = option_html("", "全部分类", category) + "".join(option_html(c, c, category) for c in CATEGORIES)
    statuses = ["new_reply", "interested", "ask_price", "ask_sample", "rejected", "posted", "needs_human", "invalid", "ai_failed", "sent", "done"]
    status_options = option_html("", "全部状态", status) + "".join(option_html(s, s, status) for s in statuses)
    gmail_options = "".join(
        [
            option_html("", "全部 Gmail 状态", gmail_status),
            option_html("drafted", "已创建草稿", gmail_status),
            option_html("pending", "未创建草稿", gmail_status),
            option_html("error", "Gmail 错误", gmail_status),
        ]
    )
    error_options = "".join(
        [
            option_html("", "全部错误状态", has_error),
            option_html("yes", "有错误", has_error),
            option_html("no", "无错误", has_error),
        ]
    )
    row_html = "".join(
        f"""
        <tr>
          <td><a href="/replies/{row['id']}">{esc(row['creator_name'])}</a></td>
          <td>{esc(row['creator_email'] or '-')}</td>
          <td>{esc(row['platform'])}</td>
          <td>{esc(row['product_name'])}</td>
          <td>{esc(row['summary_zh'] or '-')}</td>
          <td>{badge('ready' if row['reply_draft'] else 'missing')}</td>
          <td>{badge('error' if row['gmail_error'] else ('drafted' if row['gmail_draft_id'] else 'pending'))}</td>
          <td>{badge('error' if row['feishu_error'] else 'ok')}</td>
          <td class="muted">{esc(row['updated_at'])}</td>
          <td><a class="button secondary" href="/replies/{row['id']}">处理</a></td>
        </tr>
        """
        for row in rows
    ) or '<tr><td colspan="10" class="muted">没有符合筛选条件的记录。</td></tr>'
    body = f"""
    <div class="page-head">
      <div>
        <h1>达人跟进中心</h1>
        <p class="page-kicker">集中查看所有达人沟通、AI 建议、Gmail 草稿和飞书通知状态。</p>
      </div>
      <div class="toolbar"><a class="button" href="/replies/new">录入达人回复</a></div>
    </div>
    <form class="panel filters" method="get" action="/followups">
      <div><label>Campaign</label><select name="campaign_id">{campaign_options}</select></div>
      <div><label>平台</label><select name="platform">{platform_options}</select></div>
      <div><label>AI 分类</label><select name="category">{category_options}</select></div>
      <div><label>当前状态</label><select name="status">{status_options}</select></div>
      <div><label>Gmail 状态</label><select name="gmail_status">{gmail_options}</select></div>
      <div><label>错误</label><select name="has_error">{error_options}</select></div>
      <div class="actions"><button type="submit">筛选</button><a class="button secondary" href="/followups">重置</a></div>
    </form>
    <section class="panel">
      <h2>跟进列表</h2>
      <div class="table-wrap">
        <table class="compact-table">
          <thead>
            <tr><th>达人</th><th>邮箱</th><th>平台</th><th>产品</th><th>最新回复摘要</th><th>AI 草稿</th><th>Gmail</th><th>飞书</th><th>更新时间</th><th>操作</th></tr>
          </thead>
          <tbody>{row_html}</tbody>
        </table>
      </div>
    </section>
    """
    return layout("达人跟进中心", body)


def gmail_page():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT el.*,
                   COALESCE(m.product_name, ca.name) AS product_name,
                   COALESCE(cm.name, cc.name) AS creator_name
            FROM email_logs el
            LEFT JOIN messages m ON m.id = el.message_id
            LEFT JOIN creators cm ON cm.id = m.creator_id
            LEFT JOIN creators cc ON cc.id = el.creator_id
            LEFT JOIN campaigns ca ON ca.id = cc.campaign_id
            ORDER BY el.created_at DESC
            LIMIT 200
            """
        ).fetchall()
    total = len(rows)
    created_count = sum(1 for row in rows if row["gmail_draft_id"])
    failed_count = sum(1 for row in rows if row["error_message"])
    card_html = "".join(
        f"""
        <article class="gmail-draft-card">
          <div class="gmail-draft-person">
            <span>达人 / 收件邮箱</span>
            <strong>{esc(row['creator_name'] or '-')}</strong>
            <small>{esc(row['to_email'] or '未记录邮箱')}</small>
          </div>
          <div class="gmail-draft-subject">
            <span>邮件标题</span>
            <strong>{esc(row['subject'] or '-')}</strong>
            <p>{esc(row['product_name'] or '-')}</p>
          </div>
          <div class="gmail-draft-id">
            <span>草稿状态</span>
            {badge(email_log_status_label(row['status']))}
            <div style="margin-top:8px;"><code title="{esc(row['gmail_draft_id'] or '-')}">{esc(short_id(row['gmail_draft_id']))}</code></div>
          </div>
          <div class="gmail-draft-meta">
            <span>创建时间</span>
            <strong>{esc((row['created_at'] or '-')[:16])}</strong>
            <small>{'错误：' + esc(row['error_message']) if row['error_message'] else '无错误'}</small>
          </div>
          <div class="gmail-draft-action">
            {f'<a class="button secondary" href="/replies/{row["message_id"]}">查看回复</a>' if row['message_id'] else (f'<a class="button secondary" href="/creators/{row["creator_id"]}">查看达人</a>' if row['creator_id'] else '<span class="muted">无入口</span>')}
          </div>
        </article>
        """
        for row in rows
    ) or '<div class="notice">还没有 Gmail 草稿记录。</div>'
    body = f"""
    <div class="page-head">
      <div>
        <h1>Gmail 草稿</h1>
        <p class="page-kicker">按达人查看已经写入 Gmail 草稿箱的邮件，失败记录会保留在这里方便排查。</p>
      </div>
      <div class="toolbar"><a class="button secondary" href="/settings">Gmail 设置</a></div>
    </div>
    <section class="grid stats">
      <div class="stat"><strong>{total}</strong><span>草稿记录</span></div>
      <div class="stat"><strong>{created_count}</strong><span>已进草稿箱</span></div>
      <div class="stat"><strong>{failed_count}</strong><span>创建失败</span></div>
    </section>
    <section class="panel">
      <div class="section-title">
        <h2>最近草稿</h2>
        <span class="muted">最多显示最近 200 条</span>
      </div>
      <div class="gmail-draft-list">{card_html}</div>
    </section>
    """
    return layout("Gmail 草稿", body)


SCREENING_COUNTRIES = {"SA": "沙特", "AE": "阿联酋", "OTHER": "其他", "": "未核验"}
SCREENING_STATUSES = {
    "unverified": "待核验",
    "verified": "已核验",
    "excluded": "不适配",
}


def parse_followers(value):
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdecimal() or len(value) > 9:
        raise ValueError("粉丝数必须是非负整数")
    return int(value)


def screening_page(query):
    country = query.get("country", ["SA"])[0]
    platform = query.get("platform", [""])[0]
    minimum = query.get("min", ["100"])[0]
    maximum = query.get("max", ["1000"])[0]
    tag = (query.get("tag", [""])[0] or "").strip()[:80]
    verification = query.get("verification", ["verified"])[0]
    no_conflict = query.get("no_conflict", ["1"])[0] == "1"
    try:
        min_followers = parse_followers(minimum)
        max_followers = parse_followers(maximum)
        if min_followers is not None and max_followers is not None and min_followers > max_followers:
            raise ValueError("最低粉丝数不能超过最高粉丝数")
        error = ""
    except ValueError as exc:
        min_followers = max_followers = None
        error = str(exc)
    where, params = [], []
    if country in SCREENING_COUNTRIES:
        if country:
            where.append("c.country = ?")
            params.append(country)
    if platform in PLATFORMS:
        where.append("c.platform = ?")
        params.append(platform)
    if min_followers is not None:
        where.append("c.followers >= ?")
        params.append(min_followers)
    if max_followers is not None:
        where.append("c.followers <= ?")
        params.append(max_followers)
    if tag:
        where.append("c.content_tags LIKE ?")
        params.append(f"%{tag}%")
    if verification in SCREENING_STATUSES:
        where.append("c.screening_status = ?")
        params.append(verification)
    if no_conflict:
        where.append("COALESCE(c.competitor_conflict, 0) = 0")
    clause = "WHERE " + " AND ".join(where) if where else ""
    with db() as conn:
        rows = conn.execute(
            f"""SELECT c.*, ca.name AS campaign_name FROM creators c
            LEFT JOIN campaigns ca ON ca.id = c.campaign_id
            {clause} ORDER BY c.followers ASC, c.updated_at DESC LIMIT 300""",
            params,
        ).fetchall()
    campaigns = list_campaigns()
    campaign_options = "".join(option_html(row["id"], row["name"], "") for row in campaigns)
    countries = option_html("ALL", "全部国家", country) + "".join(
        option_html(code, name, country) for code, name in SCREENING_COUNTRIES.items()
    )
    platforms = option_html("", "全部平台", platform) + "".join(
        option_html(p, p, platform) for p in PLATFORMS
    )
    statuses = option_html("ALL", "全部状态", verification) + "".join(
        option_html(code, name, verification) for code, name in SCREENING_STATUSES.items()
    )
    results = "".join(
        f"""<tr>
        <td><a href="/creators/{row['id']}">{esc(row['name'])}</a></td>
        <td>{esc(row['platform'])}</td>
        <td>{esc(SCREENING_COUNTRIES.get(row['country'] or '', '未核验'))} / {esc(row['city'] or '-')}</td>
        <td>{esc(row['followers'] if row['followers'] is not None else '未核验')}</td>
        <td>{esc(row['content_tags'] or '-')}</td>
        <td>{esc(SCREENING_STATUSES.get(row['screening_status'], '待核验'))}</td>
        <td>{'有冲突' if row['competitor_conflict'] else '未标记'}</td>
        <td>{f'<a href="{esc(row["evidence_url"])}" target="_blank" rel="noopener noreferrer">证据</a>' if row['evidence_url'] and row['evidence_url'].startswith(('https://', 'http://')) else '-'}</td>
        <td>{esc(row['observed_at'] or '-')}</td>
        </tr>"""
        for row in rows
    ) or '<tr><td colspan="9">无匹配记录。先在下方录入候选，核验后再筛选。</td></tr>'
    flash = query.get("flash", [""])[0]
    body = f"""
    <h1>达人筛选</h1>
    <p class="page-kicker">基于已录入资料筛选；默认沙特、100–1,000 粉、已核验、排除竞品冲突。此页不抓取 TikTok，也不自动邀约。</p>
    {f'<div class="notice">{esc(error)}</div>' if error else ''}
    {f'<div class="notice ok">{esc(flash)}</div>' if flash else ''}
    <form class="panel filters" method="get" action="/screening">
      <div><label>国家</label><select name="country">{countries}</select></div>
      <div><label>平台</label><select name="platform">{platforms}</select></div>
      <div><label>最低粉丝数</label><input type="number" name="min" min="0" value="{esc(minimum)}"></div>
      <div><label>最高粉丝数</label><input type="number" name="max" min="0" value="{esc(maximum)}"></div>
      <div><label>内容标签</label><input name="tag" value="{esc(tag)}" placeholder="美妆 / 眼妆 / makeup"></div>
      <div><label>核验状态</label><select name="verification">{statuses}</select></div>
      <div><label><input type="checkbox" name="no_conflict" value="1" {'checked' if no_conflict else ''}> 排除已标记竞品冲突</label></div>
      <div class="actions"><button type="submit">筛选</button><a class="button secondary" href="/screening">重置</a></div>
    </form>
    <section class="panel">
      <h2>筛选结果：{len(rows)} 条</h2>
      <div class="table-wrap"><table class="compact-table">
        <thead><tr><th>达人</th><th>平台</th><th>国家/城市</th><th>粉丝</th><th>标签</th><th>核验</th><th>竞品</th><th>来源</th><th>观察日期</th></tr></thead>
        <tbody>{results}</tbody>
      </table></div>
    </section>
    <form class="panel" method="post" action="/screening/candidates">
      <h2>录入一位候选达人</h2>
      <p class="muted">没有核验资料时保留“待核验”；账号名称或阿语内容不等于沙特受众。</p>
      <div class="grid form-grid">
        <div><label>所属项目</label><select name="campaign_id">{campaign_options}</select></div>
        <div><label>账号/名称</label><input name="name" required></div>
        <div><label>主页 URL</label><input name="profile_url" type="url" placeholder="https://www.tiktok.com/@..."></div>
        <div><label>平台</label><select name="platform">{"".join(option_html(p, p, "TikTok") for p in PLATFORMS)}</select></div>
        <div><label>国家（需证据）</label><select name="country">{"".join(option_html(code, name, "") for code, name in SCREENING_COUNTRIES.items())}</select></div>
        <div><label>城市</label><input name="city"></div>
        <div><label>粉丝数</label><input type="number" name="followers" min="0"></div>
        <div><label>标签</label><input name="content_tags" placeholder="美妆, 眼妆"></div>
        <div><label>核验状态</label><select name="screening_status">{"".join(option_html(code, name, "unverified") for code, name in SCREENING_STATUSES.items())}</select></div>
        <div><label>观察日期</label><input type="date" name="observed_at"></div>
        <div class="full"><label>证据 URL</label><input type="url" name="evidence_url" placeholder="https://..."></div>
        <div class="full"><label>证据说明/备注</label><textarea name="notes"></textarea></div>
        <div><label><input type="checkbox" name="competitor_conflict" value="1"> 已发现直接竞品冲突</label></div>
      </div>
      <div class="actions"><button type="submit">保存候选</button></div>
    </form>
    """
    return layout("达人筛选", body)


def creators_page(query):
    q = (query.get("q", [""])[0] or "").strip()
    platform = query.get("platform", [""])[0]
    where = []
    params = []
    if q:
        where.append("(c.name LIKE ? OR c.profile_url LIKE ? OR c.email LIKE ? OR ca.name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if platform in PLATFORMS:
        where.append("c.platform = ?")
        params.append(platform)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.name, c.platform, c.profile_url, c.updated_at, c.email,
                   c.outreach_status, c.gmail_draft_id, c.gmail_error, c.outreach_error,
                   ca.name AS campaign_name,
                   COUNT(m.id) AS message_count,
                   COUNT(DISTINCT m.campaign_id) AS campaign_count,
                   MAX(m.updated_at) AS latest_at,
                   SUM(CASE WHEN m.status NOT IN ('done', 'sent') THEN 1 ELSE 0 END) AS open_count,
                   SUM(CASE WHEN m.gmail_draft_id IS NOT NULL AND m.gmail_draft_id != '' THEN 1 ELSE 0 END) AS gmail_drafts,
                   SUM(CASE WHEN m.ai_error IS NOT NULL OR m.feishu_error IS NOT NULL OR m.gmail_error IS NOT NULL THEN 1 ELSE 0 END) AS error_count,
                   (
                     SELECT id FROM messages mx
                     WHERE mx.creator_id = c.id
                     ORDER BY mx.updated_at DESC
                     LIMIT 1
                   ) AS latest_message_id
            FROM creators c
            LEFT JOIN campaigns ca ON ca.id = c.campaign_id
            LEFT JOIN messages m ON m.creator_id = c.id
            {where_sql}
            GROUP BY c.id
            ORDER BY latest_at DESC, c.updated_at DESC
            LIMIT 300
            """,
            params,
        ).fetchall()
    platform_options = option_html("", "全部平台", platform) + "".join(option_html(p, p, platform) for p in PLATFORMS)
    row_html = "".join(
        f"""
        <tr>
          <td>{esc(row['name'])}</td>
          <td>{esc(row['email'] or '-')}</td>
          <td>{esc(row['platform'])}</td>
          <td>{f'<a href="{esc(row["profile_url"])}" target="_blank">主页</a>' if row['profile_url'] else '-'}</td>
          <td>{esc(row['message_count'] or 0)}</td>
          <td>{esc(row['campaign_name'] or row['campaign_count'] or 0)}</td>
          <td>{badge(lifecycle_stage(row['outreach_status'])[0])}</td>
          <td>{badge(gmail_state_label(row))}</td>
          <td>{badge(error_state_label(row))}</td>
          <td class="muted">{esc(row['latest_at'] or row['updated_at'] or '-')}</td>
          <td><a class="button secondary" href="/creators/{row['id']}">进入达人</a></td>
        </tr>
        """
        for row in rows
    ) or '<tr><td colspan="11" class="muted">没有找到达人记录。</td></tr>'
    body = f"""
    <div class="page-head">
      <div>
        <h1>达人数据库</h1>
        <p class="page-kicker">沉淀所有达人记录，快速查看沟通数量、邮箱、项目和异常状态。</p>
      </div>
      <div class="toolbar"><a class="button" href="/replies/new">录入达人回复</a></div>
    </div>
    <form class="panel filters" method="get" action="/creators">
      <div>
        <label>搜索</label>
        <input name="q" value="{esc(q)}" placeholder="达人名称 / 邮箱 / 产品">
      </div>
      <div>
        <label>平台</label>
        <select name="platform">{platform_options}</select>
      </div>
      <div class="actions">
        <button type="submit">筛选</button>
        <a class="button secondary" href="/creators">重置</a>
      </div>
    </form>
    <section class="panel">
      <h2>达人记录</h2>
      <div class="table-wrap">
        <table class="compact-table">
          <thead>
            <tr>
              <th>达人</th><th>邮箱</th><th>平台</th><th>主页</th><th>回复数</th><th>Campaign 数</th>
              <th>跟进状态</th><th>Gmail 草稿</th><th>异常</th><th>最近更新</th><th>操作</th>
            </tr>
          </thead>
          <tbody>{row_html}</tbody>
        </table>
      </div>
    </section>
    """
    return layout("达人数据库", body)


def campaign_detail_page(campaign_id, flash=""):
    campaign = load_campaign(campaign_id)
    if not campaign:
        return layout("未找到", '<div class="notice">没有找到这个 Campaign。</div>')
    ensure_campaign_scores(campaign_id)
    with db() as conn:
        tasks = conn.execute(
            """
            SELECT * FROM campaign_tasks
            WHERE campaign_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 8
            """,
            (campaign_id,),
        ).fetchall()
        task_records = conn.execute(
            """
            SELECT r.*, t.task_name, c.name AS creator_name
            FROM campaign_task_records r
            JOIN campaign_tasks t ON t.id = r.task_id
            JOIN creators c ON c.id = r.creator_id
            WHERE r.campaign_id = ?
            ORDER BY r.updated_at DESC, r.id DESC
            LIMIT 16
            """,
            (campaign_id,),
        ).fetchall()
    creators = campaign_creators(campaign_id)
    metrics = {
        "total": len(creators),
        "missing_email": sum(1 for row in creators if not row["email"]),
        "draft_ready": sum(1 for row in creators if row["outreach_draft"]),
        "gmail_drafted": sum(1 for row in creators if row["gmail_draft_id"]),
        "sent": sum(1 for row in creators if row["outreach_status"] == "sent"),
        "done": sum(1 for row in creators if row["outreach_status"] == "done"),
        "errors": sum(1 for row in creators if row["outreach_error"] or row["gmail_error"]),
    }
    stats_html = "".join(
        f'<div class="stat"><strong>{value}</strong><span>{label}</span></div>'
        for label, value in [
            ("项目达人", metrics["total"]),
            ("缺邮箱", metrics["missing_email"]),
            ("首封草稿", metrics["draft_ready"]),
            ("Gmail 草稿", metrics["gmail_drafted"]),
            ("已发送", metrics["sent"]),
            ("已完成", metrics["done"]),
            ("异常", metrics["errors"]),
        ]
    )
    task_rows_html = "".join(
        f"""
        <tr>
          <td>{esc(row['updated_at'])}</td>
          <td>{esc(row['task_name'])}</td>
          <td>{badge(row['status'])}</td>
          <td>{esc(row['success_count'])}</td>
          <td>{esc(row['skipped_count'])}</td>
          <td>{esc(row['failed_count'])}</td>
          <td>{esc(row['total_count'])}</td>
        </tr>
        """
        for row in tasks
    ) or '<tr><td colspan="7" class="muted">还没有批量任务。</td></tr>'
    record_rows_html = "".join(
        f"""
        <tr>
          <td>{esc(row['updated_at'])}</td>
          <td>{esc(row['creator_name'])}</td>
          <td>{esc(row['task_name'])}</td>
          <td>{badge(row['status'])}</td>
          <td>{esc(row['result'] or row['error_message'] or '-')}</td>
        </tr>
        """
        for row in task_records
    ) or '<tr><td colspan="5" class="muted">暂无任务明细。</td></tr>'
    notice_cls = "notice" if any(word in flash for word in ("失败", "错误", "超时", "timeout", "timed out")) else "notice ok"
    notice = f'<div class="{notice_cls}">{esc(flash)}</div>' if flash else ""
    auto_refresh = any(row["outreach_status"] == "generating" for row in creators) or any(row["status"] == "running" for row in tasks)
    auto_refresh_html = (
        "<script>setTimeout(() => window.location.reload(), 5000);</script>"
        if auto_refresh
        else ""
    )
    row_parts = []
    for row in creators:
        outreach_error = row["outreach_error"] if "outreach_error" in row.keys() else ""
        if row["gmail_draft_id"]:
            gmail_action = badge("已有 Gmail 草稿")
        elif not row["email"]:
            gmail_action = badge("先补邮箱")
        elif not row["outreach_draft"]:
            gmail_action = badge("先生成邮件")
        else:
            gmail_action = '<button class="ghost-action" type="submit" name="action" value="create_gmail_draft">Gmail 草稿</button>'
        if row["outreach_status"] == "generating":
            outreach_badge = badge("generating")
            generate_action = badge("生成中")
        else:
            outreach_badge = badge("error" if outreach_error else ("draft_ready" if row["outreach_draft"] else "missing"))
            generate_label = "重写草稿" if row["outreach_draft"] else "生成草稿"
            generate_action = f'<button class="primary-action" type="submit" name="action" value="generate_outreach">{generate_label}</button>'
        outreach_error_html = (
            f'<div class="notice" style="margin-top:10px;">首封邮件生成失败：{esc(outreach_error)}</div>'
            if outreach_error
            else ""
        )
        gmail_error_html = (
            f'<div class="notice" style="margin-top:10px;">Gmail 草稿创建失败：{esc(row["gmail_error"])}</div>'
            if row["gmail_error"]
            else ""
        )
        row_parts.append(
            f"""
        <tr>
          <td>{esc(row['name'])}</td>
          <td title="{esc(row['score_reason'] or '')}">{badge(row['priority_level'] or '未评分')}</td>
          <td>{esc(row['match_score'] or 0)}</td>
          <td>{esc(row['email'] or '-')}</td>
          <td>{esc(row['platform'])}</td>
          <td>{f'<a href="{esc(row["profile_url"])}" target="_blank">主页</a>' if row['profile_url'] else '-'}</td>
          <td>{badge(row['outreach_status'] or 'to_contact')}</td>
          <td>{outreach_badge}</td>
          <td>{badge('error' if row['gmail_error'] else ('drafted' if row['gmail_draft_id'] else 'pending'))}</td>
          <td>{esc(row['gmail_draft_id'] or '-')}</td>
          <td class="actions-cell">
            <form class="row-actions" method="post" action="/creators/{row['id']}/action">
              {generate_action}
              {gmail_action}
              <button class="quiet-action" type="submit" name="action" value="sent">已发送</button>
              <button class="quiet-action" type="submit" name="action" value="done">完成</button>
            </form>
          </td>
        </tr>
        <tr>
          <td colspan="11">
            <details>
              <summary>查看/编辑首封邮件草稿</summary>
              <form method="post" action="/creators/{row['id']}/action">
                <div class="grid form-grid">
                  <div>
                    <label>达人邮箱</label>
                    <input name="email" value="{esc(row['email'] or '')}" placeholder="name@example.com">
                  </div>
                  <div>
                    <label>主页链接</label>
                    <input name="profile_url" value="{esc(row['profile_url'] or '')}" placeholder="https://www.youtube.com/@...">
                  </div>
                </div>
                <label>Subject</label>
                <input name="outreach_subject" value="{esc(row['outreach_subject'] or f'Collaboration Opportunity - {campaign["name"]}')}">
                <label style="margin-top:10px;">Body</label>
                <textarea name="outreach_draft" style="min-height:160px;">{esc(row['outreach_draft'] or '')}</textarea>
                {outreach_error_html}
                {gmail_error_html}
                <div class="actions">
                  <button type="submit" name="action" value="save_outreach">保存草稿</button>
                </div>
              </form>
            </details>
          </td>
        </tr>
        """
        )
    rows_html = "".join(row_parts) or '<tr><td colspan="11" class="muted">这个 Campaign 还没有达人。先在下方批量添加。</td></tr>'
    body = f"""
    <div class="page-head">
      <div>
        <h1>{esc(campaign['name'])}</h1>
        <p class="page-kicker">项目任务中心：按优先级批量生成首封邮件、创建 Gmail 草稿，并沉淀每次执行记录。</p>
      </div>
      <div class="toolbar">
        <a class="button secondary" href="{default_campaign_href()}">返回项目</a>
      </div>
    </div>
    {notice}
    <section class="grid stats">{stats_html}</section>
    <section class="panel">
      <h2>项目任务中心</h2>
      <p class="muted">批量创建 Gmail 草稿只会处理“有邮箱 + 已有首封邮件 + 未创建 Gmail 草稿”的达人；不会自动发送邮件。</p>
      <form class="actions" method="post" action="/campaigns/{campaign_id}/bulk">
        <button class="secondary" type="submit" name="action" value="score">重新计算优先级</button>
        <button type="submit" name="action" value="generate_outreach">批量生成首封邮件</button>
        <button class="secondary" type="submit" name="action" value="create_gmail_draft">批量创建 Gmail 草稿</button>
        <button class="secondary" type="submit" name="action" value="mark_sent">批量标记已发送</button>
        <button class="secondary" type="submit" name="action" value="mark_done">批量标记完成</button>
      </form>
      <form method="post" action="/campaigns/{campaign_id}/bulk" style="margin-top:16px;">
        <label>直接下指令</label>
        <div class="inline-command">
          <input name="command" placeholder="例如：给这个项目生成首封邮件并创建 Gmail 草稿">
          <button type="submit" name="action" value="command">执行</button>
        </div>
        <p class="muted">支持：重新评分、生成首封邮件、创建 Gmail 草稿、标记已发送、标记完成。</p>
      </form>
    </section>
    <section class="panel">
      <h2>最近任务</h2>
      <div class="table-wrap">
        <table class="compact-table">
          <thead>
            <tr><th>更新时间</th><th>任务</th><th>状态</th><th>成功</th><th>跳过</th><th>失败</th><th>总数</th></tr>
          </thead>
          <tbody>{task_rows_html}</tbody>
        </table>
      </div>
      <details style="margin-top:12px;">
        <summary>查看最近任务明细</summary>
        <div class="table-wrap" style="margin-top:10px;">
          <table class="compact-table">
            <thead>
              <tr><th>时间</th><th>达人</th><th>任务</th><th>状态</th><th>结果</th></tr>
            </thead>
            <tbody>{record_rows_html}</tbody>
          </table>
        </div>
      </details>
    </section>
    <section class="panel">
      <h2>新增达人</h2>
      <form method="post" action="/campaigns/{campaign_id}/creators">
        <div class="grid form-grid">
          <div>
            <label>默认平台</label>
            <select name="platform">{"".join(option_html(p, p, "YouTube") for p in PLATFORMS)}</select>
          </div>
          <div>
            <label>批量格式</label>
            <input value="每行：达人名称, 邮箱, 主页链接" disabled>
          </div>
          <div class="full">
            <label>批量添加达人</label>
            <textarea name="bulk_creators" placeholder="CNC Kitchen, name@example.com, https://www.youtube.com/@CNCKitchen&#10;Maker's Muse, name@example.com, https://www.youtube.com/@MakersMuse"></textarea>
          </div>
        </div>
        <div class="actions"><button type="submit">添加到项目</button></div>
      </form>
    </section>
    <section class="panel">
      <h2>项目达人列表</h2>
      <p class="muted">操作顺序：先生成首封邮件；如果缺邮箱，展开“查看/编辑首封邮件草稿”补邮箱并保存；然后创建 Gmail 草稿。</p>
      <div class="table-wrap">
        <table class="compact-table">
          <thead>
            <tr><th>达人</th><th>优先级</th><th>匹配分</th><th>邮箱</th><th>平台</th><th>主页</th><th>进度</th><th>首封邮件</th><th>Gmail</th><th>草稿 ID</th><th>操作</th></tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </section>
    {auto_refresh_html}
    """
    return layout("项目", body)


def campaign_detail_page(campaign_id, flash=""):
    campaign = load_campaign(campaign_id)
    if not campaign:
        return layout("未找到", '<div class="notice">没有找到这个 Campaign。</div>')
    ensure_campaign_scores(campaign_id)
    creators = campaign_creators(campaign_id)
    with db() as conn:
        running_tasks = conn.execute(
            "SELECT id FROM campaign_tasks WHERE campaign_id = ? AND status = 'running' LIMIT 1",
            (campaign_id,),
        ).fetchall()
    metrics = {
        "total": len(creators),
        "pending": sum(1 for row in creators if row["outreach_status"] not in ("sent", "done")),
        "gmail_drafted": sum(1 for row in creators if row["gmail_draft_id"]),
        "sent": sum(1 for row in creators if row["outreach_status"] == "sent"),
        "done": sum(1 for row in creators if row["outreach_status"] == "done"),
        "errors": sum(1 for row in creators if row["outreach_error"] or row["gmail_error"]),
    }
    progress = int((metrics["sent"] + metrics["done"]) * 100 / max(1, metrics["total"]))
    stats_html = "".join(
        f'<div class="stat"><strong>{value}</strong><span>{label}</span></div>'
        for label, value in [
            ("项目达人", metrics["total"]),
            ("待跟进", metrics["pending"]),
            ("Gmail 草稿", metrics["gmail_drafted"]),
            ("已发送", metrics["sent"]),
            ("已完成", metrics["done"]),
            ("异常", metrics["errors"]),
        ]
    )
    notice_cls = "notice" if any(word in flash for word in ("失败", "错误", "超时", "timeout", "timed out")) else "notice ok"
    notice = f'<div class="{notice_cls}">{esc(flash)}</div>' if flash else ""
    auto_refresh = any(row["outreach_status"] == "generating" for row in creators) or bool(running_tasks)
    auto_refresh_html = "<script>setTimeout(() => window.location.reload(), 5000);</script>" if auto_refresh else ""

    grouped = {}
    for row in creators:
        date_key = (row["updated_at"] or row["created_at"] or "未记录日期")[:10]
        grouped.setdefault(date_key, []).append(row)
    date_sections = []
    for date_key, rows in grouped.items():
        row_parts = []
        for row in rows:
            row_parts.append(render_creator_progress_row(row))
        date_sections.append(
            f"""
        <div class="date-group">
          <div class="date-heading">{esc(date_key)} 更新</div>
          <div class="creator-progress-list">{"".join(row_parts)}</div>
        </div>
        """
        )
    rows_html = "".join(date_sections) or '<div class="muted">这个项目还没有达人。先在下方批量添加。</div>'
    inline_action_script = """
    <script>
      async function submitCreatorAction(url, data, card) {
        const controls = card ? card.querySelectorAll('button, select') : [];
        controls.forEach(item => item.disabled = true);
        try {
          const target = new URL(url, window.location.origin).toString();
          const response = await fetch(target, {
            method: 'POST',
            headers: {
              'X-Requested-With': 'fetch',
              'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
            },
            body: new URLSearchParams(data).toString()
          });
          if (!response.ok) throw new Error('请求失败，请刷新后重试');
          const contentType = response.headers.get('content-type') || '';
          if (!contentType.includes('application/json')) throw new Error('页面版本过旧，请刷新后重试');
          const result = await response.json();
          if (!result.ok) throw new Error(result.flash || '操作失败');
          if (card && result.row_html) card.outerHTML = result.row_html;
        } catch (error) {
          alert(error.message || '操作失败，请刷新后重试');
          controls.forEach(item => item.disabled = false);
        }
      }
      document.addEventListener('click', async event => {
        const button = event.target.closest('.js-stage-action');
        if (!button || button.disabled) return;
        event.preventDefault();
        const card = button.closest('.creator-progress-card');
        const data = new URLSearchParams();
        data.set('action', button.dataset.action || '');
        await submitCreatorAction(button.dataset.url, data, card);
      });
      document.addEventListener('submit', async event => {
        const form = event.target.closest('.js-inline-action');
        if (!form) return;
        event.preventDefault();
        const data = new FormData(form);
        const card = form.closest('.creator-progress-card');
        await submitCreatorAction(form.getAttribute('action'), data, card || form);
      });
      document.addEventListener('change', event => {
        if (event.target.matches('.js-inline-action select')) {
          event.target.form.requestSubmit();
        }
      });
    </script>
    """
    body = f"""
    <div class="page-head">
      <div>
        <h1>{esc(campaign['name'])}</h1>
        <p class="page-kicker">项目内只看达人进度。点进达人后再处理信件、报价、Gmail 草稿和往来记录。</p>
      </div>
      <div class="toolbar">
        <a class="button secondary" href="/">返回工作台</a>
        <a class="button secondary" href="/campaigns?edit={campaign_id}">编辑项目</a>
      </div>
    </div>
    {notice}
    <section class="grid stats">{stats_html}</section>
    <section class="grid overview-grid">
      <div class="panel">
        <div class="section-title">
          <div>
            <h2>项目情况</h2>
            <p class="muted">这些信息会作为生成首封邮件的上下文。</p>
          </div>
          {badge(f'{progress}% 完成')}
        </div>
        <div class="progress-line"><span style="width:{progress}%"></span></div>
        <div class="project-brief-grid">
          {campaign_brief_html(campaign)}
        </div>
      </div>
      <div class="panel">
        <h2>项目动作</h2>
        <p class="muted">先用项目资料生成信件，再创建 Gmail 草稿；系统不会自动发送邮件。</p>
        <form class="actions" method="post" action="/campaigns/{campaign_id}/bulk">
          <button class="secondary" type="submit" name="action" value="score">重新评分</button>
          <button type="submit" name="action" value="generate_outreach">生成首封邮件</button>
          <button class="secondary" type="submit" name="action" value="create_gmail_draft">创建 Gmail 草稿</button>
        </form>
        <form method="post" action="/campaigns/{campaign_id}/bulk" style="margin-top:16px;">
          <label>直接下指令</label>
          <div class="inline-command">
            <input name="command" placeholder="例如：给这个项目生成首封邮件并创建 Gmail 草稿">
            <button type="submit" name="action" value="command">执行</button>
          </div>
        </form>
      </div>
    </section>
    <section class="panel">
      <div class="section-title">
        <div>
          <h2>项目达人进度</h2>
          <p class="muted">按最近更新日期分组。点击达人进入详情页查看信件记录和报价信息。</p>
        </div>
        <a class="button secondary" href="#add-creators">添加达人</a>
      </div>
      {rows_html}
    </section>
    {inline_action_script}
    <section class="panel" id="add-creators">
      <h2>添加达人</h2>
      <form method="post" action="/campaigns/{campaign_id}/creators">
        <div class="grid form-grid">
          <div>
            <label>默认平台</label>
            <select name="platform">{"".join(option_html(p, p, "YouTube") for p in PLATFORMS)}</select>
          </div>
          <div>
            <label>批量格式</label>
            <input value="每行：达人名称, 邮箱, 主页链接" disabled>
          </div>
          <div class="full">
            <label>批量添加达人</label>
            <textarea name="bulk_creators" placeholder="CNC Kitchen, name@example.com, https://www.youtube.com/@CNCKitchen&#10;Maker's Muse, name@example.com, https://www.youtube.com/@MakersMuse"></textarea>
          </div>
        </div>
        <div class="actions"><button type="submit">添加到项目</button></div>
      </form>
    </section>
    {auto_refresh_html}
    """
    return layout("项目", body)


def creator_detail_page(creator_id, flash=""):
    creator = load_creator(creator_id)
    if not creator:
        return layout("达人详情", '<div class="notice">没有找到这个达人。</div>')
    with db() as conn:
        messages = conn.execute(
            """
            SELECT * FROM messages
            WHERE creator_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 30
            """,
            (creator_id,),
        ).fetchall()
        email_logs = conn.execute(
            """
            SELECT * FROM email_logs
            WHERE creator_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 30
            """,
            (creator_id,),
        ).fetchall()
    notice_cls = "notice" if any(word in flash for word in ("失败", "错误", "缺少")) else "notice ok"
    notice = f'<div class="{notice_cls}">{esc(flash)}</div>' if flash else ""
    message_html = "".join(
        f"""
        <div class="email-log-card">
          <header>
            <h3>{esc(row['product_name'])} / {badge(row['status'])}</h3>
            <span class="muted">{esc(row['updated_at'])}</span>
          </header>
          <p><strong>达人回复：</strong>{esc(row['raw_reply'])}</p>
          <p><strong>AI 摘要：</strong>{esc(row['summary_zh'] or '-')}</p>
          <p><strong>下一步：</strong>{esc(row['next_action'] or '-')}</p>
          <a class="button secondary" href="/replies/{row['id']}">查看回复详情</a>
        </div>
        """
        for row in messages
    ) or '<div class="muted">还没有达人回复记录。</div>'
    email_log_html = "".join(
        f"""
        <div class="email-log-card">
          <header>
            <h3>{esc(row['subject'] or 'Gmail 草稿')}</h3>
            {badge(row['status'])}
          </header>
          <p><strong>To：</strong>{esc(row['to_email'] or '-')}</p>
          <p><strong>Gmail 草稿 ID：</strong>{esc(row['gmail_draft_id'] or '-')}</p>
          <p class="muted">{esc(row['created_at'])}</p>
          {f'<p class="notice">错误：{esc(row["error_message"])}</p>' if row['error_message'] else ''}
        </div>
        """
        for row in email_logs
    ) or '<div class="muted">还没有 Gmail 创建记录。</div>'
    priority_options = "".join(
        f'<option value="{esc(level)}" {"selected" if (creator["priority_level"] or "") == level else ""}>{esc(level)}</option>'
        for level in PRIORITY_LEVELS
    )
    priority_editor = f"""
      <form method="post" action="/creators/{creator_id}/action">
        <input type="hidden" name="redirect_to" value="/creators/{creator_id}">
        <input type="hidden" name="action" value="save_priority">
        <select class="priority-select {'manual' if (creator['priority_manual'] if 'priority_manual' in creator.keys() else 0) else ''}" name="priority_level" onchange="this.form.submit()">
          <option value="" {"selected" if not creator["priority_level"] else ""}>未设置</option>
          {priority_options}
        </select>
      </form>
    """
    body = f"""
    <div class="page-head">
      <div>
        <h1>{esc(creator['name'])}</h1>
        <p class="page-kicker">这里处理单个达人的信件、报价备注、Gmail 草稿和回复记录。</p>
      </div>
      <div class="toolbar">
        <a class="button secondary" href="/campaigns/{creator['campaign_id'] or ''}">返回项目</a>
        {f'<a class="button secondary" href="{esc(creator["profile_url"])}" target="_blank">打开主页</a>' if creator['profile_url'] else ''}
      </div>
    </div>
    {notice}
    <section class="grid creator-meta-grid">
      <div class="mini-field"><span>项目</span><strong>{esc(creator['campaign_name'] or '-')}</strong></div>
      <div class="mini-field"><span>平台</span><strong>{esc(creator['platform'])}</strong></div>
      <div class="mini-field"><span>邮箱</span><strong>{esc(creator['email'] or '未填写')}</strong></div>
      <div class="mini-field"><span>进度</span><strong>{badge(creator['outreach_status'] or 'to_contact')}</strong></div>
      <div class="mini-field"><span>Gmail</span><strong>{badge('error' if creator['gmail_error'] else ('drafted' if creator['gmail_draft_id'] else 'pending'))}</strong></div>
      <div class="mini-field"><span>优先级</span>{priority_editor}</div>
    </section>
    <section class="two">
      <form class="panel" method="post" action="/creators/{creator_id}/action">
        <h2>达人资料与报价备注</h2>
        <input type="hidden" name="redirect_to" value="/creators/{creator_id}">
        <div class="grid form-grid">
          <div>
            <label>达人名称</label>
            <input name="name" value="{esc(creator['name'])}">
          </div>
          <div>
            <label>平台</label>
            <select name="platform">{"".join(option_html(p, p, creator['platform']) for p in PLATFORMS)}</select>
          </div>
          <div>
            <label>邮箱</label>
            <input name="email" value="{esc(creator['email'] or '')}" placeholder="name@example.com">
          </div>
          <div>
            <label>主页链接</label>
            <input name="profile_url" value="{esc(creator['profile_url'] or '')}" placeholder="https://">
          </div>
          <div>
            <label>优先邀约语言</label>
            <select name="preferred_language">
              {option_html("Arabic", "阿语", creator["preferred_language"] or "English")}
              {option_html("English", "英语", creator["preferred_language"] or "English")}
              {option_html("Arabic + English", "阿语 + 英语", creator["preferred_language"] or "English")}
            </select>
          </div>
          <div class="full">
            <label>已核验的定制切入点</label>
            <textarea name="personalization_hook" placeholder="例如：主页在 2026-09-23 展示美妆与生活方式内容，并公开提供品牌合作入口。只填实际核验的公开信息。">{esc(creator["personalization_hook"] or "")}</textarea>
          </div>
          <div><label>国家</label><select name="country">{"".join(option_html(code, name, creator["country"] or "") for code, name in SCREENING_COUNTRIES.items())}</select></div>
          <div><label>城市</label><input name="city" value="{esc(creator['city'] or '')}"></div>
          <div><label>粉丝数</label><input type="number" name="followers" min="0" value="{esc(creator['followers'] if creator['followers'] is not None else '')}"></div>
          <div><label>内容标签</label><input name="content_tags" value="{esc(creator['content_tags'] or '')}" placeholder="美妆, 眼妆"></div>
          <div><label>核验状态</label><select name="screening_status">{"".join(option_html(code, name, creator["screening_status"] or "unverified") for code, name in SCREENING_STATUSES.items())}</select></div>
          <div><label>观察日期</label><input type="date" name="observed_at" value="{esc(creator['observed_at'] or '')}"></div>
          <div class="full"><label>证据 URL</label><input type="url" name="evidence_url" value="{esc(creator['evidence_url'] or '')}"></div>
          <div><label><input type="checkbox" name="competitor_conflict" value="1" {'checked' if creator['competitor_conflict'] else ''}> 已发现直接竞品冲突</label></div>
          <div class="full">
            <label>达人报价 / 合作备注</label>
            <textarea name="notes" placeholder="例如：报价 $800/video，要求保留样品，预计下周回复。">{esc(creator['notes'] or '')}</textarea>
          </div>
        </div>
        <div class="actions"><button type="submit" name="action" value="save_profile">保存达人信息</button></div>
      </form>
      <form class="panel" method="post" action="/creators/{creator_id}/action">
        <h2>首封邮件</h2>
        <input type="hidden" name="redirect_to" value="/creators/{creator_id}">
        <input type="hidden" name="email" value="{esc(creator['email'] or '')}">
        <input type="hidden" name="profile_url" value="{esc(creator['profile_url'] or '')}">
        <label>Subject</label>
        <input name="outreach_subject" value="{esc(creator['outreach_subject'] or f'Collaboration Opportunity - {creator["campaign_name"] or "Our Product"}')}">
        <label style="margin-top:10px;">Body</label>
        <textarea name="outreach_draft" style="min-height:260px;">{esc(creator['outreach_draft'] or '')}</textarea>
        {f'<div class="notice">首封邮件错误：{esc(creator["outreach_error"])}</div>' if creator['outreach_error'] else ''}
        {f'<div class="notice">Gmail 错误：{esc(creator["gmail_error"])}</div>' if creator['gmail_error'] else ''}
        <div class="actions">
          <button type="submit" name="action" value="save_outreach">保存邮件</button>
          <button class="secondary" type="submit" name="action" value="generate_outreach">重新生成</button>
          <button class="secondary" type="submit" name="action" value="create_gmail_draft">创建 Gmail 草稿</button>
          <button class="secondary" type="submit" name="action" value="sent">标记已发送</button>
          <button class="secondary" type="submit" name="action" value="done">完成</button>
        </div>
      </form>
    </section>
    <section class="two">
      <div class="panel">
        <h2>达人回复记录</h2>
        {message_html}
      </div>
      <div class="panel">
        <h2>Gmail 草稿记录</h2>
        {email_log_html}
      </div>
    </section>
    """
    return layout("达人详情", body)


def campaigns_page(edit_id=None, flash=""):
    rows = list_campaigns()
    editing = load_campaign(edit_id) if edit_id else None
    notice = f'<div class="notice ok">{esc(flash)}</div>' if flash else ""
    templates = {
        "saudi_colored_contacts": {
            "label": "沙特 / GCC 彩色隐形眼镜定向邀约",
            "selling_points": """Campaign purpose:
Build a small, verified creator test group for a Saudi / GCC colored-contact-lens launch.

Content direction:
Natural-looking color change on dark eyes; indoor and daylight presentation; beauty / eye-makeup / lifestyle context.

Product communication:
Use only confirmed product parameters and approved copy. Do not claim treatment effects, universal suitability, zero irritation, guaranteed comfort, registration, approval, or shipping availability unless separately confirmed.""",
            "sample_policy": "This is an interest-first outreach. Sample eligibility, shipping market, availability, prescription requirements, delivery timing, and any content obligation require manual confirmation before commitment.",
            "commission_policy": "Ask for availability, rate card, deliverable options, and usage-rights pricing first. Paid fee, commission, discount code, affiliate attribution, and paid-media usage rights require a written confirmation before commitment.",
            "forbidden_promises": """Do not promise product registration, medical approval, treatment of dry eyes, universal suitability, zero irritation, or a guaranteed visible result. Do not promise samples, pricing, commission, inventory, shipping, delivery date, discount code, usage rights, exclusivity, or payment terms before manual confirmation. Do not recommend a prescription or ask for sensitive health information in the first outreach.""",
            "brand_tone": "Warm, respectful, concise and professional. Use Arabic, English, or bilingual copy only as selected for the creator. Avoid invented familiarity and medical claims.",
        },
        "jujubit_creator_program": {
            "label": "JuJuBit 初始建联 / Creator Program",
            "selling_points": """Preferred subject: Collab? Create a custom 3D model with JuJuBit

Brand intro:
JuJuBit is an AI-powered collectible platform backed by Tripo AI, with 10M+ users globally.

Creator fit:
We are expanding our global creator program and looking for creators whose content style and audience align with AI tools, 3D modeling, collectibles, design, gaming, and creative workflows.

Email structure:
1. Friendly intro from Ryan at JuJuBit.
2. Mention why the creator may be a good fit.
3. Show "What We Offer" as clear bullets.
4. Explain the $10,000 Creator Bonus Campaign briefly.
5. Content direction: Create -> Share the process -> Reveal the final collectible.
6. Include Create Page link: https://jujubit.ai/products/customize-your-own
7. End by offering to share more details and discuss ideas for their audience.""",
            "sample_policy": "Product access / creator experience can be shared through the JuJuBit Create Page. Any physical sample, special access, shipping, or custom fulfillment still needs manual confirmation.",
            "commission_policy": """Fixed offer terms:
- 20% OFF code for the creator's audience.
- 5% commission on every order generated through the creator's link/code.
- Guaranteed $100 ad investment behind this collaboration.
- Access to the $10,000 Creator Bonus Campaign.

Bonus ranking factors:
- Conversion performance tracked through UTM links.
- Video performance and overall reach/views.
- Community engagement and content quality.""",
            "forbidden_promises": "Do not promise additional fixed fees, extra ad spend beyond the stated $100, guaranteed bonus payout, guaranteed sample shipping, inventory, logistics timelines, or custom terms unless manually confirmed.",
            "brand_tone": "Friendly, direct, creator-first, energetic, professional. Sign off as Ryan, JuJuBit Team.",
        },
        "3d_printing": {
            "label": "3D Printing 产品测评",
            "selling_points": "AI-assisted 3D printing workflow, practical maker use cases, easy setup, useful for creators who publish 3D printing tutorials or product reviews.",
            "sample_policy": "Sample availability and shipping arrangement need manual confirmation before any commitment.",
            "commission_policy": "Budget, commission, and paid collaboration terms need manual confirmation after reviewing the creator's media kit.",
            "forbidden_promises": default_forbidden_promises(),
            "brand_tone": "Friendly, concise, professional, maker-focused",
        },
        "ai_tools": {
            "label": "AI 工具推广",
            "selling_points": "AI productivity workflow, simple demo angle, clear creator value, suitable for tutorial and tool-review content.",
            "sample_policy": "Account access, trial, or product access need manual confirmation.",
            "commission_policy": "Sponsorship fee, affiliate commission, and usage rights need manual confirmation.",
            "forbidden_promises": default_forbidden_promises(),
            "brand_tone": "Clear, practical, confident, not exaggerated",
        },
        "gamedev": {
            "label": "GameDev / 独立游戏工具",
            "selling_points": "Useful for developers, supports demo-driven content, can be shown through workflow examples and build logs.",
            "sample_policy": "License, access, and review copy need manual confirmation.",
            "commission_policy": "Paid integration, affiliate, or revenue-share terms need manual confirmation.",
            "forbidden_promises": default_forbidden_promises(),
            "brand_tone": "Builder-friendly, specific, respectful",
        },
        "animation": {
            "label": "Animation / 创意工具",
            "selling_points": "Visual workflow improvement, creator-friendly demo, useful for animation, design, and production content.",
            "sample_policy": "Tool access, sample files, and creator support need manual confirmation.",
            "commission_policy": "Sponsorship, licensing, and affiliate terms need manual confirmation.",
            "forbidden_promises": default_forbidden_promises(),
            "brand_tone": "Creative, concise, professional",
        },
    }
    template_options = '<option value="">不使用模板</option>' + "".join(
        f'<option value="{key}">{esc(value["label"])}</option>'
        for key, value in templates.items()
    )
    template_json = json.dumps(templates, ensure_ascii=False).replace("</", "<\\/")
    row_html = "".join(
        f"""
        <tr>
          <td>{esc(row['name'])}</td>
          <td>{esc(row['selling_points'] or '-')}</td>
          <td>{esc(row['sample_policy'] or '-')}</td>
          <td>{esc(row['commission_policy'] or '-')}</td>
          <td>{esc(row['updated_at'])}</td>
          <td>
            <a class="button secondary" href="/campaigns/{row['id']}">打开项目</a>
            <a class="button secondary" href="/campaigns?edit={row['id']}">编辑</a>
          </td>
        </tr>
        """
        for row in rows
    ) or '<tr><td colspan="6" class="muted">还没有 Campaign。</td></tr>'
    body = f"""
    <h1>项目设置</h1>
    {notice}
    <form class="panel" method="post" action="/campaigns">
      <h2>{'编辑 Campaign' if editing else '新增 Campaign'}</h2>
      <input type="hidden" name="id" value="{esc(editing['id'] if editing else '')}">
      <div class="grid form-grid">
        <div class="full">
          <label>项目模板</label>
          <select id="campaign-template">
            {template_options}
          </select>
          <p class="muted">选择模板后会填入项目情况；你仍然可以继续手动修改。</p>
        </div>
      </div>
      <div class="grid form-grid">
        <div>
          <label>产品名称 <span class="required">*</span></label>
          <input name="name" value="{esc(editing['name'] if editing else '')}" required>
        </div>
        <div>
          <label>默认品牌语气</label>
          <input name="brand_tone" value="{esc((editing['brand_tone'] if editing else '') or default_brand_tone())}">
        </div>
        <div class="full">
          <label>产品卖点</label>
          <textarea name="selling_points">{esc(editing['selling_points'] if editing else '')}</textarea>
        </div>
        <div>
          <label>样品政策</label>
          <textarea name="sample_policy">{esc(editing['sample_policy'] if editing else '')}</textarea>
        </div>
        <div>
          <label>佣金/报价规则</label>
          <textarea name="commission_policy">{esc(editing['commission_policy'] if editing else '')}</textarea>
        </div>
        <div class="full">
          <label>禁止承诺项</label>
          <textarea name="forbidden_promises">{esc((editing['forbidden_promises'] if editing else '') or default_forbidden_promises())}</textarea>
        </div>
      </div>
      <div class="actions">
        <button type="submit">保存项目</button>
        <a class="button secondary" href="{default_campaign_href()}">返回项目</a>
      </div>
    </form>
    <section class="panel">
      <h2>Campaign 列表</h2>
      <div class="table-wrap">
        <table class="compact-table">
          <thead>
            <tr><th>产品</th><th>卖点</th><th>样品政策</th><th>报价规则</th><th>更新时间</th><th>操作</th></tr>
          </thead>
          <tbody>{row_html}</tbody>
        </table>
      </div>
    </section>
    <script>
      const campaignTemplates = {template_json};
      const templateSelect = document.getElementById('campaign-template');
      if (templateSelect) {{
        templateSelect.addEventListener('change', () => {{
          const tpl = campaignTemplates[templateSelect.value];
          if (!tpl) return;
          for (const [key, value] of Object.entries({{
            selling_points: tpl.selling_points,
            sample_policy: tpl.sample_policy,
            commission_policy: tpl.commission_policy,
            forbidden_promises: tpl.forbidden_promises,
            brand_tone: tpl.brand_tone,
          }})) {{
            const field = document.querySelector(`[name="${{key}}"]`);
            if (field) field.value = value;
          }}
        }});
      }}
    </script>
    """
    return layout("项目设置", body)


def settings_page(flash=""):
    notice = f'<div class="notice ok">{esc(flash)}</div>' if flash else ""
    body = f"""
    <h1>设置</h1>
    {notice}
    <form class="panel" method="post" action="/settings">
      <div class="grid form-grid">
        <div class="full">
          <label>默认品牌语气</label>
          <textarea name="default_brand_tone">{esc(default_brand_tone())}</textarea>
        </div>
        <div class="full">
          <label>默认禁止承诺项</label>
          <textarea name="default_forbidden_promises">{esc(default_forbidden_promises())}</textarea>
        </div>
        <div>
          <label>飞书 Webhook URL 状态</label>
          <input value="{esc(feishu_status_text())}" disabled>
        </div>
        <div>
          <label>本地保存 Webhook URL</label>
          <input name="feishu_webhook_url" value="" placeholder="留空则不修改，页面不会展示完整 URL">
        </div>
        <div>
          <label>AI API 状态</label>
          <input value="{esc(ai_status_text())}" disabled>
        </div>
        <div>
          <label>AI API Key</label>
          <input name="openai_api_key" value="" placeholder="留空则不修改，页面不会展示完整 Key">
        </div>
        <div>
          <label>AI 模型</label>
          <input name="openai_model" value="{esc(ai_model())}">
        </div>
        <div>
          <label>AI API URL</label>
          <input name="openai_api_url" value="{esc(ai_api_url())}">
        </div>
        <div class="full">
          <h2>Gmail</h2>
        </div>
        <div>
          <label>Gmail 授权状态</label>
          <input value="{esc(gmail_status_text())}" disabled>
        </div>
        <div>
          <label>OAuth 回调地址</label>
          <input value="{esc(google_redirect_uri())}" disabled>
        </div>
        <div>
          <label>Google Client ID</label>
          <input name="google_client_id" value="{esc(google_client_id() or '')}">
        </div>
        <div>
          <label>Google Client Secret</label>
          <input name="google_client_secret" value="" placeholder="留空则不修改，页面不会展示完整 Secret">
        </div>
        <div class="full">
          <label>Google Redirect URI</label>
          <input name="google_redirect_uri" value="{esc(google_redirect_uri())}">
        </div>
      </div>
      <div class="actions">
        <button type="submit">保存设置</button>
        <a class="button secondary" href="/auth/google/start">连接 Gmail</a>
        <button class="secondary" type="submit" formaction="/auth/google/disconnect">断开 Gmail</button>
      </div>
    </form>
    """
    return layout("设置", body)


def errors_page(error_id=None, flash=""):
    notice = f'<div class="notice ok">{esc(flash)}</div>' if flash else ""
    with db() as conn:
        stats = {
            row["source"]: row["count"]
            for row in conn.execute(
                """
                SELECT source, COUNT(*) AS count
                FROM error_logs
                WHERE resolved = 0
                  AND NOT (source = 'gmail' AND message = 'Google OAuth state 校验失败')
                GROUP BY source
                """
            ).fetchall()
        }
        rows = conn.execute(
            """
            SELECT e.*,
                   COALESCE(message_creator.name, creator.name) AS creator_name
            FROM error_logs e
            LEFT JOIN messages m ON e.related_type = 'message' AND e.related_id = m.id
            LEFT JOIN creators message_creator ON message_creator.id = m.creator_id
            LEFT JOIN creators creator ON e.related_type = 'creator' AND e.related_id = creator.id
            WHERE e.resolved = 0
              AND NOT (e.source = 'gmail' AND e.message = 'Google OAuth state 校验失败')
            ORDER BY e.created_at DESC
            LIMIT 100
            """
        ).fetchall()
        detail = conn.execute("SELECT * FROM error_logs WHERE id = ?", (error_id,)).fetchone() if error_id else None
    detail_html = ""
    if detail:
        detail_html = f"""
        <section class="panel">
          <h2>错误详情 #{detail['id']}</h2>
          <p><strong>来源：</strong>{esc(detail['source'])}</p>
          <p><strong>关联对象：</strong>{esc(detail['related_type'] or '-')} / {esc(detail['related_id'] or '-')}</p>
          <p><strong>摘要：</strong>{esc(detail['message'])}</p>
          <label>详情</label>
          <div class="pre">{esc(detail['detail'] or '-')}</div>
        </section>
        """
    labels = [("ai", "AI 错误"), ("feishu", "飞书错误"), ("gmail", "Gmail 错误"), ("app", "系统错误")]
    stat_html = "".join(
        f'<div class="stat"><strong>{stats.get(source, 0)}</strong><span>{label}</span></div>'
        for source, label in labels
    )
    def render_rows(source):
        subset = [row for row in rows if row["source"] == source]
        return "".join(
            f"""
            <tr>
              <td>{esc(row['created_at'])}</td>
              <td>{badge(row['source'])}</td>
              <td>{esc(row['creator_name'] or '-')}</td>
              <td>{esc(row['related_type'] or '-')} / {esc(row['related_id'] or '-')}</td>
              <td>{esc(row['message'])}</td>
              <td>{badge('done' if row['resolved'] else 'needs_human')}</td>
              <td>
                <a class="button secondary" href="/errors?id={row['id']}">查看</a>
                <form method="post" action="/errors/{row['id']}/resolve" style="display:inline">
                  <button class="secondary" type="submit">标记已解决</button>
                </form>
                {f'<a class="button secondary" href="/replies/{row["related_id"]}">处理回复</a>' if row['related_type'] == 'message' and row['related_id'] else ''}
              </td>
            </tr>
            """
            for row in subset
        ) or '<tr><td colspan="7" class="muted">暂无此类错误。</td></tr>'
    groups_html = "".join(
        f"""
        <section class="panel">
          <h2>{label}</h2>
          <div class="table-wrap">
            <table class="compact-table">
              <thead>
                <tr><th>时间</th><th>来源</th><th>关联达人</th><th>关联回复</th><th>错误摘要</th><th>是否已解决</th><th>操作</th></tr>
              </thead>
              <tbody>{render_rows(source)}</tbody>
            </table>
          </div>
        </section>
        """
        for source, label in labels
    )
    body = f"""
    <div class="page-head">
      <div>
        <h1>错误日志</h1>
        <p class="page-kicker">按来源查看 AI、飞书、Gmail 和系统异常，快速回到关联回复处理。</p>
      </div>
    </div>
    {notice}
    <section class="grid stats">{stat_html}</section>
    {detail_html}
    {groups_html}
    """
    return layout("错误日志", body)


class App(BaseHTTPRequestHandler):
    def send_html(self, html_text, status=200, headers=None):
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def wants_json(self):
        return self.headers.get("X-Requested-With") == "fetch"

    def redirect(self, path):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/":
            return self.send_html(home_page())
        if path == "/followups":
            return self.redirect("/creators")
        if path == "/creators":
            return self.send_html(creators_page(query))
        if path == "/screening":
            return self.send_html(screening_page(query))
        match = re.fullmatch(r"/creators/(\d+)", path)
        if match:
            flash = query.get("flash", [""])[0]
            return self.send_html(creator_detail_page(int(match.group(1)), flash))
        if path == "/gmail":
            return self.send_html(gmail_page())
        if path == "/replies/new":
            return self.send_html(new_reply_page())
        if path == "/campaigns":
            edit_id = query.get("edit", [""])[0]
            new_mode = query.get("new", [""])[0]
            flash = query.get("flash", [""])[0]
            if not edit_id and not new_mode:
                return self.redirect(default_campaign_href())
            return self.send_html(campaigns_page(int(edit_id) if edit_id.isdigit() else None, flash))
        match = re.fullmatch(r"/campaigns/(\d+)", path)
        if match:
            flash = query.get("flash", [""])[0]
            return self.send_html(campaign_detail_page(int(match.group(1)), flash))
        if path == "/settings":
            flash = query.get("flash", [""])[0]
            return self.send_html(settings_page(flash))
        if path == "/auth/google/start":
            if not gmail_config_ready():
                return self.redirect(f"/settings?flash={urllib.parse.quote('请先配置 Google Client ID、Client Secret 和 Redirect URI')}")
            state = secrets.token_urlsafe(24)
            save_setting("google_oauth_state", state)
            params = {
                "client_id": google_client_id(),
                "redirect_uri": google_redirect_uri(),
                "response_type": "code",
                "scope": gmail_scope(),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": state,
            }
            return self.redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))
        if path == "/auth/google/callback":
            if query.get("error"):
                error = query.get("error", ["Google OAuth 授权失败"])[0]
                create_error_log("gmail", "Google OAuth 授权失败", error, "gmail_account", None)
                return self.redirect(f"/settings?flash={urllib.parse.quote('Google OAuth 授权失败')}")
            state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            if not state or state != get_setting("google_oauth_state"):
                create_error_log("gmail", "Google OAuth state 校验失败", "state mismatch", "gmail_account", None)
                return self.redirect(f"/settings?flash={urllib.parse.quote('Google OAuth state 校验失败')}")
            if not code:
                return self.redirect(f"/settings?flash={urllib.parse.quote('Google OAuth 缺少 code')}")
            try:
                token_data = exchange_google_code(code)
                save_gmail_account(token_data)
                save_setting("google_oauth_state", "")
                return self.redirect(f"/settings?flash={urllib.parse.quote('Gmail 已连接')}")
            except Exception as exc:
                create_error_log("gmail", "Google OAuth token 交换失败", str(exc), "gmail_account", None)
                return self.redirect(f"/settings?flash={urllib.parse.quote('Gmail 连接失败，查看错误日志')}")
        if path == "/errors":
            error_id = query.get("id", [""])[0]
            flash = query.get("flash", [""])[0]
            return self.send_html(errors_page(int(error_id) if error_id.isdigit() else None, flash))
        match = re.fullmatch(r"/replies/(\d+)", path)
        if match:
            flash = query.get("flash", [""])[0]
            return self.send_html(detail_page(int(match.group(1)), flash))
        self.send_html(layout("404", '<div class="notice">页面不存在。</div>'), 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/screening/candidates":
            data = parse_post(self)
            name = (data.get("name") or "").strip()[:120]
            profile_url = (data.get("profile_url") or "").strip()[:1000]
            evidence_url = (data.get("evidence_url") or "").strip()[:1000]
            if not name or (profile_url and not profile_url.startswith(("https://", "http://"))) or (
                evidence_url and not evidence_url.startswith(("https://", "http://"))
            ):
                return self.redirect("/screening?flash=" + urllib.parse.quote("姓名或链接无效"))
            try:
                followers = parse_followers(data.get("followers"))
            except ValueError as exc:
                return self.redirect("/screening?flash=" + urllib.parse.quote(str(exc)))
            try:
                campaign_id = int(data.get("campaign_id") or 0)
            except ValueError:
                campaign_id = 0
            if not load_campaign(campaign_id):
                return self.redirect("/screening?flash=" + urllib.parse.quote("请先建立项目"))
            country = data.get("country") if data.get("country") in SCREENING_COUNTRIES else ""
            status = data.get("screening_status") if data.get("screening_status") in SCREENING_STATUSES else "unverified"
            # A verified country and follower count need a dated, inspectable source.
            observed_at = (data.get("observed_at") or "").strip()
            if status == "verified" and (not evidence_url or not observed_at or followers is None or not country):
                return self.redirect("/screening?flash=" + urllib.parse.quote("已核验需要国家、粉丝数、证据链接及观察日期"))
            with db() as conn:
                existing = conn.execute(
                    "SELECT id FROM creators WHERE campaign_id = ? AND profile_url = ?",
                    (campaign_id, profile_url),
                ).fetchone() if profile_url else None
            if existing:
                return self.redirect("/creators/" + str(existing["id"]) + "?flash=" + urllib.parse.quote("该主页已在项目中，请在此编辑"))
            creator_id = create_creator_for_campaign(
                campaign_id, name, profile_url=profile_url,
                platform=data.get("platform") if data.get("platform") in PLATFORMS else "TikTok",
                notes=(data.get("notes") or "")[:3000],
            )
            with db() as conn:
                conn.execute(
                    """UPDATE creators SET country=?, city=?, followers=?, content_tags=?, evidence_url=?,
                       observed_at=?, competitor_conflict=?, screening_status=?, updated_at=? WHERE id=?""",
                    (country, (data.get("city") or "")[:100], followers,
                     (data.get("content_tags") or "")[:250], evidence_url, observed_at,
                     1 if data.get("competitor_conflict") == "1" else 0,
                     status, now_iso(), creator_id),
                )
            return self.redirect("/creators/" + str(creator_id) + "?flash=" + urllib.parse.quote("候选达人已保存，尚未发送邀约"))
        if path == "/replies":
            data = parse_post(self)
            missing = required(data, ["creator_name", "platform", "raw_reply"])
            if not data.get("campaign_id") and not data.get("product_name"):
                missing.append("product_name")
            if data.get("platform") and data.get("platform") not in PLATFORMS:
                missing.append("platform")
            if missing:
                return self.send_html(new_reply_page(f"请补全必填字段：{', '.join(missing)}", data), 400)
            message_id = create_message(data)
            try:
                analyze_and_save(message_id)
                notify_if_needed(message_id)
                return self.redirect(f"/replies/{message_id}")
            except Exception as exc:
                mark_ai_failed(message_id, exc)
                notify_if_needed(message_id)
                return self.redirect(f"/replies/{message_id}?flash={urllib.parse.quote('原始回复已保存，但 AI 整理失败')}")

        if path == "/campaigns":
            data = parse_post(self)
            if not data.get("name"):
                return self.send_html(campaigns_page(None, "产品名称不能为空"), 400)
            ts = now_iso()
            with db() as conn:
                if data.get("id"):
                    conn.execute(
                        """
                        UPDATE campaigns
                        SET name = ?, selling_points = ?, sample_policy = ?,
                            commission_policy = ?, forbidden_promises = ?, brand_tone = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            data.get("name"),
                            data.get("selling_points"),
                            data.get("sample_policy"),
                            data.get("commission_policy"),
                            data.get("forbidden_promises"),
                            data.get("brand_tone"),
                            ts,
                            int(data["id"]),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO campaigns
                          (name, selling_points, sample_policy, commission_policy,
                           forbidden_promises, brand_tone, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            data.get("name"),
                            data.get("selling_points"),
                            data.get("sample_policy"),
                            data.get("commission_policy"),
                            data.get("forbidden_promises"),
                            data.get("brand_tone"),
                            ts,
                            ts,
                        ),
                    )
            return self.redirect(f"/campaigns?flash={urllib.parse.quote('Campaign 已保存')}")

        match = re.fullmatch(r"/campaigns/(\d+)/creators", path)
        if match:
            campaign_id = int(match.group(1))
            data = parse_post(self)
            platform = data.get("platform") if data.get("platform") in PLATFORMS else "YouTube"
            added = 0
            for line in (data.get("bulk_creators") or "").splitlines():
                parts = [part.strip() for part in line.split(",")]
                name = parts[0] if parts else ""
                if not name:
                    continue
                email_addr = parts[1] if len(parts) > 1 else ""
                profile_url = parts[2] if len(parts) > 2 else ""
                create_creator_for_campaign(campaign_id, name, email_addr, profile_url, platform)
                added += 1
            return self.redirect(f"/campaigns/{campaign_id}?flash={urllib.parse.quote(f'已添加 {added} 个达人')}")

        match = re.fullmatch(r"/campaigns/(\d+)/bulk", path)
        if match:
            campaign_id = int(match.group(1))
            data = parse_post(self)
            action = data.get("action")
            if action == "command":
                start_background_campaign_action(campaign_id, command=data.get("command"))
                flash = "已开始后台执行指令，结果会写入最近任务。"
            elif action in ("generate_outreach", "create_gmail_draft"):
                start_background_campaign_action(campaign_id, action=action)
                flash = "已开始后台执行，页面不会卡住；稍后查看最近任务和任务明细。"
            else:
                flash = execute_campaign_action(campaign_id, action)
            return self.redirect(f"/campaigns/{campaign_id}?flash={urllib.parse.quote(flash)}")

        match = re.fullmatch(r"/creators/(\d+)/action", path)
        if match:
            creator_id = int(match.group(1))
            data = parse_post(self)
            action = data.get("action")
            if not action and "platform" in data:
                action = "save_platform"
            if not action and "priority_level" in data:
                action = "save_priority"
            creator = load_creator(creator_id)
            if not creator:
                return self.redirect(f"/creators?flash={urllib.parse.quote('达人不存在')}")
            campaign_id = creator["campaign_id"]
            redirect_to = data.get("redirect_to") or f"/campaigns/{campaign_id}"
            if not action:
                flash = "页面状态已过期，请刷新后重试"
            elif action == "generate_outreach":
                start_background_creator_outreach(creator_id)
                flash = "已开始后台生成首封邮件，完成后状态会自动更新。"
            elif action == "save_profile":
                platform = data.get("platform") if data.get("platform") in PLATFORMS else creator["platform"]
                try:
                    followers = parse_followers(data.get("followers"))
                except ValueError as exc:
                    flash = str(exc)
                    return self.redirect(f"{redirect_to}?flash={urllib.parse.quote(flash)}")
                country = data.get("country") if data.get("country") in SCREENING_COUNTRIES else ""
                status = data.get("screening_status") if data.get("screening_status") in SCREENING_STATUSES else "unverified"
                evidence_url = (data.get("evidence_url") or "").strip()[:1000]
                observed_at = (data.get("observed_at") or "").strip()
                if status == "verified" and (not country or followers is None or not evidence_url or not observed_at):
                    return self.redirect(f"{redirect_to}?flash=" + urllib.parse.quote("已核验需要国家、粉丝数、证据链接及观察日期"))
                if evidence_url and not evidence_url.startswith(("https://", "http://")):
                    return self.redirect(f"{redirect_to}?flash=" + urllib.parse.quote("证据 URL 无效"))
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE creators
                        SET name = ?, platform = ?, email = ?, profile_url = ?, preferred_language = ?,
                            personalization_hook = ?, notes = ?, country = ?, city = ?,
                            followers = ?, content_tags = ?, evidence_url = ?, observed_at = ?,
                            competitor_conflict = ?, screening_status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            data.get("name") or creator["name"],
                            platform,
                            data.get("email"),
                            data.get("profile_url"),
                            data.get("preferred_language")
                            if data.get("preferred_language") in ("Arabic", "English", "Arabic + English")
                            else creator_outreach_language(creator),
                            data.get("personalization_hook"),
                            data.get("notes"),
                            country,
                            (data.get("city") or "")[:100],
                            followers,
                            (data.get("content_tags") or "")[:250],
                            evidence_url,
                            observed_at,
                            1 if data.get("competitor_conflict") == "1" else 0,
                            status,
                            now_iso(),
                            creator_id,
                        ),
                    )
                update_creator_score(creator_id)
                flash = "达人信息已保存"
            elif action == "save_outreach":
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE creators
                        SET email = ?, profile_url = ?, outreach_subject = ?, outreach_draft = ?,
                            outreach_status = 'draft_generated', outreach_error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            data.get("email"),
                            data.get("profile_url"),
                            data.get("outreach_subject"),
                            data.get("outreach_draft"),
                            now_iso(),
                            creator_id,
                        ),
                    )
                flash = "首封邮件草稿已保存"
            elif action == "save_priority":
                priority_level = data.get("priority_level")
                if priority_level and priority_level not in PRIORITY_LEVELS:
                    flash = "未识别的优先级"
                else:
                    with db() as conn:
                        conn.execute(
                            """
                            UPDATE creators
                            SET priority_level = ?, priority_manual = ?, score_reason = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                priority_level or None,
                                1 if priority_level else 0,
                                "人工设置优先级" if priority_level else "未设置人工优先级",
                                now_iso(),
                                creator_id,
                            ),
                        )
                    flash = "优先级已保存"
            elif action == "save_platform":
                platform = data.get("platform") if data.get("platform") in PLATFORMS else creator["platform"]
                with db() as conn:
                    conn.execute(
                        "UPDATE creators SET platform = ?, updated_at = ? WHERE id = ?",
                        (platform, now_iso(), creator_id),
                    )
                flash = "平台已保存"
            elif action == "create_gmail_draft":
                ok, result = create_creator_gmail_draft(creator_id)
                flash = f"Gmail 草稿已创建：{result}" if ok else f"Gmail 草稿创建失败：{result}"
            elif action == "sent":
                with db() as conn:
                    conn.execute(
                        "UPDATE creators SET outreach_status = 'sent', updated_at = ? WHERE id = ?",
                        (now_iso(), creator_id),
                    )
                flash = "已标记发送"
            elif action == "done":
                with db() as conn:
                    conn.execute(
                        "UPDATE creators SET outreach_status = 'done', updated_at = ? WHERE id = ?",
                        (now_iso(), creator_id),
                    )
                flash = "已完成"
            elif action and action.startswith("stage:"):
                stage = action.split(":", 1)[1]
                if stage not in LIFECYCLE_STAGES:
                    flash = "未识别的达人阶段"
                else:
                    with db() as conn:
                        conn.execute(
                            "UPDATE creators SET outreach_status = ?, updated_at = ? WHERE id = ?",
                            (stage, now_iso(), creator_id),
                        )
                    flash = f"已更新阶段：{lifecycle_stage(stage)[0]}"
            else:
                flash = "未识别的操作"
            if self.wants_json():
                updated_creator = load_creator(creator_id)
                return self.send_json(
                    {
                        "ok": "未识别" not in flash and "失败" not in flash,
                        "flash": flash,
                        "row_html": render_creator_progress_row(updated_creator) if updated_creator else "",
                    }
                )
            separator = "&" if "?" in redirect_to else "?"
            return self.redirect(f"{redirect_to}{separator}flash={urllib.parse.quote(flash)}")

        if path == "/settings":
            data = parse_post(self)
            save_setting("default_brand_tone", data.get("default_brand_tone"))
            save_setting("default_forbidden_promises", data.get("default_forbidden_promises"))
            if data.get("feishu_webhook_url"):
                save_setting("feishu_webhook_url", data.get("feishu_webhook_url"))
            if data.get("openai_api_key"):
                save_setting("openai_api_key", data.get("openai_api_key"))
            if data.get("openai_model"):
                save_setting("openai_model", data.get("openai_model"))
            if data.get("openai_api_url"):
                save_setting("openai_api_url", data.get("openai_api_url"))
            if data.get("google_client_id"):
                save_setting("google_client_id", data.get("google_client_id"))
            if data.get("google_client_secret"):
                save_setting("google_client_secret", data.get("google_client_secret"))
            if data.get("google_redirect_uri"):
                save_setting("google_redirect_uri", data.get("google_redirect_uri"))
            return self.redirect(f"/settings?flash={urllib.parse.quote('设置已保存')}")

        if path == "/auth/google/disconnect":
            with db() as conn:
                conn.execute("DELETE FROM gmail_accounts")
            return self.redirect(f"/settings?flash={urllib.parse.quote('Gmail 已断开')}")

        match = re.fullmatch(r"/replies/(\d+)/quick", path)
        if match:
            message_id = int(match.group(1))
            data = parse_post(self)
            action = data.get("action")
            if action == "done":
                with db() as conn:
                    conn.execute(
                        "UPDATE messages SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ?",
                        (now_iso(), now_iso(), message_id),
                    )
                return self.redirect("/")
            if action == "create_gmail_draft":
                ok, result = create_gmail_draft(message_id)
                flash = f"Gmail 草稿已创建：{result}" if ok else f"Gmail 草稿创建失败：{result}"
                return self.redirect(f"/replies/{message_id}?flash={urllib.parse.quote(flash)}")
            return self.redirect("/")

        match = re.fullmatch(r"/errors/(\d+)/resolve", path)
        if match:
            mark_error_resolved(int(match.group(1)))
            return self.redirect(f"/errors?flash={urllib.parse.quote('错误已标记为已解决')}")

        match = re.fullmatch(r"/replies/(\d+)/update", path)
        if match:
            message_id = int(match.group(1))
            data = parse_post(self)
            category = data.get("category") if data.get("category") in CATEGORIES else "needs_human"
            action = data.get("action")
            status = category
            fields = {"sent_at": None, "completed_at": None}
            if action == "sent":
                status = "sent"
                fields["sent_at"] = now_iso()
            elif action == "done":
                status = "done"
                fields["completed_at"] = now_iso()
            elif action == "needs_human":
                status = "needs_human"
                category = "needs_human"
            elif action == "resend_feishu":
                current = load_message(message_id)
                status = current["status"] if current else category
            elif action == "create_gmail_draft":
                current = load_message(message_id)
                status = current["status"] if current else category
            with db() as conn:
                conn.execute(
                    """
                    UPDATE messages
                    SET category = ?, final_reply = ?, internal_notes = ?, creator_email = ?, status = ?,
                        sent_at = COALESCE(?, sent_at),
                        completed_at = COALESCE(?, completed_at),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        category,
                        data.get("final_reply"),
                        data.get("internal_notes"),
                        data.get("creator_email"),
                        status,
                        fields["sent_at"],
                        fields["completed_at"],
                        now_iso(),
                        message_id,
                    ),
                )
            if action == "resend_feishu":
                ok, error = send_feishu_notification(message_id, is_retry=True)
                flash = "飞书通知已重新发送" if ok else f"飞书通知失败：{error}"
            elif action == "create_gmail_draft":
                ok, result = create_gmail_draft(message_id)
                flash = f"Gmail 草稿已创建：{result}" if ok else f"Gmail 草稿创建失败：{result}"
            else:
                flash = "已保存"
            return self.redirect(f"/replies/{message_id}?flash={urllib.parse.quote(flash)}")
        self.send_html(layout("404", '<div class="notice">页面不存在。</div>'), 404)

    def log_message(self, fmt, *args):
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")


def main():
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), App)
    print(f"CreatorReach AI已启动：http://127.0.0.1:{port}")
    print(f"SQLite 数据库：{DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
