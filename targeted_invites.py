"""Validation rules for manually operated TikTok Shop targeted invitations."""

from datetime import date, datetime, timedelta


def parse_batch_terms(data):
    products = [line.strip() for line in (data.get("products") or "").splitlines() if line.strip()]
    if not 1 <= len(products) <= 15 or len({p.casefold() for p in products}) != len(products):
        raise ValueError("商品清单需有 1–15 个不重复的商品，按后台销量顺序填写")
    if any(len(product) > 200 for product in products):
        raise ValueError("单个商品名称或编号过长")
    try:
        commission = float(data.get("commission_percent") or "")
    except ValueError as exc:
        raise ValueError("请填写佣金比例") from exc
    if not 0 < commission <= 100:
        raise ValueError("佣金比例需大于 0 且不超过 100%")
    try:
        starts = date.fromisoformat(data.get("starts_on") or "")
        ends = date.fromisoformat(data.get("ends_on") or "")
    except ValueError as exc:
        raise ValueError("请填写有效的开始和结束日期") from exc
    if ends <= starts or ends < date.today():
        raise ValueError("合作结束日期必须晚于开始日期且尚未过期")
    sample_rule = (data.get("sample_rule") or "").strip()[:1000]
    if not sample_rule:
        raise ValueError("请填写样品审批规则")
    return products, commission, starts.isoformat(), ends.isoformat(), sample_rule


def parse_sent_at(value):
    try:
        sent_at = datetime.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValueError("请填写实际发送时间") from exc
    if sent_at > datetime.now() + timedelta(minutes=5):
        raise ValueError("实际发送时间不能在未来")
    return sent_at.isoformat(timespec="minutes")


def followup_due(item, now=None):
    now = now or datetime.now()
    if not item["sent_at"] or item["response_status"] != "none" or item["followed_up_at"]:
        return False
    return datetime.fromisoformat(item["sent_at"]) <= now - timedelta(days=7)
