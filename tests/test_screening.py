import importlib.util
import gc
import tempfile
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("crm_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class ScreeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.tmp.name) / "data"
        app.DB_PATH = app.DATA_DIR / "workspace.sqlite3"
        app.init_db()
        ts = app.now_iso()
        with app.db() as conn:
            self.campaign_id = conn.execute(
                """INSERT INTO campaigns (name, created_at, updated_at) VALUES (?, ?, ?)""",
                ("测试项目", ts, ts),
            ).lastrowid
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # The upstream db() helper uses sqlite3 connection context managers
        # (commit/rollback only); collect closed-over connections on Windows.
        gc.collect()
        self.tmp.cleanup()

    def page(self, path):
        return urllib.request.urlopen(self.base + path).read().decode("utf-8")

    def post(self, data):
        body = urllib.parse.urlencode(data).encode("utf-8")
        return urllib.request.urlopen(self.base + "/screening/candidates", data=body).read().decode("utf-8")

    def test_screening_excludes_unverified_and_conflict(self):
        self.post({
            "campaign_id": self.campaign_id, "name": "@sa_beauty", "platform": "TikTok",
            "profile_url": "https://www.tiktok.com/@sa_beauty",
            "country": "SA", "followers": "486", "content_tags": "美妆, 眼妆",
            "screening_status": "verified", "evidence_url": "https://example.com/evidence",
            "observed_at": "2026-09-23",
        })
        self.post({
            "campaign_id": self.campaign_id, "name": "@ae_beauty", "platform": "TikTok",
            "profile_url": "https://www.tiktok.com/@ae_beauty",
            "country": "AE", "followers": "500", "content_tags": "美妆",
            "screening_status": "verified", "evidence_url": "https://example.com/ae",
            "observed_at": "2026-09-23",
        })
        self.post({
            "campaign_id": self.campaign_id, "name": "@unknown", "platform": "TikTok",
            "country": "SA", "followers": "650", "content_tags": "美妆",
            "screening_status": "unverified",
        })
        self.post({
            "campaign_id": self.campaign_id, "name": "@competitor", "platform": "TikTok",
            "country": "SA", "followers": "700", "content_tags": "美妆",
            "screening_status": "verified", "evidence_url": "https://example.com/conflict",
            "observed_at": "2026-09-23", "competitor_conflict": "1",
        })
        page = self.page("/screening")
        self.assertIn("@sa_beauty", page)
        self.assertNotIn("@ae_beauty", page)
        self.assertNotIn("@unknown", page)
        self.assertNotIn("@competitor", page)
        self.assertIn("筛选结果：1 条", page)
        self.assertIn("@unknown", self.page("/screening?verification=unverified&min=&max=&country=ALL&no_conflict=0"))

    def test_verified_requires_evidence_and_prevents_duplicate(self):
        invalid = self.post({
            "campaign_id": self.campaign_id, "name": "@no_evidence", "country": "SA",
            "followers": "450", "screening_status": "verified",
        })
        self.assertIn("已核验需要", invalid)
        self.assertEqual(0, len(self._creators()))
        creator = {
            "campaign_id": self.campaign_id, "name": "@once",
            "profile_url": "https://www.tiktok.com/@once",
            "screening_status": "unverified",
        }
        self.post(creator)
        self.post(creator)
        self.assertEqual(1, len(self._creators()))

    def _creators(self):
        with app.db() as conn:
            return conn.execute("SELECT id FROM creators").fetchall()


if __name__ == "__main__":
    unittest.main()
