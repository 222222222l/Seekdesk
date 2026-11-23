import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .secrets import transform


def now():
    return int(time.time())


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


DEFAULTS = dict(
    provider="local",
    model="",
    api_url="",
    api_key="",
    keywords="",
    location="",
    interval=60,
    threshold=80,
    scheduled=False,
    outreach=False,
    resume_auto=False,
    smtp_host="",
    email_enabled=True,
    mail_mode="auto",
    windows_notifications=False,
    smtp_port=465,
    smtp_user="",
    smtp_password="",
    email="",
    brave_key="",
    tavily_key="",
    boards="greenhouse:gitlab\nlever:spotify",
    sites="",
    platforms=[],
    daily_limit=5,
)
SECRETS = {"api_key", "smtp_password", "brave_key", "tavily_key"}


def rating_context(cfg, resume_id):
    return digest(resume_id, cfg["provider"], cfg["model"], cfg["api_url"], cfg["keywords"], cfg["location"])


def job_id(item):
    source = item["source"]
    if source in ("企业官网", "搜索发现"):
        source = "web"
    return digest(source, item["external_id"])


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "desk.sqlite3"
        with self.db() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resumes(id TEXT PRIMARY KEY, name TEXT, body BLOB, text TEXT, added INTEGER);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, source TEXT, external_id TEXT, company TEXT, title TEXT, url TEXT,
                    location TEXT, description TEXT, first_seen INTEGER, seen INTEGER,
                    rating TEXT, fingerprint TEXT, active INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, start INTEGER, end INTEGER,
                    status TEXT, reports TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, job TEXT, kind TEXT, state TEXT,
                    detail TEXT, thread TEXT, resume TEXT, created INTEGER, updated INTEGER);
                CREATE INDEX IF NOT EXISTS action_thread ON actions(thread);
                CREATE TABLE IF NOT EXISTS notice_receipts(action_id TEXT, channel TEXT, receipt TEXT,
                    PRIMARY KEY(action_id,channel));
            """
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=12)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def rows(self, sql, args=()):
        with self.db() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.db() as db:
            return db.execute(sql, args).lastrowid

    def get(self, key, default=None):
        rows = self.rows("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def put(self, key, value):
        if key == "settings":
            value = {k: transform(v) if k in SECRETS else v for k, v in value.items()}
            with self.db() as db:
                previous = db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
                previous = json.loads(previous["value"]) if previous else {}
                if value.get("windows_notifications") and not previous.get("windows_notifications"):
                    db.execute(
                        """INSERT OR REPLACE INTO kv SELECT 'desktop_after_action',
                                  CAST(COALESCE(MAX(rowid),0) AS TEXT) FROM actions"""
                    )
                profile = {k: value.get(k, "") for k in ("model", "api_url", "api_key")}
                db.executemany(
                    "INSERT OR REPLACE INTO kv VALUES(?,?)",
                    [
                        ("backend." + value["provider"], json.dumps(profile, ensure_ascii=False)),
                        (key, json.dumps(value, ensure_ascii=False)),
                    ],
                )
            return
        self.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def settings(self):
        saved = self.get("settings", {})
        cfg = {**DEFAULTS, **saved}
        if "mail_mode" not in saved and (cfg["smtp_host"] or cfg["smtp_user"]):
            cfg["mail_mode"] = "custom"
        for key in SECRETS:
            cfg[key] = transform(cfg[key], decrypt=True)
        return cfg

    def backend(self, provider):
        saved = self.get("backend." + provider)
        if saved is not None:
            return {**saved, "api_key": transform(saved.get("api_key", ""), decrypt=True)}
        current = self.settings()
        if current["provider"] == provider:
            return {k: current[k] for k in ("model", "api_url", "api_key")}
        return {"model": "", "api_url": "", "api_key": ""}

    def profile(self, full=False):
        fields = "*" if full else "id,name,added,substr(text,1,240) AS preview,length(body) AS bytes"
        rows = self.rows("SELECT " + fields + " FROM resumes WHERE id=?", (self.get("resume"),))
        return rows[0] if rows else None

    def save_resume(self, name, body, content):
        rid = hashlib.sha256(body).hexdigest()
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO resumes VALUES(?,?,?,?,?)", (rid, name, body, content, now()))
            db.execute("INSERT OR REPLACE INTO kv VALUES('resume',?)", (json.dumps(rid),))
        return rid

    def upsert(self, item, rating, fingerprint):
        jid = job_id(item)
        values = [
            item.get(k, "")
            for k in ("source", "external_id", "company", "title", "url", "location", "description")
        ]
        self.execute(
            """INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(id) DO UPDATE SET company=excluded.company,title=excluded.title,url=excluded.url,
            location=excluded.location,description=excluded.description,seen=excluded.seen,
            rating=excluded.rating,fingerprint=excluded.fingerprint,active=1""",
            (jid, *values, now(), now(), json.dumps(rating, ensure_ascii=False), fingerprint),
        )
        return jid
