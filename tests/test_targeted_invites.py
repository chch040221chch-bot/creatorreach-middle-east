import gc
import importlib.util
import json
import tempfile
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("targeted_invite_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class TargetedInviteWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.tmp.name) / "data"
        app.DB_PATH = app.DATA_DIR / "workspace.sqlite3"
        app.init_db()
        ts = app.now_iso()
        with app.db() as conn:
            self.campaign_id = conn.execute(
                "INSERT INTO campaigns (name, created_at, updated_at) VALUES (?, ?, ?)",
                ("虚构定邀项目", ts, ts),
            ).lastrowid
            self.creator_ids = []
            for index in (1, 2):
                self.creator_ids.append(conn.execute(
                    """INSERT INTO creators
                    (name, platform, campaign_id, handle_key, profile_url, followers, units_sold,
                     avg_views, metrics_window_days, evidence_url, content_evidence_url,
                     audience_evidence_url, observed_at, invite_history_checked,
                     blacklist_status, competitor_review_status, content_review_status,
                     audience_review_status, personalization_hook, outreach_status,
                     created_at, updated_at)
                    VALUES (?, 'TikTok', ?, ?, ?, 5000, 250, 3000, 30, ?, ?, ?, ?, 1,
                            'no', 'clear', 'fit', 'fit', ?, 'to_contact', ?, ?)""",
                    (f"@sample.{index}", self.campaign_id, f"sample.{index}",
                     f"https://www.tiktok.com/@sample.{index}", "https://example.com/data",
                     f"https://example.com/content/{index}", "https://example.com/audience",
                     datetime.now().date().isoformat(), f"近期眼妆演示 #{index}", ts, ts),
                ).lastrowid)
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

    def post(self, path, fields):
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        return urllib.request.urlopen(self.base + path, data=body).read().decode("utf-8")

    def batch_fields(self):
        today = datetime.now().date()
        return {
            "campaign_id": str(self.campaign_id), "creator_id": [str(x) for x in self.creator_ids],
            "products": "商品 A\n商品 B", "commission_percent": "10",
            "starts_on": today.isoformat(), "ends_on": (today + timedelta(days=30)).isoformat(),
            "sample_rule": "寄样须人工批准",
        }

    def create_batch(self):
        page = self.post("/targeted-invites", self.batch_fields())
        self.assertIn("尚未发送", page)
        with app.db() as conn:
            batch = conn.execute("SELECT * FROM targeted_invite_batches").fetchone()
            items = conn.execute("SELECT * FROM targeted_invite_items ORDER BY id").fetchall()
        return batch, items

    def test_manual_review_send_and_seven_day_followup(self):
        batch, items = self.create_batch()
        self.assertEqual(json.loads(batch["products_json"]), ["商品 A", "商品 B"])
        self.assertEqual(json.loads(items[0]["candidate_snapshot_json"])["units_sold"], 250)
        self.assertEqual(batch["status"], "draft")
        self.assertTrue(all(item["sent_at"] is None for item in items))
        premature = self.post(f"/targeted-invites/{batch['id']}/items/{items[0]['id']}/sent", {
            "sent_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "operator": "测试员", "reference": "邀请-001", "confirmed": "1",
        })
        self.assertIn("必须先审核", premature)
        for item in items:
            self.post(f"/targeted-invites/{batch['id']}/items/{item['id']}/review", {
                "draft_text": f"Hello @{item['creator_id']}, I saw your verified eye makeup demo.",
                "reviewer": "审核员", "review_action": "approve",
            })
        page = self.post(f"/targeted-invites/{batch['id']}/approve", {"reviewer": "负责人"})
        self.assertIn("请在 TikTok Shop 人工发送", page)
        missing_reference = self.post(f"/targeted-invites/{batch['id']}/items/{items[0]['id']}/sent", {
            "sent_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "operator": "测试员", "confirmed": "1",
        })
        self.assertIn("填写操作者与凭据", missing_reference)
        self.post(f"/targeted-invites/{batch['id']}/items/{items[0]['id']}/sent", {
            "sent_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "operator": "测试员", "reference": "邀请-001", "confirmed": "1",
        })
        with app.db() as conn:
            sent = conn.execute("SELECT * FROM targeted_invite_items WHERE id=?", (items[0]["id"],)).fetchone()
            creator = conn.execute("SELECT outreach_status, prior_invited_at FROM creators WHERE id=?", (items[0]["creator_id"],)).fetchone()
            event_types = [row[0] for row in conn.execute("SELECT event_type FROM targeted_invite_events WHERE item_id=?", (items[0]["id"],))]
        self.assertIsNotNone(sent["sent_at"])
        self.assertEqual(creator["outreach_status"], "sent")
        self.assertEqual(creator["prior_invited_at"], datetime.now().date().isoformat())
        self.assertIn("sent_in_tiktok_shop", event_types)
        with app.db() as conn:
            conn.execute("UPDATE targeted_invite_items SET sent_at=? WHERE id=?",
                         ((datetime.now() - timedelta(days=8)).isoformat(timespec="minutes"), items[0]["id"]))
        page = urllib.request.urlopen(self.base + "/targeted-invites").read().decode("utf-8")
        self.assertIn("七天待跟进：1 人", page)
        self.post(f"/targeted-invites/{batch['id']}/items/{items[0]['id']}/followup", {
            "operator": "测试员", "reference": "站内跟进截图-01",
        })
        page = urllib.request.urlopen(self.base + "/targeted-invites").read().decode("utf-8")
        self.assertIn("七天待跟进：0 人", page)

    def test_batch_limits_and_placeholder_review(self):
        fields = self.batch_fields()
        fields["products"] = "\n".join(f"商品 {n}" for n in range(16))
        page = self.post("/targeted-invites", fields)
        self.assertIn("1–15", page)
        fields = self.batch_fields()
        fields["creator_id"] = [str(n) for n in range(1, 52)]
        page = self.post("/targeted-invites", fields)
        self.assertIn("1–50", page)
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM targeted_invite_batches").fetchone()[0], 0)
        batch, items = self.create_batch()
        page = self.post(f"/targeted-invites/{batch['id']}/items/{items[0]['id']}/review", {
            "draft_text": "Hi [name], we offer {commission}.",
            "reviewer": "审核员", "review_action": "approve",
        })
        self.assertIn("占位符", page)
        page = self.post(f"/targeted-invites/{batch['id']}/approve", {"reviewer": "负责人"})
        self.assertIn("逐人核对", page)

    def test_changed_blacklist_blocks_batch_approval(self):
        batch, items = self.create_batch()
        for item in items:
            self.post(f"/targeted-invites/{batch['id']}/items/{item['id']}/review", {
                "draft_text": "已核对的个性化站内邀约。", "reviewer": "审核员", "review_action": "approve",
            })
        with app.db() as conn:
            conn.execute("UPDATE creators SET blacklist_status='yes' WHERE id=?", (self.creator_ids[0],))
        page = self.post(f"/targeted-invites/{batch['id']}/approve", {"reviewer": "负责人"})
        self.assertIn("核验已变化", page)
        with app.db() as conn:
            status = conn.execute("SELECT status FROM targeted_invite_batches WHERE id=?", (batch["id"],)).fetchone()[0]
        self.assertEqual(status, "draft")


if __name__ == "__main__":
    unittest.main()
