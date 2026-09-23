"""Pure parsing and decision rules for the TikTok Shop targeted invite queue."""

import csv
import io
import re
from datetime import date, timedelta
from urllib.parse import urlparse


MIN_FOLLOWERS = 1_000
MAX_FOLLOWERS = 100_000
MIN_UNITS_SOLD = 100
MIN_AVG_VIEWS = 100
METRIC_WINDOW_DAYS = 30

HEADER_ALIASES = {
    "账号": "handle", "账号/名称": "handle", "达人": "handle",
    "handle": "handle", "username": "handle", "平台": "platform",
    "platform": "platform", "粉丝": "followers", "followers": "followers",
    "成交件数": "units_sold", "成交": "units_sold", "units_sold": "units_sold",
    "平均播放": "avg_views", "平均播放量": "avg_views", "avg_views": "avg_views",
    "主页url": "profile_url", "主页链接": "profile_url", "profile_url": "profile_url",
    "证据url": "evidence_url", "证据链接": "evidence_url", "evidence_url": "evidence_url",
    "观察日期": "observed_at", "observed_at": "observed_at",
    "统计天数": "metrics_window_days", "metrics_window_days": "metrics_window_days",
    "上次邀约日期": "prior_invited_at", "prior_invited_at": "prior_invited_at",
    "黑名单": "blacklist_status", "blacklist_status": "blacklist_status",
    "内容证据url": "content_evidence_url", "content_evidence_url": "content_evidence_url",
    "来源截图": "source_ref", "截图编号": "source_ref", "source_ref": "source_ref",
}


def normalize_handle(value, profile_url=""):
    """Return a stable platform username, never a display-name guess from a URL."""
    source = (profile_url or value or "").strip().strip("`")
    if source.startswith(("https://", "http://")):
        parsed = urlparse(source)
        if parsed.hostname not in ("tiktok.com", "www.tiktok.com", "m.tiktok.com"):
            return ""
        path = parsed.path
        match = re.search(r"/@([A-Za-z0-9._]{2,40})(?:/|$)", path)
        source = match.group(1) if match else ""
    source = source.strip().lstrip("@").rstrip("¹").lower()
    return source if re.fullmatch(r"[a-z0-9._]{2,40}", source) else ""


def parse_count(value):
    value = str(value or "").strip().replace(",", "").replace(" ", "")
    if not value or value in {"-", "未知"}:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(万|千|[kKmM])?", value)
    if not match:
        raise ValueError(f"无法识别的数字：{value}")
    number = float(match.group(1))
    multiplier = {None: 1, "千": 1_000, "万": 10_000, "k": 1_000, "K": 1_000,
                  "m": 1_000_000, "M": 1_000_000}[match.group(2)]
    return round(number * multiplier)


def _rows_from_text(text):
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    if lines[0].lstrip().startswith("|"):
        table = []
        for line in lines:
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
                continue
            table.append(cells)
        return table
    delimiter = "\t" if "\t" in lines[0] else ","
    return list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))


def parse_candidates(text):
    """Parse pasted TSV, CSV, or one Markdown table into rows and errors."""
    raw = _rows_from_text(text)
    if not raw:
        return [], ["没有可解析的候选行"]
    headers = [HEADER_ALIASES.get(h.strip().lower().replace(" ", "")) for h in raw[0]]
    if "handle" not in headers or "followers" not in headers:
        return [], ["首行必须包含账号和粉丝列"]
    rows, errors = [], []
    seen = set()
    for line_no, cells in enumerate(raw[1:], 2):
        if len(cells) != len(headers):
            errors.append(f"第 {line_no} 行列数与表头不一致")
            continue
        item = {key: value.strip().strip("`") for key, value in zip(headers, cells) if key}
        profile_url = item.get("profile_url", "")
        for url_field in ("profile_url", "evidence_url", "content_evidence_url"):
            link = item.get(url_field, "")
            if link and urlparse(link).scheme not in ("http", "https"):
                errors.append(f"第 {line_no} 行{url_field}必须是 http(s) 链接")
                break
        else:
            link = ""
        if link:
            continue
        handle = normalize_handle(item.get("handle", ""), profile_url)
        if not handle:
            errors.append(f"第 {line_no} 行账号格式不明确，请填平台用户名或主页 URL")
            continue
        typed_handle = normalize_handle(item.get("handle", ""))
        if profile_url and typed_handle and typed_handle != handle:
            errors.append(f"第 {line_no} 行账号与主页 URL 不一致")
            continue
        platform = item.get("platform", "TikTok") or "TikTok"
        if platform.lower() != "tiktok":
            errors.append(f"第 {line_no} 行平台不是 TikTok")
            continue
        key = (platform.lower(), handle)
        if key in seen:
            errors.append(f"第 {line_no} 行重复账号 @{handle}")
            continue
        seen.add(key)
        try:
            for field in ("followers", "units_sold", "avg_views", "metrics_window_days"):
                item[field] = parse_count(item.get(field))
        except ValueError as exc:
            errors.append(f"第 {line_no} 行{exc}")
            continue
        for field in ("observed_at", "prior_invited_at"):
            if item.get(field):
                try:
                    date.fromisoformat(item[field])
                except ValueError:
                    errors.append(f"第 {line_no} 行{field}必须是 YYYY-MM-DD")
                    break
        else:
            if item["followers"] is None:
                errors.append(f"第 {line_no} 行缺少粉丝数")
                continue
            item["handle"] = handle
            item["platform"] = "TikTok"
            item["line_no"] = line_no
            if item.get("blacklist_status", "unknown") not in ("yes", "no", "unknown", ""):
                errors.append(f"第 {line_no} 行黑名单请填 yes、no 或 unknown")
                continue
            rows.append(item)
    return rows, errors


