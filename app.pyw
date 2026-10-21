import csv
from contextlib import contextmanager
import hashlib
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import socket
import sqlite3
import shutil
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
DOWNLOAD_DIR = APP_DIR / "downloads"
DB_PATH = DATA_DIR / "assistant.sqlite3"
DATA_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

SESSION = {"account": None, "auth_code": None}
SESSION_LOCK = threading.Lock()


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
        """)
        roster_columns = {row[1] for row in conn.execute("PRAGMA table_info(roster)")}
        if "student_id" not in roster_columns:
            conn.execute("ALTER TABLE roster ADD COLUMN student_id TEXT NOT NULL DEFAULT ''")
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "assignment_label" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN assignment_label TEXT NOT NULL DEFAULT ''")
        if "match_by_student_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN match_by_student_id INTEGER NOT NULL DEFAULT 1")
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


def attachment_parts(message):
    found = []
    ordinal = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = decoded(part.get_filename())
        if not filename and part.get_content_disposition() != "attachment":
            continue
        if not filename:
            filename = "附件.pdf" if part.get_content_type() == "application/pdf" else "附件"
        payload = part.get_payload(decode=True) or b""
        if filename.lower().endswith(".pdf") or part.get_content_type() == "application/pdf":
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            found.append({
                "ordinal": ordinal,
                "filename": filename,
                "content_type": part.get_content_type(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        ordinal += 1
    return found


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
    return name[:180] or "作业.pdf"


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
            parts = []
            ordinal = 0
            for part in message.walk():
                if part.is_multipart():
                    continue
                filename = decoded(part.get_filename())
                if not filename and part.get_content_disposition() != "attachment":
                    continue
                if not filename:
                    filename = "附件.pdf" if part.get_content_type() == "application/pdf" else "附件"
                payload = part.get_payload(decode=True) or b""
                if filename.lower().endswith(".pdf") or part.get_content_type() == "application/pdf":
                    if not filename.lower().endswith(".pdf"):
                        filename += ".pdf"
                    parts.append((ordinal, filename, payload))
                ordinal += 1
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


class Handler(BaseHTTPRequestHandler):
    server_version = "ClassworkAssistant/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 2_000_000:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(size).decode("utf-8")) if size else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.send_json({"ok": True, "name": "QQ作业助手"})
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
            with SESSION_LOCK:
                account = SESSION["account"]
            return self.send_json({"roster": roster, "tasks": tasks, "groups": groups, "connected": bool(account), "account": account or ""})
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
            self.end_headers()
            return self.wfile.write(content)
        return self.send_json({"error": "未找到接口"}, 404)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            return self.send_json({"error": "来源校验失败"}, 403)
        try:
            data = self.read_json()
            path = urlparse(self.path).path
            if path == "/api/connect":
                account = str(data.get("account", "")).strip().lower()
                auth_code = str(data.get("auth_code", "")).strip().replace(" ", "")
                if not re.fullmatch(r"[^@\s]+@[^@\s]+", account) or not auth_code:
                    raise ValueError("请填写完整邮箱地址和客户端授权码")
                client = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=20)
                try:
                    client.login(account, auth_code)
                finally:
                    try:
                        client.logout()
                    except Exception:
                        pass
                with SESSION_LOCK:
                    SESSION.update(account=account, auth_code=auth_code)
                return self.send_json({"ok": True, "account": account})
            if path == "/api/disconnect":
                with SESSION_LOCK:
                    SESSION.update(account=None, auth_code=None)
                return self.send_json({"ok": True})
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
                        cursor = conn.execute("""INSERT INTO tasks(name,assignment_label,match_by_student_id,start_date,end_date,due_at,keywords,keyword_mode,keyword_fields,group_name,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (name,assignment_label,int(match_by_student_id),start.isoformat(),end.isoformat(),due_at,json.dumps(keywords,ensure_ascii=False),mode,json.dumps(fields),group_name,datetime.now().astimezone().isoformat(timespec="minutes")))
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
                    raise ValueError("请至少选择一个 PDF")
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
                return self.send_json({"ok": True, "downloaded": downloaded})
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
                    raise ValueError("本次统计没有可导出的 PDF")
                downloaded = materialize_attachments(task_id, task, rows, account, auth_code)
                desktop = desktop_directory()
                desktop.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                base_name = f"{task['start_date']}_至_{task['end_date']}_{clean_label(task['name'])}_{timestamp}"
                export_dir = desktop / base_name
                suffix = 2
                while export_dir.exists():
                    export_dir = desktop / f"{base_name}_{suffix}"
                    suffix += 1
                export_dir.mkdir()
                for item in downloaded:
                    source = Path(item["path"])
                    shutil.copy2(source, export_dir / source.name)
                    manifest = source.with_name(source.name + ".source.json")
                    if manifest.is_file():
                        shutil.copy2(manifest, export_dir / manifest.name)
                return self.send_json({
                    "ok": True, "folder": str(export_dir), "folder_name": export_dir.name,
                    "files": len(downloaded),
                })
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


def find_port():
    for candidate in range(8765, 8796):
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
                    if state.get("name") == "QQ作业助手":
                        webbrowser.open(f"http://127.0.0.1:{candidate}")
                        return None
            except Exception:
                pass
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def main():
    initialize()
    port = find_port()
    if port is None:
        return
    server = LocalServer(("127.0.0.1", port), Handler)
    threading.Timer(0.7, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
