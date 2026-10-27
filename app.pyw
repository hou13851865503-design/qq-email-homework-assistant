import csv
from contextlib import contextmanager
import hashlib
import imaplib
import io
import json
import mimetypes
import os
import re
import smtplib
import socket
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
DOWNLOAD_DIR = APP_DIR / "downloads"
DB_PATH = DATA_DIR / "assistant.sqlite3"
DATA_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

SESSION = {"account": None, "auth_code": None}
SESSION_LOCK = threading.Lock()

# ---- 记住此设备（一键连接）----
# 账号与授权码用 Windows DPAPI 加密后保存在 data/session.bin，
# 只有当前 Windows 账户能解密；data/ 已在 .gitignore 中，不会进仓库。
SESSION_FILE = DATA_DIR / "session.bin"
SESSION_CACHE = {"saved_account": "", "auto_state": "idle", "auto_error": ""}


def _dpapi(data: bytes, protect: bool) -> bytes:
    """调用 Windows DPAPI 加/解密。失败抛 OSError。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, max(len(data), 1)),
        ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if protect:
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    else:
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def save_session(account: str, auth_code: str) -> None:
    payload = json.dumps({"account": account, "auth_code": auth_code}).encode("utf-8")
    DATA_DIR.mkdir(exist_ok=True)
    SESSION_FILE.write_bytes(_dpapi(payload, True))
    SESSION_CACHE["saved_account"] = account


def load_session() -> tuple[str, str]:
    try:
        data = json.loads(_dpapi(SESSION_FILE.read_bytes(), False).decode("utf-8"))
        account = str(data.get("account") or "")
        auth_code = str(data.get("auth_code") or "")
        if account and auth_code:
            SESSION_CACHE["saved_account"] = account
            return account, auth_code
    except Exception:
        pass
    return "", ""


def clear_session() -> None:
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    SESSION_CACHE["saved_account"] = ""


def try_login(account: str, auth_code: str) -> None:
    client = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=20)
    try:
        client.login(account, auth_code)
    finally:
        try:
            client.logout()
        except Exception:
            pass
MAX_ROSTER_UPLOAD = 8 * 1024 * 1024
MAX_ROSTER_ROWS = 1000
MAX_ANNOUNCEMENT_RECIPIENTS = 200

ROSTER_HEADERS = {
    "name": {"姓名", "学生姓名", "name", "studentname"},
    "student_id": {"学号", "学生学号", "学籍号", "studentid", "studentno", "studentnumber"},
    "qq_number": {"qq", "qq号", "qq号码", "qq帐号", "qq账号", "qqnumber", "qqid"},
    "group_name": {"班级", "小组", "分类", "group", "groupname", "class", "classname"},
    "email": {"邮箱", "邮件", "email", "emailaddress"},
}


def normalize_roster_header(value):
    return re.sub(r"[\s_\-（）()：:]+", "", str(value or "").strip().lower())


def split_roster_line(line):
    line = line.strip().strip("|")
    if not line:
        return []
    if "|" in line:
        return [cell.strip() for cell in line.split("|")]
    if "\t" in line:
        return [cell.strip() for cell in line.split("\t")]
    cells = re.split(r"\s{2,}", line)
    if len(cells) == 1:
        cells = re.split(r"\s+", line)
    return [cell.strip() for cell in cells if cell.strip()]


def roster_header_map(cells):
    result = {}
    for index, cell in enumerate(cells):
        normalized = normalize_roster_header(cell)
        for field, aliases in ROSTER_HEADERS.items():
            if normalized in aliases:
                result[field] = index
                break
    return result


def merge_header_rows(rows):
    """把多行表头按列合并成一行（每列取最下层非空值）。

    两级表头里，上层是"联系方式"这类分组，下层才是"QQ号/邮箱"这种具体列名，
    所以要优先取下面那行。
    """
    if not rows:
        return []
    width = max(len(row) for row in rows)
    merged = []
    for column in range(width):
        value = ""
        for row in reversed(rows):
            text = row[column] if column < len(row) else ""
            if text:
                value = text
                break
        merged.append(value)
    return merged


def parse_roster_rows(table_rows):
    header_index = None
    columns = {}
    header_span = 1
    # 表头可能占 1~3 行（两级表头、合并单元格很常见）
    for index in range(len(table_rows)):
        for span in (1, 2, 3):
            if index + span > len(table_rows):
                break
            combined = merge_header_rows(table_rows[index:index + span])
            found = roster_header_map(combined)
            if all(field in found for field in ("name", "student_id", "qq_number")):
                header_index, columns, header_span = index, found, span
                break
        if header_index is not None:
            break
    if header_index is None:
        raise ValueError("未找到同时包含姓名、学号和 QQ 号的表头，请确认表格列名后重试")

    parsed = []
    header_cells = merge_header_rows(table_rows[header_index:header_index + header_span])
    header_start = min(columns.values())
    header_end = max(columns.values()) + 1
    header_width = header_end - header_start
    inline_values = header_cells[header_end:]
    candidate_rows = []
    if inline_values and len(inline_values) % header_width == 0:
        candidate_rows.extend(
            (header_index + header_span + 1 + start // header_width, [""] * header_start + inline_values[start:start + header_width])
            for start in range(0, len(inline_values), header_width)
        )
    candidate_rows.extend(enumerate(table_rows[header_index + header_span:], start=header_index + header_span + 1))

    for offset, cells in candidate_rows:
        if not any(str(cell).strip() for cell in cells):
            continue
        repeated_header = roster_header_map(cells)
        if all(field in repeated_header for field in ("name", "student_id", "qq_number")):
            continue
        row = {field: (cells[index].strip() if index < len(cells) else "") for field, index in columns.items()}
        if not any(row.values()):
            continue
        qq = re.sub(r"\s+", "", row.get("qq_number", ""))
        student_id = re.sub(r"\s+", "", row.get("student_id", ""))
        name = re.sub(r"\s+", " ", row.get("name", "")).strip()
        error = ""
        if not name or not student_id or not qq:
            error = "姓名、学号和 QQ 号均不能为空"
        elif not re.fullmatch(r"\d{5,12}", qq):
            error = "QQ 号应为 5 至 12 位数字"
        parsed.append({
            "row_number": offset,
            "name": name,
            "student_id": student_id,
            "qq_number": qq,
            "group_name": row.get("group_name", "").strip(),
            "email": row.get("email", "").strip(),
            "error": error,
        })
        if len(parsed) > MAX_ROSTER_ROWS:
            raise ValueError(f"名单超过 {MAX_ROSTER_ROWS} 行，请拆分文件后导入")

    if not parsed:
        raise ValueError("表格中没有可导入的成员记录")
    return parsed


def parse_docx_roster(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 32 * 1024 * 1024:
                raise ValueError("Word 文档内容过大")
            root = ET.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError("无法读取此 Word 文件，请上传有效的 .docx 文档") from exc

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    val_attr = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
    tables = []
    for table in root.findall(".//w:tbl", ns):
        rows = []
        for table_row in table.findall("./w:tr", ns):
            cells = []
            for cell in table_row.findall("./w:tc", ns):
                text = " ".join(node.text or "" for node in cell.findall(".//w:t", ns)).strip()
                span = 1
                span_node = cell.find("./w:tcPr/w:gridSpan", ns)
                if span_node is not None:
                    try:
                        span = max(1, int(span_node.get(val_attr, "1") or "1"))
                    except (TypeError, ValueError):
                        span = 1
                cells.append(text)
                # 合并单元格要占多个列位，否则后面所有列都会错位
                cells.extend([""] * (span - 1))
            rows.append(cells)
        tables.append(rows)
    for table in tables:
        try:
            return parse_roster_rows(table)
        except ValueError:
            continue

    paragraphs = ["".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
                  for paragraph in root.findall(".//w:p", ns)]
    rows = [split_roster_line(line) for line in paragraphs if line.strip()]
    return parse_roster_rows(rows)


def parse_pdf_roster(content):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF 识别依赖尚未安装，请在项目目录运行：python -m pip install -r requirements.txt") from exc
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请先解除密码保护")
        if len(reader.pages) > 100:
            raise ValueError("PDF 超过 100 页，请拆分文件后导入")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取此 PDF，请确认文件未损坏或加密") from exc
    rows = [split_roster_line(line) for line in text.splitlines() if line.strip()]
    try:
        return parse_roster_rows(rows)
    except ValueError as exc:
        if not text.strip():
            raise ValueError("PDF 中没有可提取的文字；扫描图片版 PDF 暂不支持 OCR") from exc
        raise


def convert_legacy_doc(content):
    """把老版 .doc 用本机 Word 转成 .docx 字节流。

    .doc 是二进制 OLE 格式，纯 Python 解析不可靠；本机装了 Word 就借它转换。
    没有 Word 时给明确提示，而不是抛一个看不懂的错。
    """
    if os.name != "nt":
        raise ValueError("旧版 .doc 需要本机安装 Word 才能自动转换，请用 Word 另存为 .docx 后上传")
    temp_dir = Path(tempfile.mkdtemp(prefix="roster_doc_"))
    try:
        source = temp_dir / "input.doc"
        target = temp_dir / "output.docx"
        source.write_bytes(content)
        script = (
            "$ErrorActionPreference='Stop';"
            "$w=New-Object -ComObject Word.Application;"
            "$w.Visible=$false;$w.DisplayAlerts=0;"
            "try{"
            f"$d=$w.Documents.Open('{source}',$false,$true,$false);"
            f"$d.SaveAs2('{target}',16);"
            "$d.Close(0);"
            "}finally{$w.Quit()}"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=90)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("调用 Word 转换失败，请把文件另存为 .docx 后重试") from exc
        if not target.is_file():
            raise ValueError("Word 转换未生成文件，请确认本机已安装 Word，或另存为 .docx 后上传")
        return target.read_bytes()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def parse_roster_upload(filename, content):
    suffix = Path(filename).suffix.lower()
    if suffix not in (".docx", ".doc", ".pdf"):
        raise ValueError("仅支持 .docx / .doc Word 文档和 .pdf 文件")
    if not content:
        raise ValueError("文件内容为空")
    if suffix == ".doc":
        content = convert_legacy_doc(content)
        suffix = ".docx"
    rows = parse_docx_roster(content) if suffix == ".docx" else parse_pdf_roster(content)
    seen_qq = set()
    seen_student_ids = set()
    for row in rows:
        if row["qq_number"] in seen_qq:
            row["error"] = "文件中存在重复 QQ 号"
        elif row["student_id"] in seen_student_ids:
            row["error"] = "文件中存在重复学号"
        seen_qq.add(row["qq_number"])
        seen_student_ids.add(row["student_id"])
    return {"filename": Path(filename).name, "rows": rows}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS roster (
                qq_number TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                student_id TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                email_verified INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                assignment_label TEXT NOT NULL DEFAULT '',
                match_by_student_id INTEGER NOT NULL DEFAULT 1,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                due_at TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '[]',
                keyword_mode TEXT NOT NULL DEFAULT 'any',
                keyword_fields TEXT NOT NULL DEFAULT '["subject","body","filename"]',
                group_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                uid TEXT NOT NULL,
                uidvalidity TEXT NOT NULL,
                message_id TEXT NOT NULL DEFAULT '',
                from_addr TEXT NOT NULL DEFAULT '',
                from_name TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL,
                matched_qq TEXT NOT NULL DEFAULT '',
                match_method TEXT NOT NULL DEFAULT '',
                UNIQUE(task_id, uid)
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'application/pdf',
                size INTEGER NOT NULL DEFAULT 0,
                preview_sha256 TEXT NOT NULL DEFAULT '',
                stored_path TEXT NOT NULL DEFAULT '',
                actual_sha256 TEXT NOT NULL DEFAULT '',
                UNIQUE(message_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                qq_number TEXT NOT NULL,
                recipient TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(task_id, qq_number)
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                group_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS announcement_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
                qq_number TEXT NOT NULL,
                name TEXT NOT NULL,
                group_name TEXT NOT NULL DEFAULT '',
                recipient TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                UNIQUE(announcement_id, qq_number)
            );
        """)
        roster_columns = {row[1] for row in conn.execute("PRAGMA table_info(roster)")}
        if "student_id" not in roster_columns:
            conn.execute("ALTER TABLE roster ADD COLUMN student_id TEXT NOT NULL DEFAULT ''")
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "assignment_label" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN assignment_label TEXT NOT NULL DEFAULT ''")
        if "match_by_student_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN match_by_student_id INTEGER NOT NULL DEFAULT 1")
        if "auto_remind" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN auto_remind INTEGER NOT NULL DEFAULT 0")
        if "auto_reminded_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN auto_reminded_at TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_roster_student_id ON roster(student_id) WHERE student_id<>''")