def evaluate_candidate(candidate, today=None):
    """Return outcome and explicit reasons; unknown evidence never means safe."""
    today = today or date.today()
    reasons, missing = [], []
    followers = candidate.get("followers")
    if followers is not None and not MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS:
        reasons.append(f"粉丝不在 {MIN_FOLLOWERS:,}–{MAX_FOLLOWERS:,} 范围")
    units = candidate.get("units_sold")
    views = candidate.get("avg_views")
    if units is not None and units < MIN_UNITS_SOLD:
        reasons.append("成交件数低于 100")
    if views is not None and views < MIN_AVG_VIEWS:
        reasons.append("平均播放低于 100")
    if candidate.get("blacklist_status") == "yes":
        reasons.append("黑名单")
    if candidate.get("competitor_review_status") == "conflict":
        reasons.append("已确认竞品冲突")
    if candidate.get("content_review_status") == "unfit":
        reasons.append("内容不契合")
    if candidate.get("audience_review_status") == "unfit":
        reasons.append("受众不契合")
    invited_at = candidate.get("prior_invited_at")
    if invited_at:
        try:
            if date.fromisoformat(invited_at) >= today - timedelta(days=30):
                reasons.append("近 30 天已邀约")
        except ValueError:
            missing.append("有效的上次邀约日期")
    if reasons:
        return "excluded", reasons
    for field, label in (
        ("units_sold", "成交件数"), ("avg_views", "平均播放"),
        ("profile_url", "主页 URL"),
        ("content_evidence_url", "近期内容证据"),
        ("audience_evidence_url", "受众证据"), ("observed_at", "观察日期"),
        ("personalization_hook", "个性化切入点"),
    ):
        if candidate.get(field) in (None, ""):
            missing.append(label)
    if not candidate.get("evidence_url") and not candidate.get("source_ref"):
        missing.append("后台数据证据 URL 或来源截图编号")
    window = candidate.get("metrics_window_days")
    if window == 28:
        if candidate.get("metrics_review_status") != "accepted_28d":
            missing.append("28 天代理口径，待人工核验")
    elif window != METRIC_WINDOW_DAYS:
        missing.append("同一近 30 天统计周期")
    observed_at = candidate.get("observed_at")
    if observed_at:
        try:
            observed_date = date.fromisoformat(observed_at)
            if observed_date > today or observed_date < today - timedelta(days=30):
                missing.append("近 30 天内的观察日期")
        except ValueError:
            missing.append("有效的观察日期")
    if candidate.get("blacklist_status") not in ("yes", "no"):
        missing.append("黑名单核查")
    if not candidate.get("invite_history_checked"):
        missing.append("历史邀约核查")
    if candidate.get("competitor_review_status") not in ("clear", "conflict"):
        missing.append("竞品核查")
    if candidate.get("content_review_status") not in ("fit", "unfit"):
        missing.append("内容契合核查")
    if candidate.get("audience_review_status") not in ("fit", "unfit"):
        missing.append("受众契合核查")
    return ("pending", missing) if missing else ("ready_for_review", ["基础门槛及证据齐全，待人工审核"])


def percentile_scores(candidates):
    """Weighted percentiles within each comparable 28/30-day cohort."""
    def rank(value, sorted_values):
        if len(sorted_values) <= 1:
            return 50.0
        lower = sum(x < value for x in sorted_values)
        equal = sum(x == value for x in sorted_values)
        return 100.0 * (lower + (equal - 1) / 2) / (len(sorted_values) - 1)
    scores = {}
    for window in (28, 30):
        comparable = [item for item in candidates if item.get("metrics_window_days") == window
                      and all(item.get(key) is not None for key in ("units_sold", "avg_views", "followers"))
                      and evaluate_candidate(item)[0] != "excluded"]
        values = {key: sorted(item[key] for item in comparable)
                  for key in ("units_sold", "avg_views", "followers")}
        for item in comparable:
            scores[item["handle"]] = round(
                0.5 * rank(item["units_sold"], values["units_sold"])
                + 0.3 * rank(item["avg_views"], values["avg_views"])
                + 0.2 * rank(item["followers"], values["followers"]), 1)
    return scores
