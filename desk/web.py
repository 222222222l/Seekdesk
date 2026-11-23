import base64
import binascii
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from . import backends, models, notifications, platforms, searxng
from .engine import message
from .storage import DEFAULTS, SECRETS, digest, now, rating_context

ROOT = Path(__file__).resolve().parents[1]
STATIC = {
    "index.html",
    "backend-picker.js",
    "desk.js",
    "desk.css",
    "review.html",
    "app.js",
    "core.js",
    "fixture.js",
    "style.css",
}
POLICY = "default-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


def validate_settings(payload, current):
    cfg = dict(current)
    for k, v in payload.items():
        if k not in DEFAULTS or type(v) is not type(DEFAULTS[k]):
            raise ValueError("设置字段无效：" + k)
        if isinstance(v, str):
            v = v.strip()
            if len(v) > 9000:
                raise ValueError("设置内容过长")
        cfg[k] = v
    for k, lo, hi in [
        ("interval", 1, 10080),
        ("threshold", 0, 100),
        ("daily_limit", 1, 100),
        ("smtp_port", 1, 65535),
    ]:
        if not lo <= cfg[k] <= hi:
            raise ValueError(k + " 超出有效范围")
    if cfg["provider"] not in backends.IDS:
        raise ValueError("未知模型类型")
    if cfg["provider"] in backends.APIS:
        backends.validate_base(cfg["api_url"])
        p = urlsplit(cfg["api_url"])
        if (
            not p.hostname
            or p.username
            or p.password
            or (
                p.scheme != "https"
                and not (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1"))
            )
            or not cfg["model"]
        ):
            raise ValueError("请填写 HTTPS 模型 API 基址和模型名称，本机模型可用 HTTP")
    if any(k not in platforms.PLATFORMS for k in cfg["platforms"]):
        raise ValueError("未知招聘平台")
    for board in cfg["boards"].split():
        if not re.fullmatch(r"(greenhouse|lever):[A-Za-z0-9_-]{1,80}", board):
            raise ValueError("招聘板格式为 greenhouse:公司 或 lever:公司")
    for url in cfg["sites"].split():
        if urlsplit(url).scheme not in ("https", "http") or not urlsplit(url).hostname:
            raise ValueError("请填写完整的招聘官网 URL")
    if cfg["email"] and not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", cfg["email"]):
        raise ValueError("通知邮箱格式不正确")
    if cfg["mail_mode"] not in ("auto", "custom"):
        raise ValueError("未知邮件配置方式")
    if cfg["windows_notifications"] and os.name != "nt":
        raise ValueError("桌面提醒需要工作台运行在 Windows 上")
    if (
        notifications.mail_connection(cfg) != notifications.mail_connection(current)
        and "smtp_password" not in payload
    ):
        cfg["smtp_password"] = ""
    if (
        cfg["email_enabled"]
        and cfg["mail_mode"] == "auto"
        and cfg["smtp_password"]
        and not notifications.mail_connection(cfg)[0]
    ):
        raise ValueError("此邮箱请使用自定义发件设置，或改用 QQ、163、126 邮箱")
    if any(
        "\n" in cfg[k] or "\r" in cfg[k] for k in ["smtp_host", "smtp_user", "model", "api_key", "api_url"]
    ):
        raise ValueError("配置中含有无效的换行")
    return cfg


def model_draft(store, data):
    key = data.get("provider")
    if key not in backends.IDS:
        raise ValueError("未知模型后端")
    saved = store.backend(key)
    base = saved["api_url"] or (backends.APIS[key][1] if key in backends.APIS else "")
    url = data.get("api_url", base).strip().rstrip("/") if key in backends.APIS else ""
    secret = (
        data.get("api_key", saved["api_key"] if url == base.rstrip("/") else "")
        if key in backends.APIS
        else ""
    )
    model = data.get("model", saved["model"]).strip()
    if model.startswith("-") or any("\n" in v or "\r" in v or len(v) > 8192 for v in (model, url, secret)):
        raise ValueError("模型名称或连接参数格式不正确")
    return {"provider": key, "model": model, "api_url": url, "api_key": secret}


class DeskHandler(BaseHTTPRequestHandler):
    def reply(self, status, data, kind="application/json", head=False):
        if kind == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        policy = (
            POLICY.replace("connect-src 'self'", "connect-src 'none'")
            if self.path == "/review.html"
            else POLICY
        )
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(data)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass  # A tab may navigate away while a model catalogue is loading.

    def host_ok(self):
        return self.headers.get("Host", "").lower() in {
            f"localhost:{self.server.server_port}",
            f"127.0.0.1:{self.server.server_port}",
        }

    def do_GET(self):
        self.read(False)

    def do_HEAD(self):
        self.read(True)

    def read(self, head):
        if not self.host_ok():
            return self.reply(400, {"error": "地址无效"}, head=head)
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.reply(200, {"workspace": digest(str(self.server.store.root.resolve()))}, head=head)
        if path == "/api/state":
            return self.reply(200, self.state(), head=head)
        if path == "/api/models":
            items = backends.catalog()
            for key, item in items.items():
                saved = self.server.store.backend(key)
                item.update(model=saved["model"], saved_url=saved["api_url"], key_set=bool(saved["api_key"]))
            return self.reply(200, items, head=head)
        if path.startswith("/api/jobs/"):
            rows = self.server.store.rows("SELECT * FROM jobs WHERE id=?", (path.removeprefix("/api/jobs/"),))
            if rows:
                job = rows[0]
                job["rating"] = json.loads(job["rating"])
                return self.reply(200, job, head=head)
        name = "index.html" if path == "/" else path[1:]
        if name not in STATIC:
            return self.reply(404, {"error": "页面不存在"}, head=head)
        mime = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
        self.reply(200, (ROOT / name).read_bytes(), mime, head)

    def state(self):
        s = self.server.store
        cfg = s.settings()
        visible = {k: v for k, v in cfg.items() if k not in SECRETS}
        visible.update({k + "_set": bool(cfg[k]) for k in SECRETS})
        current = rating_context(cfg, s.get("resume"))
        joblist = s.rows(
            """SELECT id,title,company,url,location,source,first_seen,active,
             COALESCE(json_extract(rating,'$.context')=?,0) AS current,
             json_extract(rating,'$.score') score,json_extract(rating,'$.eligible') eligible,
             json_extract(rating,'$.pref_match') pref_match,
             json_extract(rating,'$.mode') mode,json_extract(rating,'$.reason') reason FROM jobs
             ORDER BY current DESC,score DESC,active DESC,pref_match DESC,seen DESC,id""",
            (current,),
        )
        runs = s.rows("SELECT * FROM runs ORDER BY id DESC LIMIT 30")
        for run in runs:
            run["reports"] = json.loads(run["reports"])
        actions = s.rows(
            """SELECT a.id,a.job,a.kind,a.state,a.detail,a.created,a.updated,j.title,j.company,j.url
                 FROM actions a JOIN jobs j ON a.job=j.id ORDER BY updated DESC LIMIT 150"""
        )
        contacted = s.rows("SELECT count(*) n FROM actions WHERE kind='打招呼' AND state='已完成'")[0]["n"]
        return dict(
            settings=visible,
            desktop_available=os.name == "nt",
            profile=s.profile(),
            jobs=joblist,
            runs=runs,
            actions=actions,
            contacted=contacted,
            busy=self.server.engine.lock.locked(),
            phase=self.server.engine.current,
            due=s.get("due", 0),
            logins={
                **s.get("logins", {}),
                **{k: platforms.VERIFY_NOTE for k in s.get("verification_pending", {})},
            },
            search_plugins={"searxng": searxng.state(s)},
            platforms={k: v["name"] for k, v in platforms.PLATFORMS.items()},
        )

    def do_POST(self):
        if not self.host_ok():
            return self.reply(403, {"error": "来源无效"})
        if not self.path.startswith("/api/"):
            return self.reply(405, {"error": "仅支持 API 请求"})
        if (
            self.headers.get("Origin") != "http://" + self.headers.get("Host", "")
            or self.headers.get_content_type() != "application/json"
        ):
            return self.reply(403, {"error": "请通过本机工作台提交"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8_500_000:
                raise ValueError("文件过大或内容为空（上限 6 MB）")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("请求格式无效")
            result = self.submit(data)
            self.reply(200, result or {"ok": True})
        except (ValueError, binascii.Error, KeyError) as e:
            self.reply(400, {"error": message(e)})
        except Exception as e:
            self.reply(400, {"error": message(e)})

    def submit(self, data):
        server = self.server
        s = server.store
        if self.path == "/api/notifications/test-desktop":
            return {"note": notifications.desktop_notice("求职工作台 · 测试提醒", "这是本机测试通知。")}
        if self.path == "/api/run":
            if "platform" in data:
                key = data["platform"]
                if (
                    key not in ("selected", *platforms.PLATFORMS)
                    or type(data.get("interactive", False)) is not bool
                ):
                    raise ValueError("请选择可复查的平台")
                if data.get("interactive") and key == "selected":
                    raise ValueError("一次只能打开一个官网复查窗口")
                server.engine.start("只读复查", key, data.get("interactive", False))
            else:
                server.engine.start()
        elif self.path in ("/api/models/scan", "/api/models/use", "/api/models/test"):
            cfg = model_draft(s, data)
            key = cfg["provider"]
            if self.path == "/api/models/scan":
                if key in backends.AGENTS:
                    names, note = backends.cli_models(key)
                elif key in backends.APIS:
                    names, note = backends.api_models(cfg), "供应商返回的模型目录；可用权限以连接测试为准"
                else:
                    names, note = [], "本地匹配无需模型"
                return {
                    "models": list(dict.fromkeys(n for n in names if isinstance(n, str) and n)),
                    "note": note,
                }
            if self.path == "/api/models/test":
                if key != "local":
                    validate_settings(cfg, s.settings())
                    if models.model_json(cfg, '只输出 {"ok":true}', {}).get("ok") is not True:
                        raise ValueError("模型未返回预期 JSON")
                return {"note": "测试成功"}
            with server.config_lock:
                if server.engine.lock.locked():
                    raise ValueError("本轮巡查结束后可切换模型")
                if key in backends.AGENTS:
                    backends.cli_executable(key)
                # Save the legacy active connection the first time a user switches away.
                previous = s.settings()
                updated = validate_settings(cfg, previous)
                if previous["provider"] != key and not s.get("backend." + previous["provider"]):
                    s.put("settings", previous)
                s.put("settings", updated)
                server.engine.refresh_rankings()
        elif self.path == "/api/plugins/searxng":
            return searxng.operate(s, data.get("action"), data)
        elif self.path == "/api/pause":
            server.engine.pause()
        elif self.path == "/api/retry":
            with s.db() as db:
                changed = db.execute(
                    """UPDATE actions SET state='待处理',detail='用户已核实未送达，等待下轮巡查',updated=?
                                  WHERE id=? AND state='待核对'""",
                    (now(), str(data["id"])),
                ).rowcount
                if not changed:
                    raise ValueError("此动作当前无法重新排队")
        elif self.path == "/api/resume":
            name = Path(str(data["name"]).replace("\\", "/")).name[:180]
            body = base64.b64decode(data["data"], validate=True)
            if not 0 < len(body) <= 6 * 1024 * 1024:
                raise ValueError("简历文件上限为 6 MB")
            text = models.resume_text(name, body)
            with server.config_lock:
                if server.engine.lock.locked():
                    raise ValueError("请在本轮巡查结束后更换简历")
                s.save_resume(name, body, text)
                server.engine.refresh_rankings()
        elif self.path == "/api/settings":
            with server.config_lock:
                if server.engine.lock.locked():
                    raise ValueError("本轮巡查正在使用当前设置，结束后可修改")
                previous = s.settings()
                if {"provider", "api_key", "api_url", "model"}.intersection(data):
                    draft = model_draft(s, {"provider": previous["provider"], **data})
                    data = {**data, **draft}
                    if draft["provider"] != previous["provider"] and not s.get(
                        "backend." + previous["provider"]
                    ):
                        s.put("settings", previous)
                cfg = validate_settings(data, previous)
                if cfg["scheduled"] and not s.profile():
                    raise ValueError("开启定时任务前请先上传简历")
                if cfg["interval"] != s.settings()["interval"] or (
                    cfg["scheduled"] and not s.settings()["scheduled"]
                ):
                    s.put("due", now() + cfg["interval"] * 60)
                s.put("settings", cfg)
                if rating_context(cfg, s.get("resume")) != rating_context(previous, s.get("resume")):
                    server.engine.refresh_rankings()
        elif self.path == "/api/login":
            key = data.get("platform")
            if key not in platforms.PLATFORMS:
                raise ValueError("未知平台")
            search = s.get("last_search." + key) if data.get("purpose") == "search" else None
            if data.get("purpose") == "search" and not search:
                search = platforms.platform_term(
                    s.settings()["keywords"] or "招聘", s.settings()["location"], True
                )

            def status(label):
                with server.config_lock:
                    s.put("logins", {**s.get("logins", {}), key: label})

            def launch():
                try:
                    platforms.login_window(s.root, key, status, search)
                    status("窗口已关闭 · 巡查时核验")
                except platforms.BrowserError as e:
                    if "Executable doesn't exist" in str(e):
                        status("缺少专属浏览器，请运行启动工作台.cmd 安装 Chromium")
                    elif "ProcessSingleton" in str(e) or "user data directory is already in use" in str(e):
                        status("该平台的浏览器正在使用，请切换到已打开的登录窗口")
                    else:
                        status("专属浏览器已退出，点击重新打开登录窗口")
                except Exception as e:
                    status(message(e))

            if platforms.LOCKS[key].locked():
                raise ValueError("平台浏览器正在使用")
            status("正在打开官方窗口")
            threading.Thread(target=launch, daemon=True).start()
        else:
            raise ValueError("未知操作")

    def log_message(self, *args):
        pass