def decoded(value):
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def local_date(parsed):
    if parsed is None:
        return datetime.now().astimezone()
    try:
        if parsed.tzinfo is None:
            return parsed.astimezone()
        return parsed.astimezone()
    except Exception:
        return datetime.now().astimezone()


def iter_attachment_parts(message):
    """遍历邮件里的附件（任意类型：PDF / Word / 压缩包等）。

    ordinal 只对"合格分部"递增，扫描与下载两处必须用同一套规则，
    否则按 ordinal 定位附件会错位。
    """
    ordinal = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = decoded(part.get_filename())
        disposition = (part.get_content_disposition() or "").lower()
        if not filename and disposition != "attachment":
            continue
        if not filename:
            ext = (mimetypes.guess_extension(part.get_content_type() or "") or "").lower()
            if ext in (".jpe", ".jpeg"):
                ext = ".jpg"
            filename = "附件" + ext
        yield ordinal, filename.strip(), part.get_content_type(), (part.get_payload(decode=True) or b"")
        ordinal += 1


def attachment_parts(message):
    """邮件中的所有附件；不看扩展名，只按文件名参与匹配。"""
    return [{
        "ordinal": ordinal,
        "filename": filename,
        "content_type": content_type,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    } for ordinal, filename, content_type, payload in iter_attachment_parts(message)]


