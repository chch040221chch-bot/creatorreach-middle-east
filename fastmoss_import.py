"""Read a FastMoss creator export using only Python's standard library."""

import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from targeted_screening import normalize_handle, parse_count


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_XLSX_BYTES = 8_000_000
MAX_XML_BYTES = 40_000_000
MAX_ROWS = 10_000

REQUIRED_HEADERS = (
    "达人ID", "Tiktok达人详情", "粉丝总量", "近28天销量",
    "近28天带货视频平均播放量", "国家/地区", "达人分类", "带货倾向",
    "FastMoss达人详情页",
)

COUNTRY_CODES = {"越南": "VN", "泰国": "TH", "沙特阿拉伯": "SA", "阿联酋": "AE"}


def _column_index(cell_reference):
    letters = re.match(r"[A-Z]+", cell_reference or "")
    if not letters:
        return None
    index = 0
    for letter in letters.group():
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _xlsx_rows(blob):
    if len(blob) > MAX_XLSX_BYTES:
        raise ValueError("Excel 文件超过 8 MB 上限")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 .xlsx") from exc
    with archive:
        xml_files = [part for part in archive.infolist() if part.filename.endswith(".xml")]
        if any(part.file_size > MAX_XML_BYTES for part in xml_files):
            raise ValueError("Excel 内容过大")
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError("Excel 工作簿结构无效") from exc
        sheets = workbook.find(f"{{{MAIN}}}sheets")
        sheet = next((item for item in sheets if item.get("name") == "FastMoss"), None) if sheets is not None else None
        if sheet is None:
            raise ValueError("找不到 FastMoss 工作表")
        relation_id = sheet.get(f"{{{DOC_REL}}}id")
        relation = next((item for item in relationships if item.get("Id") == relation_id), None)
        if relation is None:
            raise ValueError("FastMoss 工作表链接无效")
        target = relation.get("Target", "")
        sheet_path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        if not sheet_path.startswith("xl/worksheets/"):
            raise ValueError("FastMoss 工作表路径无效")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                strings_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            except ET.ParseError as exc:
                raise ValueError("Excel 文本表无效") from exc
            shared = ["".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t"))
                      for item in strings_root.findall(f"{{{MAIN}}}si")]
        try:
            sheet_root = ET.fromstring(archive.read(sheet_path))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError("FastMoss 工作表内容无效") from exc
        result = []
        for row in sheet_root.findall(f".//{{{MAIN}}}sheetData/{{{MAIN}}}row"):
            if len(result) >= MAX_ROWS + 1:
                raise ValueError("Excel 超过 10,000 条记录上限")
            cells = {}
            for cell in row.findall(f"{{{MAIN}}}c"):
                index = _column_index(cell.get("r"))
                if index is None or index > 100:
                    continue
                value_node = cell.find(f"{{{MAIN}}}v")
                if cell.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter(f"{{{MAIN}}}t"))
                elif value_node is None:
                    value = ""
                elif cell.get("t") == "s":
                    try:
                        value = shared[int(value_node.text or "")]
                    except (IndexError, ValueError) as exc:
                        raise ValueError("Excel 共享文本索引无效") from exc
                else:
                    value = value_node.text or ""
                cells[index] = value
            if cells:
                values = [""] * (max(cells) + 1)
                for index, value in cells.items():
                    values[index] = value
                result.append((int(row.get("r") or len(result) + 1), values))
    return result


def read_fastmoss_export(blob, filename, observed_at):
    """Return normalized candidates and row errors; never write to a database."""
    try:
        raw_rows = _xlsx_rows(blob)
    except zipfile.BadZipFile as exc:
        raise ValueError("Excel 压缩包已损坏") from exc
    if not raw_rows:
        raise ValueError("FastMoss 工作表为空")
    header = [str(value or "").strip() for value in raw_rows[0][1]]
    missing = [name for name in REQUIRED_HEADERS if name not in header]
    if missing:
        raise ValueError("缺少 FastMoss 字段：" + "、".join(missing))
    basename = re.split(r"[/\\]", filename or "FastMoss.xlsx")[-1][:120]
    candidates, errors = [], []
    for excel_row, values in raw_rows[1:]:
        if not any(str(value or "").strip() for value in values):
            continue
        data = dict(zip(header, values))
        handle = normalize_handle(str(data.get("达人ID") or ""), str(data.get("Tiktok达人详情") or ""))
        if not handle:
            errors.append(f"第 {excel_row} 行没有可核实的 TikTok 账号")
            continue
        entered_handle = normalize_handle(str(data.get("达人ID") or ""))
        if entered_handle and entered_handle != handle:
            errors.append(f"第 {excel_row} 行达人 ID 与 TikTok 主页账号不一致")
            continue
        try:
            followers = parse_count(data.get("粉丝总量"))
            units_sold = parse_count(data.get("近28天销量"))
            avg_views = parse_count(data.get("近28天带货视频平均播放量"))
        except ValueError as exc:
            errors.append(f"第 {excel_row} 行数字无效：{exc}")
            continue
        category = str(data.get("达人分类") or "").strip()
        tendency = str(data.get("带货倾向") or "").strip()
        country_name = str(data.get("国家/地区") or "").strip()
        candidates.append({
            "handle": handle, "platform": "TikTok", "profile_url": str(data.get("Tiktok达人详情") or "").strip(),
            "display_name": str(data.get("达人昵称") or "").strip(),
            "followers": followers, "units_sold": units_sold, "avg_views": avg_views,
            "metrics_window_days": 28, "metrics_review_status": "unknown",
            "metric_source": "FastMoss", "view_metric_type": "近28天带货视频平均播放量",
            "evidence_url": str(data.get("FastMoss达人详情页") or "").strip(),
            "observed_at": observed_at, "source_ref": f"{basename} 第{excel_row}行",
            "country": COUNTRY_CODES.get(country_name, ""), "source_country": country_name,
            "content_tags": ", ".join(part for part in (category, tendency) if part)[:250],
            "beauty_signal": "美妆" in category or "美妆" in tendency,
            "line_no": excel_row,
        })
    return candidates, errors
