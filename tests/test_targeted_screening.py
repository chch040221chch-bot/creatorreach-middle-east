import gc
import importlib.util
import tempfile
import unittest
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("targeted_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class TargetedScreeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.tmp.name) / "data"
        app.DB_PATH = app.DATA_DIR / "workspace.sqlite3"
        app.init_db()
        ts = app.now_iso()
        with app.db() as conn:
            self.campaign_id = conn.execute(
                "INSERT INTO campaigns (name, created_at, updated_at) VALUES (?, ?, ?)",
                ("测试站内定邀", ts, ts),
            ).lastrowid
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        gc.collect()
        self.tmp.cleanup()

    def post(self, path, data):
        body = urllib.parse.urlencode(data).encode("utf-8")
        return urllib.request.urlopen(self.base + path, data=body).read().decode("utf-8")

    def count(self):
        with app.db() as conn:
            return conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]

    def test_preview_import_and_duplicate_without_url(self):
        batch = "账号\t粉丝\t成交件数\t平均播放\t统计天数\t来源截图\n@a.b\t1.2万\t130\t250\t30\t截图-01\n@tiny\t500\t110\t120\t30\t截图-02"
        page = self.post("/targeted-screening/preview", {"campaign_id": self.campaign_id, "candidates": batch})
        self.assertIn("@a.b", page)
        self.assertIn("粉丝不在", page)
        self.assertEqual(self.count(), 0)
        page = self.post("/targeted-screening/import", {"campaign_id": self.campaign_id, "candidates": batch})
        self.assertEqual(self.count(), 2)
        self.assertIn("待核验", page)
        with app.db() as conn:
            source = conn.execute("SELECT source_ref FROM creators WHERE handle_key='a.b'").fetchone()[0]
        self.assertEqual(source, "截图-01")
        self.post("/targeted-screening/import", {"campaign_id": self.campaign_id, "candidates": batch})
        self.assertEqual(self.count(), 2)

    def test_recent_invite_and_manual_checks(self):
        batch = "账号\t粉丝\t成交件数\t平均播放\t统计天数\n@beauty.t\t1200\t130\t250\t30"
        self.post("/targeted-screening/import", {"campaign_id": self.campaign_id, "candidates": batch})
        with app.db() as conn:
            creator_id = conn.execute("SELECT id FROM creators WHERE handle_key='beauty.t'").fetchone()[0]
        prior_date = (date.today() - timedelta(days=5)).isoformat()
        common = {
            "profile_url": "https://www.tiktok.com/@beauty.t",
            "evidence_url": "https://example.com/data",
            "content_evidence_url": "https://example.com/content",
            "audience_evidence_url": "https://example.com/audience",
            "observed_at": date.today().isoformat(),
            "units_sold": 130, "avg_views": 250, "metrics_window_days": 30,
            "invite_history_checked": 1, "blacklist_status": "no",
            "competitor_review_status": "clear",
            "content_review_status": "fit", "audience_review_status": "fit",
            "personalization_hook": "已核验的眼妆短视频展示方式",
        }
        page = self.post(f"/targeted-screening/candidates/{creator_id}", {**common, "prior_invited_at": prior_date})
        self.assertIn("近 30 天已邀约", page)
        page = self.post(f"/targeted-screening/candidates/{creator_id}", {**common, "prior_invited_at": ""})
        self.assertIn("待人工审核", page)
        self.assertNotIn("已发送邀约", page)

    def test_candidate_queue_filters_by_status_and_handle(self):
        batch = "账号\t粉丝\t成交件数\t平均播放\t统计天数\n@beauty.pending\t5000\t150\t500\t30\n@beauty.excluded\t500\t150\t500\t30"
        self.post("/targeted-screening/import", {"campaign_id": self.campaign_id, "candidates": batch})
        query = urllib.parse.urlencode({"campaign_id": self.campaign_id, "status": "excluded", "q": "beauty"})
        page = urllib.request.urlopen(self.base + f"/targeted-screening?{query}").read().decode("utf-8")
        self.assertIn("已录入候选（当前显示 1 / 共 2）", page)
        self.assertIn("beauty.excluded</a>", page)
        self.assertNotIn("beauty.pending</a>", page)
        self.assertIn("粉丝不在", page)
        self.assertIn("核验状态", page)

    def test_parser_rejects_ambiguous_rows_and_score_requires_same_window(self):
        rows, errors = app.parse_candidates("| 账号 | 粉丝 | 成交件数 | 平均播放 | 统计天数 |\n|---|---|---|---|---|\n| @m | 1万 | 100 | 120 | 30 |")
        self.assertEqual(len(rows), 0)
        self.assertTrue(errors)
        rows, errors = app.parse_candidates("账号,粉丝,成交件数,平均播放,统计天数\n@beauty.one,1万,100,120,30\n@beauty.two,2万,300,500,7")
        self.assertEqual(errors, [])
        scores = app.percentile_scores(rows)
        self.assertIn("beauty.one", scores)
        self.assertNotIn("beauty.two", scores)
        rows, errors = app.parse_candidates(
            "账号,粉丝,主页 URL\n@beauty.one,1000,https://www.tiktok.com/@someone.else"
        )
        self.assertEqual(rows, [])
        self.assertIn("不一致", errors[0])

    def test_targeted_creator_cannot_be_marked_sent_without_record(self):
        batch = "账号\t粉丝\n@beauty.sent\t2000"
        self.post("/targeted-screening/import", {"campaign_id": self.campaign_id, "candidates": batch})
        with app.db() as conn:
            creator_id = conn.execute("SELECT id FROM creators WHERE handle_key='beauty.sent'").fetchone()[0]
        page = self.post(f"/creators/{creator_id}/action", {"action": "sent", "redirect_to": "/targeted-screening"})
        self.assertIn("不能直接标记已发送", page)
        with app.db() as conn:
            status = conn.execute("SELECT outreach_status FROM creators WHERE id=?", (creator_id,)).fetchone()[0]
        self.assertEqual(status, "to_contact")

    def test_legacy_screening_dedupes_handle_without_profile_url(self):
        data = {"campaign_id": self.campaign_id, "name": "@same.handle", "followers": "800"}
        self.post("/screening/candidates", data)
        self.post("/screening/candidates", data)
        self.assertEqual(self.count(), 1)

    def test_threshold_boundary_and_missing_period_are_explained(self):
        base = {"followers": 100_000, "units_sold": 100, "avg_views": 100}
        outcome, reasons = app.evaluate_candidate(base)
        self.assertEqual(outcome, "pending")
        self.assertIn("同一近 30 天统计周期", reasons)
        outcome, reasons = app.evaluate_candidate({**base, "followers": 100_001})
        self.assertEqual(outcome, "excluded")
        self.assertIn("粉丝不在", reasons[0])

    def test_28_day_proxy_needs_explicit_human_review(self):
        row = {
            "handle": "beauty.proxy", "followers": 5000, "units_sold": 200, "avg_views": 1000,
            "metrics_window_days": 28, "metrics_review_status": "unknown",
            "profile_url": "https://www.tiktok.com/@beauty.proxy",
            "evidence_url": "https://example.com/data", "content_evidence_url": "https://example.com/video",
            "audience_evidence_url": "https://example.com/audience", "observed_at": date.today().isoformat(),
            "blacklist_status": "no", "invite_history_checked": 1,
            "competitor_review_status": "clear", "content_review_status": "fit",
            "audience_review_status": "fit", "personalization_hook": "已核验的眼妆演示",
        }
        outcome, reasons = app.evaluate_candidate(row)
        self.assertEqual(outcome, "pending")
        self.assertIn("28 天代理口径，待人工核验", reasons)
        self.assertIn("beauty.proxy", app.percentile_scores([row]))
        outcome, _ = app.evaluate_candidate({**row, "metrics_review_status": "accepted_28d"})
        self.assertEqual(outcome, "ready_for_review")


if __name__ == "__main__":
    unittest.main()