def message_text(message):
    chunks = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() != "text/plain":
            continue
        try:
            content = part.get_content()
            if isinstance(content, str):
                chunks.append(content)
        except Exception:
            payload = part.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(chunks)


def clean_filename(name):
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "作业"


def clean_label(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    return name[:90] or "作业统计"


def identifier_in_text(identifier, text):
    token = str(identifier).strip()
    if not token:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def resolve_member(roster, from_addr, identity_texts, match_by_student_id):
    sender_member = None
    sender_method = ""
    for member in roster:
        if member["email"] and member["email"].lower() == from_addr:
            sender_member, sender_method = member, "登记邮箱"
            break
        numeric_sender = re.fullmatch(r"(\d{5,12})@qq\.com", from_addr)
        if numeric_sender and numeric_sender.group(1) == member["qq_number"]:
            sender_member, sender_method = member, "QQ号与发件地址一致"
            break
    if not match_by_student_id:
        return (sender_member, sender_method) if sender_member else (None, "未匹配到登记邮箱或QQ号")

    student_matches = [
        member for member in roster
        if member["student_id"] and any(identifier_in_text(member["student_id"], text) for text in identity_texts)
    ]
    if len(student_matches) > 1:
        return None, "多个学号，待确认"
    if student_matches:
        student_member = student_matches[0]
        if sender_member and sender_member["qq_number"] != student_member["qq_number"]:
            return None, "发件人与学号不一致"
        return student_member, "学号" if not sender_member else sender_method + "+学号"
    return (sender_member, sender_method) if sender_member else (None, "未匹配到登记邮箱、QQ号或学号")


def desktop_directory():
    if os.name == "nt":
        import ctypes
        folder = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.shell32.SHGetFolderPathW(0, 0x10, 0, 0, folder)
        if result == 0 and folder.value:
            return Path(folder.value)
    return Path.home() / "Desktop"


def open_folder(path):
    """在资源管理器里打开文件夹（失败不影响主流程）。"""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def export_folder_for(task):
    """在桌面新建一个不重名的导出文件夹；桌面不可写时退回「下载」目录。"""
    desktop = desktop_directory()
    try:
        desktop.mkdir(parents=True, exist_ok=True)
        probe = desktop / ".write_test"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
    except Exception:
        desktop = Path.home() / "Downloads"
        desktop.mkdir(parents=True, exist_ok=True)
    base = "{0}_至_{1}_{2}_{3}".format(
        task["start_date"], task["end_date"],
        clean_label(task["name"]), datetime.now().strftime("%Y%m%d_%H%M%S"))
    target = desktop / base
    suffix = 2
    while target.exists():
        target = desktop / f"{base}_{suffix}"
        suffix += 1
    target.mkdir(parents=True, exist_ok=True)
    return target


def copy_into_folder(items, folder):
    """把下载好的文件（连同 .source.json 来源清单）复制到目标文件夹。"""
    copied = 0
    for item in items:
        source = Path(item["path"])
        if not source.is_file():
            continue
        try:
            shutil.copy2(source, folder / source.name)
            copied += 1
        except Exception:
            continue
        manifest = source.with_name(source.name + ".source.json")
        if manifest.is_file():
            try:
                shutil.copy2(manifest, folder / manifest.name)
            except Exception:
                pass
    return copied


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_attachments(task_id, task, rows, account, auth_code):
    task_dir = DOWNLOAD_DIR / f"{task_id:04d}_{clean_label(task['name'])}"
    task_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    pending = []
    for row in rows:
        saved_path = Path(row["stored_path"]) if row["stored_path"] else None
        if saved_path and saved_path.is_relative_to(DOWNLOAD_DIR.resolve()) and saved_path.is_file() and row["actual_sha256"]:
            if file_sha256(saved_path) == row["actual_sha256"]:
                downloaded.append({"filename": saved_path.name, "path": str(saved_path), "sha256": row["actual_sha256"]})
                continue
        pending.append(row)
    if not pending:
        return downloaded

    client = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=30)
    try:
        client.login(account, auth_code)
        client.select("INBOX", readonly=True)
        current_validity_data = client.response("UIDVALIDITY")[1]
        current_validity = current_validity_data[0].decode() if current_validity_data and current_validity_data[0] else ""
        for row in pending:
            if row["uidvalidity"] != current_validity:
                raise ValueError("邮箱收件箱标识已变化，请重新扫描该日期范围后再下载")
            status, fetched = client.uid("fetch", row["uid"].encode(), "(UID RFC822)")
            if status != "OK":
                raise ValueError(f"无法从邮箱取回来源邮件 UID {row['uid']}")
            raw = next((piece for item in fetched if isinstance(item, tuple) for piece in item[1:] if isinstance(piece, bytes)), b"")
            if not raw:
                raise ValueError("来源邮件内容为空，请重新扫描")
            message = message_from_bytes(raw, policy=default)
            if str(message.get("Message-ID", "")).strip() != row["source_message_id"]:
                raise ValueError("邮件来源校验失败，请重新扫描后再下载")
            parts = [(ordinal, filename, payload)
                     for ordinal, filename, _ctype, payload in iter_attachment_parts(message)]
            selected = next((part for part in parts if part[0] == row["ordinal"]), None)
            if not selected:
                raise ValueError("所选附件在原邮件中已不存在")
            _, filename, payload = selected
            digest = hashlib.sha256(payload).hexdigest()
            if row["preview_sha256"] and digest != row["preview_sha256"]:
                raise ValueError("附件哈希校验失败，文件未写入磁盘")
            safe_name = clean_filename(filename)
            target = task_dir / safe_name
            suffix = 2
            while target.exists():
                target = task_dir / f"{Path(safe_name).stem} ({suffix}){Path(safe_name).suffix}"
                suffix += 1
            target.write_bytes(payload)
            manifest = {
                "task": task["name"], "sha256": digest, "file": target.name,
                "sender": row["from_addr"], "sender_name": row["from_name"],
                "received_at": row["received_at"], "subject": row["subject"],
                "message_id": row["source_message_id"], "imap_folder": "INBOX",
                "imap_uid": row["uid"], "imap_uidvalidity": row["uidvalidity"],
                "matched_qq": row["matched_qq"], "match_method": row["match_method"],
            }
            target.with_name(target.name + ".source.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            with db() as conn:
                conn.execute("UPDATE attachments SET stored_path=?,actual_sha256=? WHERE id=?", (str(target), digest, row["id"]))
            downloaded.append({"filename": target.name, "path": str(target), "sha256": digest})
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return downloaded


def task_row(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("没有找到这次统计任务")
    return dict(row)


def task_summary(conn, task_id):
    task = task_row(conn, task_id)
    roster_sql = "SELECT * FROM roster WHERE active=1"
    params = []
    if task["group_name"]:
        roster_sql += " AND group_name=?"
        params.append(task["group_name"])
    roster = conn.execute(roster_sql + " ORDER BY group_name, qq_number", params).fetchall()
    messages = conn.execute("SELECT * FROM messages WHERE task_id=? ORDER BY received_at", (task_id,)).fetchall()
    attachments = conn.execute("""
        SELECT a.*, m.from_addr, m.from_name, m.subject, m.received_at, m.matched_qq,
               m.match_method, m.message_id AS source_message_id, m.uid AS source_uid
        FROM attachments a JOIN messages m ON m.id=a.message_id
        WHERE m.task_id=? ORDER BY m.received_at, a.filename
    """, (task_id,)).fetchall()
    submitted = {r["matched_qq"] for r in messages if r["matched_qq"]}
    reminder_rows = conn.execute("SELECT qq_number FROM reminders WHERE task_id=?", (task_id,)).fetchall()
    reminded = {r["qq_number"] for r in reminder_rows}
    due = datetime.fromisoformat(task["due_at"]) if task["due_at"] else None
    members = []
    for member in roster:
        member_attachments = [dict(a) for a in attachments if a["matched_qq"] == member["qq_number"]]
        late = bool(due and any(datetime.fromisoformat(a["received_at"]) > due for a in member_attachments))
        members.append({
            **dict(member),
            "status": "missing" if member["qq_number"] not in submitted else ("late" if late else "submitted"),
            "files": member_attachments,
            "reminded": member["qq_number"] in reminded,
        })
    matched_messages = [dict(m) for m in messages if m["matched_qq"]]
    unmatched_messages = [dict(m) for m in messages if not m["matched_qq"]]
    return {
        "task": {**task, "keywords": json.loads(task["keywords"]), "keyword_fields": json.loads(task["keyword_fields"])},
        "members": members,
        "messages": matched_messages,
        "unmatched": unmatched_messages,
        "attachments": [dict(a) for a in attachments],
        "counts": {
            "expected": len(members),
            "submitted": sum(1 for m in members if m["status"] in ("submitted", "late")),
            "missing": sum(1 for m in members if m["status"] == "missing"),
            "late": sum(1 for m in members if m["status"] == "late"),
            "unmatched": len(unmatched_messages),
            "files": len(attachments),
        },
    }


def smtp_send(account, auth_code, recipient, subject, body):
    message = EmailMessage()
    message["From"] = account
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20) as client:
        client.login(account, auth_code)
        client.send_message(message)


def smtp_send_announcement(account, auth_code, recipients, subject, body):
    results = []
    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as client:
            client.login(account, auth_code)
            for member in recipients:
                message = EmailMessage()
                message["From"] = account
                message["To"] = member["recipient"]
                message["Subject"] = subject
                message.set_content(body)
                try:
                    refused = client.send_message(message)
                    refusal = next((reason for address, reason in refused.items() if address.lower() == member["recipient"].lower()), None)
                    if refusal:
                        results.append({"qq_number": member["qq_number"], "status": "failed", "error": str(refusal)[:500]})
                    else:
                        results.append({"qq_number": member["qq_number"], "status": "sent", "error": ""})
                except Exception as exc:
                    results.append({"qq_number": member["qq_number"], "status": "failed", "error": str(exc)[:500] or "发送失败"})
    except Exception as exc:
        error = str(exc)[:500] or "QQ 邮箱连接失败"
        completed = {result["qq_number"] for result in results}
        results.extend({"qq_number": member["qq_number"], "status": "failed", "error": error}
                       for member in recipients if member["qq_number"] not in completed)
    return results


class Handler(BaseHTTPRequestHandler):
    server_version = "ClassworkAssistant/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 2_000_000:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(size).decode("utf-8")) if size else {}

    def read_roster_upload(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("上传文件大小无效") from exc
        if size <= 0:
            raise ValueError("文件内容为空")
        if size > MAX_ROSTER_UPLOAD:
            raise ValueError("文件不能超过 8 MB")
        content = self.rfile.read(size)
        if len(content) != size:
            raise ValueError("文件上传不完整，请重试")
        return content

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.send_json({"ok": True, "name": "QQ作业助手", "app_dir": str(APP_DIR)})
        if parsed.path == "/api/state":
            with db() as conn:
                roster = [dict(row) for row in conn.execute("SELECT * FROM roster ORDER BY group_name, qq_number")]
                tasks = []
                for row in conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 100"):
                    item = dict(row)
                    summary = task_summary(conn, item["id"])
                    item["counts"] = summary["counts"]
                    tasks.append(item)
                groups = [r[0] for r in conn.execute("SELECT DISTINCT group_name FROM roster WHERE active=1 AND group_name<>'' ORDER BY group_name")]
                announcements = [dict(row) for row in conn.execute("""
                    SELECT a.id,a.subject,a.group_name,a.created_at,COUNT(r.id) AS recipient_count,
                        SUM(CASE WHEN r.status='sent' THEN 1 ELSE 0 END) AS sent_count,
                        SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN r.status='pending' THEN 1 ELSE 0 END) AS pending_count
                    FROM announcements a LEFT JOIN announcement_recipients r ON r.announcement_id=a.id
                    GROUP BY a.id ORDER BY a.id DESC LIMIT 10
                """)]
            with SESSION_LOCK:
                account = SESSION["account"]
            return self.send_json({"roster": roster, "tasks": tasks, "groups": groups, "announcements": announcements, "connected": bool(account), "account": account or "",
                                   "has_saved": bool(SESSION_CACHE["saved_account"]), "saved_account": SESSION_CACHE["saved_account"],
                                   "auto_state": SESSION_CACHE["auto_state"], "auto_error": SESSION_CACHE["auto_error"]})
        if parsed.path == "/api/status":
            with db() as conn:
                task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                roster_count = conn.execute("SELECT COUNT(*) FROM roster WHERE active=1").fetchone()[0]
            with SESSION_LOCK:
                account = SESSION["account"]
            return self.send_json({
                "ok": True,
                "name": "QQ作业助手",
                "running": True,
                "connected": bool(account),
                "account": account or "",
                "has_saved": bool(SESSION_CACHE["saved_account"]),
                "auto_state": SESSION_CACHE["auto_state"],
                "task_count": task_count,
                "roster_count": roster_count,
                "server_time": datetime.now().isoformat(timespec="seconds"),
            })
        match = re.fullmatch(r"/api/announcements/(\d+)", parsed.path)
        if match:
            with db() as conn:
                announcement = conn.execute("SELECT * FROM announcements WHERE id=?", (int(match.group(1)),)).fetchone()
                if not announcement:
                    return self.send_json({"error": "未找到通知记录"}, 404)
                recipients = [dict(row) for row in conn.execute(
                    "SELECT qq_number,name,group_name,recipient,status,error,sent_at FROM announcement_recipients WHERE announcement_id=? ORDER BY id",
                    (int(match.group(1)),),
                )]
            return self.send_json({"announcement": dict(announcement), "recipients": recipients})
        match = re.fullmatch(r"/api/tasks/(\d+)", parsed.path)
        if match:
            with db() as conn:
                return self.send_json(task_summary(conn, int(match.group(1))))
        match = re.fullmatch(r"/api/tasks/(\d+)/reminders", parsed.path)
        if match:
            with db() as conn:
                summary = task_summary(conn, int(match.group(1)))
                drafts = []
                for member in summary["members"]:
                    if member["status"] != "missing" or not member["email_verified"] or not member["email"] or member["reminded"]:
                        continue
                    deadline = summary["task"]["due_at"] or "未设置"
                    drafts.append({
                        "qq_number": member["qq_number"], "name": member["name"], "group_name": member["group_name"],
                        "recipient": member["email"],
                        "subject": "【作业提醒】" + summary["task"]["name"],
                        "body": f"{member['name']}同学：\n\n目前还没有在统计结果中找到你提交的“{summary['task']['name']}”。\n截止时间：{deadline}\n如已提交或有特殊情况，请直接回复这封邮件说明。\n\n班级作业提醒",
                    })
            return self.send_json({"task_id": int(match.group(1)), "drafts": drafts})
        if parsed.path == "/" or parsed.path.startswith("/static/"):
            relative = "index.html" if parsed.path == "/" else parsed.path.removeprefix("/static/")
            target = (STATIC_DIR / relative).resolve()
            if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
                return self.send_json({"error": "未找到页面"}, 404)
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else ""))
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(content)
        return self.send_json({"error": "未找到接口"}, 404)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            return self.send_json({"error": "来源校验失败"}, 403)
        try:
            path = urlparse(self.path).path
            if path == "/api/roster/import-preview":
                filename = Path(unquote(self.headers.get("X-Upload-Filename", ""))).name
                content = self.read_roster_upload()
                return self.send_json(parse_roster_upload(filename, content))
            data = self.read_json()
            if path == "/api/connect":
                account = str(data.get("account", "")).strip().lower()
                auth_code = str(data.get("auth_code", "")).strip().replace(" ", "")
                remember = bool(data.get("remember", True))
                if not re.fullmatch(r"[^@\s]+@[^@\s]+", account) or not auth_code:
                    raise ValueError("请填写完整邮箱地址和客户端授权码")
                try_login(account, auth_code)
                with SESSION_LOCK:
                    SESSION.update(account=account, auth_code=auth_code)
                if remember:
                    save_session(account, auth_code)
                else:
                    clear_session()
                SESSION_CACHE["auto_state"] = "ok"
                SESSION_CACHE["auto_error"] = ""
                return self.send_json({"ok": True, "account": account, "remember": remember})
            if path == "/api/auto-connect":
                # 一键连接：用本机已保存（DPAPI 加密）的凭据直接登录
                with SESSION_LOCK:
                    current = SESSION["account"]
                if current:
                    return self.send_json({"ok": True, "account": current, "already": True})
                account, auth_code = load_session()
                if not account:
                    return self.send_json({"ok": False, "error": "本机没有保存的连接信息"}, 400)
                SESSION_CACHE["auto_state"] = "connecting"
                try:
                    try_login(account, auth_code)
                except Exception as exc:
                    SESSION_CACHE["auto_state"] = "error"
                    SESSION_CACHE["auto_error"] = f"自动连接失败：{exc}"
                    return self.send_json({"ok": False, "error": SESSION_CACHE["auto_error"]}, 400)
                with SESSION_LOCK:
                    SESSION.update(account=account, auth_code=auth_code)
                SESSION_CACHE["auto_state"] = "ok"
                SESSION_CACHE["auto_error"] = ""
                return self.send_json({"ok": True, "account": account})
            if path == "/api/disconnect":
                with SESSION_LOCK:
                    SESSION.update(account=None, auth_code=None)
                clear_session()
                SESSION_CACHE["auto_state"] = "idle"
                return self.send_json({"ok": True})
            if path == "/api/announcements":
                with SESSION_LOCK:
                    account, auth_code = SESSION["account"], SESSION["auth_code"]
                if not account or not auth_code:
                    raise ValueError("请先连接 QQ 邮箱")
                subject = str(data.get("subject", "")).strip()
                body = str(data.get("body", "")).strip()
                group_filter = str(data.get("group_filter", "")).strip()
                raw_qq_numbers = data.get("qq_numbers")
                if not isinstance(raw_qq_numbers, list):
                    raise ValueError("收件人列表格式无效")
                qq_numbers = [str(value).strip() for value in raw_qq_numbers]
                if not subject or not body:
                    raise ValueError("通知主题和正文不能为空")
                if len(subject) > 200 or "\r" in subject or "\n" in subject:
                    raise ValueError("主题不能超过 200 字，且不能包含换行")
                if len(body) > 10000:
                    raise ValueError("通知正文不能超过 10000 字")
                if not qq_numbers:
                    raise ValueError("请至少选择一位收件人")
                if len(qq_numbers) > MAX_ANNOUNCEMENT_RECIPIENTS:
                    raise ValueError(f"一次最多向 {MAX_ANNOUNCEMENT_RECIPIENTS} 位成员发送通知")
                if len(set(qq_numbers)) != len(qq_numbers):
                    raise ValueError("收件人列表包含重复成员，请刷新后重试")
                with db() as conn:
                    placeholders = ",".join("?" for _ in qq_numbers)
                    rows = conn.execute(f"""
                        SELECT qq_number,name,student_id,group_name,email,email_verified,active
                        FROM roster WHERE qq_number IN ({placeholders})
                    """, qq_numbers).fetchall()
                    by_qq = {row["qq_number"]: row for row in rows}
                    if len(by_qq) != len(qq_numbers):
                        raise ValueError("部分成员已不在花名册中，请刷新名单")
                    recipients = []
                    for qq in qq_numbers:
                        member = by_qq[qq]
                        if not member["active"] or not member["email_verified"] or not member["email"]:
                            raise ValueError(f"{member['name']}（QQ {qq}）没有已核实邮箱，不能发送")
                        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", member["email"]):
                            raise ValueError(f"{member['name']}（QQ {qq}）的邮箱地址格式无效")
                        if group_filter == "__none__" and member["group_name"]:
                            raise ValueError("收件人分类已发生变化，请刷新列表后重试")
                        if group_filter and group_filter != "__none__" and member["group_name"] != group_filter:
                            raise ValueError("收件人分类已发生变化，请刷新列表后重试")
                        recipients.append({
                            "qq_number": qq, "name": member["name"],
                            "group_name": member["group_name"], "recipient": member["email"],
                        })
                    created_at = datetime.now().astimezone().isoformat(timespec="minutes")
                    cursor = conn.execute(
                        "INSERT INTO announcements(subject,body,group_name,created_at) VALUES(?,?,?,?)",
                        (subject, body, "全部分类" if not group_filter else ("未分类" if group_filter == "__none__" else group_filter), created_at),
                    )
                    announcement_id = cursor.lastrowid
                    conn.executemany("""
                        INSERT INTO announcement_recipients(announcement_id,qq_number,name,group_name,recipient,status)
                        VALUES(?,?,?,?,?,'pending')
                    """, [
                        (announcement_id, member["qq_number"], member["name"], member["group_name"], member["recipient"])
                        for member in recipients
                    ])

                results = smtp_send_announcement(account, auth_code, recipients, subject, body)
                sent_at = datetime.now().astimezone().isoformat(timespec="minutes")
                with db() as conn:
                    conn.executemany("""
                        UPDATE announcement_recipients SET status=?,error=?,sent_at=?
                        WHERE announcement_id=? AND qq_number=?
                    """, [
                        (result["status"], result["error"], sent_at if result["status"] == "sent" else "", announcement_id, result["qq_number"])
                        for result in results
                    ])
                sent = [result["qq_number"] for result in results if result["status"] == "sent"]
                errors = [result for result in results if result["status"] == "failed"]
                return self.send_json({"ok": not errors, "announcement_id": announcement_id, "sent": sent, "errors": errors})
            if path == "/api/roster":
                action = data.get("action", "save")
                with db() as conn:
                    if action == "delete":
                        conn.execute("DELETE FROM roster WHERE qq_number=?", (str(data.get("qq_number", "")),))
                        return self.send_json({"ok": True})
                    if action == "import":
                        reader = csv.DictReader(data.get("csv", "").lstrip("\ufeff").splitlines())
                        count = 0
                        for row in reader:
                            normalized = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
                            def cell(*names):
                                return next((normalized[n] for n in names if normalized.get(n)), "")
                            qq = cell("qq_number", "qq", "qq号", "qq号码")
                            name = cell("name", "姓名")
                            if not qq or not name:
                                continue
                            student_id = cell("student_id", "student_no", "学号", "学籍号")
                            group_name = cell("group_name", "group", "班级", "小组")
                            email = cell("email", "邮箱", "邮件")
                            verified = cell("email_verified", "verified", "邮箱已核实").lower() in ("1", "true", "yes", "y", "是")
                            owner = conn.execute("SELECT qq_number,name FROM roster WHERE student_id=? AND qq_number<>?", (student_id, qq)).fetchone() if student_id else None
                            if owner:
                                raise ValueError(f"学号 {student_id} 已登记给 {owner['name']}（QQ {owner['qq_number']}）")
                            conn.execute("""INSERT INTO roster(qq_number,name,student_id,group_name,email,email_verified,active) VALUES(?,?,?,?,?,?,1)
                                ON CONFLICT(qq_number) DO UPDATE SET name=excluded.name,student_id=excluded.student_id,group_name=excluded.group_name,email=excluded.email,email_verified=excluded.email_verified""",
                                (qq, name, student_id, group_name, email, int(verified)))
                            count += 1
                        return self.send_json({"ok": True, "count": count})
                    if action == "import-parsed":
                        rows = data.get("rows")
                        if not isinstance(rows, list) or not rows or len(rows) > MAX_ROSTER_ROWS:
                            raise ValueError("请至少选择一条有效记录，且一次最多导入 1000 条")
                        normalized_rows = []
                        seen_qq = set()
                        seen_student_ids = set()
                        for row in rows:
                            if not isinstance(row, dict):
                                raise ValueError("导入数据格式无效，请重新预览文件")
                            qq = str(row.get("qq_number", "")).strip()
                            name = str(row.get("name", "")).strip()
                            student_id = str(row.get("student_id", "")).strip()
                            group_name = str(row.get("group_name", "")).strip()
                            if not name or not student_id or not re.fullmatch(r"\d{5,12}", qq):
                                raise ValueError("每条记录都需要有效的姓名、学号和 5 至 12 位 QQ 号")
                            if len(name) > 80 or len(student_id) > 80 or len(group_name) > 100:
                                raise ValueError("姓名、学号或分类名称过长")
                            if qq in seen_qq:
                                raise ValueError(f"文件中 QQ 号 {qq} 重复，请检查预览记录")
                            if student_id in seen_student_ids:
                                raise ValueError(f"文件中学号 {student_id} 重复，请检查预览记录")
                            seen_qq.add(qq)
                            seen_student_ids.add(student_id)
                            normalized_rows.append((qq, name, student_id, group_name))

                        for qq, name, student_id, group_name in normalized_rows:
                            owner = conn.execute("SELECT qq_number,name FROM roster WHERE student_id=? AND qq_number<>?", (student_id, qq)).fetchone()
                            if owner:
                                raise ValueError(f"学号 {student_id} 已登记给 {owner['name']}（QQ {owner['qq_number']}）")

                        created = 0
                        updated = 0
                        for qq, name, student_id, group_name in normalized_rows:
                            existing = conn.execute("SELECT qq_number FROM roster WHERE qq_number=?", (qq,)).fetchone()
                            if existing:
                                conn.execute("UPDATE roster SET name=?,student_id=?,group_name=? WHERE qq_number=?", (name, student_id, group_name, qq))
                                updated += 1
                            else:
                                conn.execute("INSERT INTO roster(qq_number,name,student_id,group_name) VALUES(?,?,?,?)", (qq, name, student_id, group_name))
                                created += 1
                        return self.send_json({"ok": True, "count": len(normalized_rows), "created": created, "updated": updated})
                    qq = str(data.get("qq_number", "")).strip()
                    name = str(data.get("name", "")).strip()
                    student_id = str(data.get("student_id", "")).strip()
                    group_name = str(data.get("group_name", "")).strip()
                    email = str(data.get("email", "")).strip().lower()
                    if not qq or not name:
                        raise ValueError("QQ号和姓名不能为空")
                    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                        raise ValueError("邮箱地址格式不正确")
                    owner = conn.execute("SELECT qq_number,name FROM roster WHERE student_id=? AND qq_number<>?", (student_id, qq)).fetchone() if student_id else None
                    if owner:
                        raise ValueError(f"学号 {student_id} 已登记给 {owner['name']}（QQ {owner['qq_number']}）")
                    conn.execute("""INSERT INTO roster(qq_number,name,student_id,group_name,email,email_verified,active) VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(qq_number) DO UPDATE SET name=excluded.name,student_id=excluded.student_id,group_name=excluded.group_name,email=excluded.email,email_verified=excluded.email_verified,active=excluded.active""",
                        (qq, name, student_id, group_name, email, int(bool(data.get("email_verified"))), int(bool(data.get("active", True)))))
                return self.send_json({"ok": True})
            if path == "/api/scan":
                with SESSION_LOCK:
                    account, auth_code = SESSION["account"], SESSION["auth_code"]
                if not account or not auth_code:
                    raise ValueError("请先连接 QQ 邮箱")
                name = str(data.get("name", "")).strip()
                start = date.fromisoformat(data.get("start_date", ""))
                end = date.fromisoformat(data.get("end_date", ""))
                if not name or end < start:
                    raise ValueError("请填写任务名称，并确认结束日期不早于开始日期")
                assignment_label = str(data.get("assignment_label", "")).strip()
                match_by_student_id = bool(data.get("match_by_student_id", True))
                keywords = [str(word).strip() for word in data.get("keywords", []) if str(word).strip()]
                fields = [field for field in data.get("keyword_fields", []) if field in ("subject", "body", "filename")]
                if (keywords or assignment_label) and not fields:
                    raise ValueError("请至少选择一个检索位置")
                mode = "all" if data.get("keyword_mode") == "all" else "any"
                group_name = str(data.get("group_name", "")).strip()
                due_at = str(data.get("due_at", "")).strip()
                if due_at:
                    due_at = datetime.fromisoformat(due_at).astimezone().isoformat(timespec="minutes")
                client = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=30)
                try:
                    client.login(account, auth_code)
                    status, _ = client.select("INBOX", readonly=True)
                    if status != "OK":
                        raise ValueError("无法读取收件箱")
                    uidvalidity_data = client.response("UIDVALIDITY")[1]
                    uidvalidity = (uidvalidity_data[0].decode() if uidvalidity_data and uidvalidity_data[0] else "")
                    since = start.strftime("%d-%b-%Y")
                    before = (end + timedelta(days=1)).strftime("%d-%b-%Y")
                    status, rows = client.uid("search", None, "SINCE", since, "BEFORE", before)
                    if status != "OK":
                        raise ValueError("邮箱日期搜索失败")
                    uids = rows[0].split() if rows and rows[0] else []
                    if len(uids) > 3000:
                        raise ValueError("时间范围内邮件超过 3000 封，请缩小日期范围")
                    candidates = []
                    with db() as conn:
                        roster = conn.execute("SELECT * FROM roster WHERE active=1").fetchall()
                    for uid in uids:
                        status, fetched = client.uid("fetch", uid, "(UID INTERNALDATE RFC822)")
                        if status != "OK":
                            continue
                        raw = b""
                        internaldate = None
                        for item in fetched:
                            if not isinstance(item, tuple):
                                continue
                            header = item[0] or b""
                            match_date = re.search(br'INTERNALDATE "([^"]+)"', header)
                            if match_date:
                                try:
                                    internaldate = imaplib.Internaldate2tuple(b'INTERNALDATE "' + match_date.group(1) + b'"')
                                    if internaldate:
                                        internaldate = datetime.fromtimestamp(time.mktime(internaldate)).astimezone()
                                except Exception:
                                    internaldate = None
                            for piece in item[1:]:
                                if isinstance(piece, bytes):
                                    raw = piece
                                    break
                        if not raw:
                            continue
                        received = internaldate or datetime.now().astimezone()
                        if not (start.isoformat() <= received.date().isoformat() <= end.isoformat()):
                            continue
                        message = message_from_bytes(raw, policy=default)
                        subject = decoded(message.get("Subject"))
                        from_name, from_addr = parseaddr(decoded(message.get("From")))
                        from_name, from_addr = decoded(from_name), from_addr.lower()
                        body = message_text(message)
                        pdfs = attachment_parts(message)
                        if not pdfs:
                            continue
                        haystack = {
                            "subject": subject.casefold(),
                            "body": body.casefold(),
                        }
                        matches = []
                        for attachment in pdfs:
                            haystack["filename"] = attachment["filename"].casefold()
                            relevant = [haystack[field] for field in fields]
                            fixed_match = not assignment_label or any(assignment_label.casefold() in text for text in relevant)
                            if keywords:
                                checks = [any(word.casefold() in text for text in relevant) for word in keywords]
                                matched = all(checks) if mode == "all" else any(checks)
                            else:
                                matched = True
                            if fixed_match and matched:
                                matches.append(attachment)
                        if not matches:
                            continue
                        identity_texts = [haystack[field] for field in fields if field != "filename"]
                        if "filename" in fields:
                            identity_texts.extend(attachment["filename"].casefold() for attachment in matches)
                        matched_member, match_method = resolve_member(roster, from_addr, identity_texts, match_by_student_id)
                        if group_name and matched_member and matched_member["group_name"] != group_name:
                            continue
                        candidates.append({
                            "uid": uid.decode(), "uidvalidity": uidvalidity,
                            "message_id": str(message.get("Message-ID", "")).strip(),
                            "from_addr": from_addr, "from_name": from_name, "subject": subject,
                            "received_at": received.isoformat(timespec="minutes"),
                            "matched_qq": matched_member["qq_number"] if matched_member else "",
                            "match_method": match_method, "attachments": matches,
                        })
                    with db() as conn:
                        cursor = conn.execute("""INSERT INTO tasks(name,assignment_label,match_by_student_id,start_date,end_date,due_at,keywords,keyword_mode,keyword_fields,group_name,auto_remind,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name,assignment_label,int(match_by_student_id),start.isoformat(),end.isoformat(),due_at,json.dumps(keywords,ensure_ascii=False),mode,json.dumps(fields),group_name,int(bool(data.get("auto_remind"))),datetime.now().astimezone().isoformat(timespec="minutes")))
                        task_id = cursor.lastrowid
                        for item in candidates:
                            cursor = conn.execute("""INSERT INTO messages(task_id,uid,uidvalidity,message_id,from_addr,from_name,subject,received_at,matched_qq,match_method)
                                VALUES(?,?,?,?,?,?,?,?,?,?)""", (task_id,item["uid"],item["uidvalidity"],item["message_id"],item["from_addr"],item["from_name"],item["subject"],item["received_at"],item["matched_qq"],item["match_method"]))
                            message_pk = cursor.lastrowid
                            for attachment in item["attachments"]:
                                conn.execute("""INSERT INTO attachments(message_id,ordinal,filename,content_type,size,preview_sha256)
                                    VALUES(?,?,?,?,?,?)""", (message_pk,attachment["ordinal"],attachment["filename"],attachment["content_type"],attachment["size"],attachment["sha256"]))
                finally:
                    try:
                        client.logout()
                    except Exception:
                        pass
                with db() as conn:
                    return self.send_json(task_summary(conn, task_id))
            match = re.fullmatch(r"/api/tasks/(\d+)/download", path)
            if match:
                task_id = int(match.group(1))
                ids = [int(v) for v in data.get("attachment_ids", [])]
                if not ids:
                    raise ValueError("请至少选择一个文件")
                with SESSION_LOCK:
                    account, auth_code = SESSION["account"], SESSION["auth_code"]
                if not account or not auth_code:
                    raise ValueError("邮箱连接已断开，请重新连接")
                with db() as conn:
                    task = task_row(conn, task_id)
                    placeholders = ",".join("?" for _ in ids)
                    rows = conn.execute(f"""SELECT a.*,m.task_id,m.uid,m.uidvalidity,m.message_id AS source_message_id,m.from_addr,m.from_name,m.subject,m.received_at,m.matched_qq,m.match_method
                        FROM attachments a JOIN messages m ON m.id=a.message_id WHERE m.task_id=? AND a.id IN ({placeholders})""", [task_id,*ids]).fetchall()
                if len(rows) != len(set(ids)):
                    raise ValueError("所选文件中包含无效记录，请刷新页面后重试")
                downloaded = materialize_attachments(task_id, task, rows, account, auth_code)
                folder = export_folder_for(task)
                copied = copy_into_folder(downloaded, folder)
                open_folder(folder)
                return self.send_json({"ok": True, "downloaded": downloaded, "folder": str(folder),
                                       "folder_name": folder.name, "files": copied})
            match = re.fullmatch(r"/api/tasks/(\d+)/export-desktop", path)
            if match:
                task_id = int(match.group(1))
                with SESSION_LOCK:
                    account, auth_code = SESSION["account"], SESSION["auth_code"]
                if not account or not auth_code:
                    raise ValueError("邮箱连接已断开，请重新连接")
                with db() as conn:
                    task = task_row(conn, task_id)
                    rows = conn.execute("""SELECT a.*,m.task_id,m.uid,m.uidvalidity,m.message_id AS source_message_id,
                        m.from_addr,m.from_name,m.subject,m.received_at,m.matched_qq,m.match_method
                        FROM attachments a JOIN messages m ON m.id=a.message_id
                        WHERE m.task_id=? ORDER BY m.received_at,a.filename""", (task_id,)).fetchall()
                if not rows:
                    raise ValueError("本次统计没有可导出的文件")
                downloaded = materialize_attachments(task_id, task, rows, account, auth_code)
                folder = export_folder_for(task)
                copied = copy_into_folder(downloaded, folder)
                open_folder(folder)
                return self.send_json({
                    "ok": True, "folder": str(folder), "folder_name": folder.name,
                    "files": copied,
                })
            match = re.fullmatch(r"/api/tasks/(\d+)/auto-remind", path)
            if match:
                task_id = int(match.group(1))
                enabled = 1 if data.get("enabled") else 0
                with db() as conn:
                    if not conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone():
                        raise ValueError("未找到该任务")
                    conn.execute("UPDATE tasks SET auto_remind=? WHERE id=?", (enabled, task_id))
                    return self.send_json(task_summary(conn, task_id))
            match = re.fullmatch(r"/api/tasks/(\d+)/remind", path)
            if match:
                task_id = int(match.group(1))
                with SESSION_LOCK:
                    account, auth_code = SESSION["account"], SESSION["auth_code"]
                if not account or not auth_code:
                    raise ValueError("请先连接 QQ 邮箱")
                selected = set(str(x) for x in data.get("qq_numbers", []))
                if not selected:
                    raise ValueError("没有选择收件人")
                with db() as conn:
                    summary = task_summary(conn, task_id)
                    drafts = {d["qq_number"]: d for d in self._drafts(summary)}
                    if not selected.issubset(drafts):
                        raise ValueError("有些收件人已提交、未核实邮箱或已提醒，请刷新预览")
                sent, errors = [], []
                for qq in sorted(selected):
                    draft = drafts[qq]
                    try:
                        smtp_send(account, auth_code, draft["recipient"], draft["subject"], draft["body"])
                    except Exception as exc:
                        errors.append({"qq_number": qq, "error": str(exc)})
                        continue
                    with db() as conn:
                        conn.execute("INSERT OR IGNORE INTO reminders(task_id,qq_number,recipient,sent_at) VALUES(?,?,?,?)", (task_id,qq,draft["recipient"],datetime.now().astimezone().isoformat(timespec="minutes")))
                    sent.append(qq)
                return self.send_json({"ok": not errors, "sent": sent, "errors": errors})
            return self.send_json({"error": "未找到接口"}, 404)
        except Exception as exc:
            message = str(exc)
            if isinstance(exc, imaplib.IMAP4.error):
                message = "邮箱连接失败。请确认已在 QQ 邮箱开启 IMAP/SMTP，并使用最新客户端授权码。"
            return self.send_json({"error": message or "操作失败"}, 400)

    @staticmethod
    def _drafts(summary):
        result = []
        for member in summary["members"]:
            if member["status"] != "missing" or not member["email_verified"] or not member["email"] or member["reminded"]:
                continue
            deadline = summary["task"]["due_at"] or "未设置"
            result.append({
                "qq_number": member["qq_number"], "name": member["name"], "group_name": member["group_name"],
                "recipient": member["email"], "subject": "【作业提醒】" + summary["task"]["name"],
                "body": f"{member['name']}同学：\n\n目前还没有在统计结果中找到你提交的“{summary['task']['name']}”。\n截止时间：{deadline}\n如已提交或有特殊情况，请直接回复这封邮件说明。\n\n班级作业提醒",
            })
        return result


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def auto_remind_loop():
    """后台调度：对开启「截止后自动提醒」且已过截止时间的任务，自动发送一次未交提醒。

    仅向「未提交 + 邮箱已核实 + 未提醒过」的成员发送；每个任务只发一次（auto_reminded_at 标记）。
    全程包裹异常，绝不因单次失败影响服务或其它任务。
    """
    while True:
        time.sleep(60)
        try:
            with SESSION_LOCK:
                account = SESSION.get("account")
                auth_code = SESSION.get("auth_code")
            if not account or not auth_code:
                continue
            with db() as conn:
                rows = conn.execute(
                    "SELECT id,due_at FROM tasks WHERE auto_remind=1 AND auto_reminded_at IS NULL AND due_at<>''"
                ).fetchall()
            now = datetime.now().astimezone()
            for task_id, due_at in rows:
                try:
                    due = datetime.fromisoformat(due_at)
                except Exception:
                    continue
                if due > now:
                    continue
                try:
                    with db() as conn:
                        summary = task_summary(conn, task_id)
                    drafts = Handler._drafts(summary)
                    stamp = datetime.now().astimezone().isoformat(timespec="minutes")
                    if not drafts:
                        with db() as conn:
                            conn.execute("UPDATE tasks SET auto_reminded_at=? WHERE id=?", (stamp, task_id))
                        continue
                    for draft in drafts:
                        try:
                            smtp_send(account, auth_code, draft["recipient"], draft["subject"], draft["body"])
                        except Exception:
                            continue
                        with db() as conn:
                            conn.execute(
                                "INSERT OR IGNORE INTO reminders(task_id,qq_number,recipient,sent_at) VALUES(?,?,?,?)",
                                (task_id, draft["qq_number"], draft["recipient"], datetime.now().astimezone().isoformat(timespec="minutes")),
                            )
                    with db() as conn:
                        conn.execute("UPDATE tasks SET auto_reminded_at=? WHERE id=?", (stamp, task_id))
                except Exception:
                    continue
        except Exception:
            continue


def find_port():
    candidates = [8765] + list(range(8766, 8796))
    for candidate in candidates:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", candidate))
            probe.close()
            return candidate
        except OSError:
            probe.close()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{candidate}/api/health", timeout=0.5) as response:
                    state = json.loads(response.read().decode("utf-8"))
                    if state.get("name") == "QQ作业助手" and state.get("app_dir") == str(APP_DIR):
                        webbrowser.open(f"http://127.0.0.1:{candidate}")
                        return None
            except Exception:
                pass
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def startup_auto_connect():
    """启动时用本机保存的凭据自动连接（实现"打开即已连接"）。"""
    account, auth_code = load_session()
    if not account:
        return
    SESSION_CACHE["auto_state"] = "connecting"
    try:
        try_login(account, auth_code)
    except Exception as exc:
        SESSION_CACHE["auto_state"] = "error"
        SESSION_CACHE["auto_error"] = f"自动连接失败：{exc}"
        return
    with SESSION_LOCK:
        SESSION.update(account=account, auth_code=auth_code)
    SESSION_CACHE["auto_state"] = "ok"
    SESSION_CACHE["auto_error"] = ""


def main():
    initialize()
    port = find_port()
    if port is None:
        return
    server = LocalServer(("127.0.0.1", port), Handler)
    threading.Thread(target=auto_remind_loop, daemon=True).start()
    threading.Thread(target=startup_auto_connect, daemon=True).start()
    threading.Timer(0.7, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
