import gc
import importlib.util
import io
import re
import tempfile
import unittest
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from xml.sax.saxutils import escape


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("fastmoss_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def synthetic_fastmoss_xlsx():
    headers = ["达人昵称", "达人ID", "Tiktok达人详情", "粉丝总量", "近28天销量",
               "近28天带货视频平均播放量", "国家/地区", "达人分类", "带货倾向",
               "FastMoss达人详情页"]
    rows = [headers,
            ["样例美妆", "sample.beauty", "https://www.tiktok.com/@sample.beauty", "5000", "230",
             "1200", "越南", "美妆", "美妆,个护", "https://www.fastmoss.com/detail/1"],
            ["样例非美妆", "sample.other", "https://www.tiktok.com/@sample.other", "6000", "250",
             "1400", "越南", "服饰", "服饰", "https://www.fastmoss.com/detail/2"],
            ["样例小号", "sample.tiny", "https://www.tiktok.com/@sample.tiny", "500", "250",
             "1400", "越南", "美妆", "美妆", "https://www.fastmoss.com/detail/3"]]
    sheet_rows = []
    for row_no, row in enumerate(rows, 1):
        cells = "".join(f'<c r="{chr(65 + col)}{row_no}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                        for col, value in enumerate(row))
        sheet_rows.append(f'<row r="{row_no}">{cells}</row>')
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>' + "".join(sheet_rows) + '</sheetData></worksheet>')
    workbook = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="FastMoss" sheetId="1" r:id="rId1"/></sheets></workbook>')
    relations = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relations)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


class FastMossImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DATA_DIR = Path(self.tmp.name) / "data"
        app.DB_PATH = app.DATA_DIR / "workspace.sqlite3"
        app.init_db()
        ts = app.now_iso()
        with app.db() as conn:
            self.campaign_id = conn.execute(
                "INSERT INTO campaigns (name, created_at, updated_at) VALUES (?, ?, ?)",
                ("全球美妆候选池", ts, ts),
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

    def test_xlsx_preview_does_not_write_and_confirmed_import_keeps_pending(self):
        boundary = "fastmoss-test-boundary"
        parts = []
        for name, value in (("campaign_id", str(self.campaign_id)), ("observed_at", date.today().isoformat())):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"xlsx_file\"; filename=\"sample.xlsx\"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".encode()
                     + synthetic_fastmoss_xlsx() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self.base + "/targeted-screening/xlsx-preview", b"".join(parts),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        page = urllib.request.urlopen(request).read().decode("utf-8")
        self.assertIn("共 3 条", page)
        self.assertIn("美妆标签或倾向 1 条", page)
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0], 0)
        token = re.search(r'name="preview_token" value="([^"]+)"', page).group(1)
        body = urllib.parse.urlencode({"preview_token": token, "mode": "beauty_numeric", "confirmed": "1"}).encode()
        page = urllib.request.urlopen(self.base + "/targeted-screening/xlsx-import", data=body).read().decode("utf-8")
        self.assertIn("FastMoss 候选已录入 1 条", page)
        self.assertIn("28 天代理口径，待人工核验", page)
        with app.db() as conn:
            creators = conn.execute("SELECT * FROM creators").fetchall()
        self.assertEqual(len(creators), 1)
        self.assertEqual(creators[0]["handle_key"], "sample.beauty")
        self.assertEqual(creators[0]["country"], "VN")
        self.assertEqual(creators[0]["source_country"], "越南")
        self.assertEqual(creators[0]["metrics_window_days"], 28)
        self.assertEqual(creators[0]["metrics_review_status"], "unknown")
        self.assertEqual(creators[0]["outreach_status"], "to_contact")
        body = urllib.parse.urlencode({"metrics_window_days": "30"}).encode()
        page = urllib.request.urlopen(
            self.base + f"/targeted-screening/candidates/{creators[0]['id']}", data=body
        ).read().decode("utf-8")
        self.assertIn("不能直接改标为 30 天", page)
        with app.db() as conn:
            window = conn.execute("SELECT metrics_window_days FROM creators WHERE id=?", (creators[0]["id"],)).fetchone()[0]
        self.assertEqual(window, 28)

    def test_large_candidate_pool_is_paginated(self):
        ts = app.now_iso()
        with app.db() as conn:
            conn.executemany(
                """INSERT INTO creators
                   (name, platform, campaign_id, handle_key, followers, metrics_window_days,
                    outreach_status, created_at, updated_at)
                   VALUES (?, 'TikTok', ?, ?, 5000, 28, 'to_contact', ?, ?)""",
                [(f"@sample.{n:03d}", self.campaign_id, f"sample.{n:03d}", ts, ts)
                 for n in range(101)],
            )
        page = urllib.request.urlopen(
            self.base + f"/targeted-screening?campaign_id={self.campaign_id}&page=2"
        ).read().decode("utf-8")
        self.assertIn("第 2 / 2 页", page)
        self.assertIn("sample.100", page)
        self.assertNotIn("sample.000</a>", page)


if __name__ == "__main__":
    unittest.main()
