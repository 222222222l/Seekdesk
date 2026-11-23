import json
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

from . import locations, models, notifications, platforms, searxng, sources
from .storage import digest, job_id, now, rating_context


def message(exc):
    # Library errors can include URLs, prompts or credentials.
    if (type(exc) is ValueError or isinstance(exc, platforms.NeedsSetup)) and len(str(exc)) < 190:
        return str(exc)
    return f"调用失败（{type(exc).__name__}），请检查连接、登录状态或配置"


def email_notice(cfg, subject, body):
    host, port, sender = notifications.mail_connection(cfg)
    if not notifications.mail_ready(cfg):
        raise platforms.NeedsSetup("待配置通知邮箱，请填写邮箱和授权码，或选择自定义发件服务")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = cfg["email"]
    msg["Subject"] = subject
    msg.set_content(body)
    ssl_ctx = ssl.create_default_context()
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, 465, timeout=15, context=ssl_ctx)
        else:
            smtp = smtplib.SMTP(host, port, timeout=15)
    except OSError as exc:
        raise platforms.NeedsSetup("SMTP 连接失败，请检查邮件服务器和端口") from exc
    with smtp:
        try:
            if port != 465:
                smtp.starttls(context=ssl_ctx)
            if cfg["smtp_password"]:
                smtp.login(sender, cfg["smtp_password"])
        except (OSError, smtplib.SMTPException) as exc:
            raise platforms.NeedsSetup("SMTP 登录或 TLS 协商失败，请核对邮箱配置") from exc
        smtp.send_message(msg)
    return "SMTP 已接收"


class Engine:
    def __init__(self, store, config_lock=None):
        self.store = store
        self.config_lock = config_lock or threading.RLock()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.cancelled = threading.Event()
        self.current = ""
        self.refresh_rankings()
        self.store.execute("UPDATE runs SET status='中断',end=? WHERE end IS NULL", (now(),))
        self.store.execute(
            "UPDATE actions SET state='待核对', detail='进程中断，请核对平台记录' WHERE state='执行中'"
        )

    def start(self, reason="手动", only_platform=None, interactive=False):
        with self.config_lock:
            if not self.store.profile():
                raise ValueError("请先上传简历，再启动巡查")
            if reason == "定时" and not self.store.settings()["scheduled"]:
                return
            if not self.lock.acquire(False):
                raise ValueError("巡查正在运行")
            self.cancelled.clear()
            args = (reason, only_platform, interactive) if only_platform else (reason,)
            threading.Thread(target=self._run, args=args, daemon=True).start()

    def pause(self):
        with self.config_lock:
            self.cancelled.set()
            self.store.put("settings", {**self.store.settings(), "scheduled": False})

    def stopping(self):
        return self.cancelled.is_set() or self.stop.is_set()

    def _run(self, reason, *options):
        try:
            self.run(reason, *options)
        finally:
            self.current = ""
            self.lock.release()

    def scheduler(self):
        while not self.stop.wait(2):
            cfg = self.store.settings()
            if cfg["scheduled"] and not self.lock.locked() and now() >= self.store.get("due", 0):
                try:
                    self.start("定时")
                except ValueError:
                    self.stop.wait(3)

    def action(self, job, kind, identity, resume, thread, perform):
        if self.stopping():
            return
        aid = digest(job["id"], kind, identity)
        with self.store.db() as db:
            previous = db.execute("SELECT state FROM actions WHERE id=?", (aid,)).fetchone()
            if previous and previous["state"] != "待处理":
                return
            db.execute(
                """INSERT INTO actions VALUES(?,?,?,'执行中','',?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET state='执行中',updated=excluded.updated""",
                (aid, job["id"], kind, thread, resume, now(), now()),
            )
        try:
            receipt = perform()
            state, detail = "已完成", str(receipt)
            if kind == "打招呼":
                thread = str(receipt)
        except platforms.NeedsSetup as exc:
            state, detail = "待处理", message(exc)
        except Exception as exc:
            state, detail = "待核对", message(exc)
        self.store.execute(
            "UPDATE actions SET state=?,detail=?,thread=?,updated=? WHERE id=?",
            (state, detail, thread, now(), aid),
        )

    def inbox(self, cfg, report):
        sent = self.store.rows(
            """SELECT job,thread,resume FROM actions WHERE kind='打招呼' AND state='已完成'"""
        )
        for a in sent:
            if self.stopping():
                return
            job = self.store.rows("SELECT * FROM jobs WHERE id=?", (a["job"],))[0]
            if job["source"] not in cfg["platforms"]:
                continue
            try:
                for eid, event in platforms.incoming(self.store.root, job, a["thread"]):
                    if event == "contact_request":
                        self.action(
                            job,
                            "交换联系方式",
                            eid,
                            a["resume"],
                            a["thread"],
                            lambda: self.deliver_notice(
                                digest(job["id"], "交换联系方式", eid),
                                cfg,
                                job,
                                "联系方式请求 · " + job["company"],
                                f'{job["company"]} 的「{job["title"]}」会话收到了交换联系方式请求。\n请在原平台处理：{job["url"]}',
                            ),
                        )
                    elif event == "resume_request":
                        self.action(
                            job, "简历邀请", eid, a["resume"], a["thread"], lambda: "已收到平台邀请卡片"
                        )
            except Exception as exc:
                report("会话 · " + job["company"], 0, message(exc))
        if cfg["resume_auto"]:
            invitations = self.store.rows(
                """SELECT DISTINCT job,thread,resume FROM actions
                            WHERE kind='简历邀请' AND state='已完成'"""
            )
            for a in invitations:
                job = self.store.rows("SELECT * FROM jobs WHERE id=?", (a["job"],))[0]
                if job["source"] not in cfg["platforms"]:
                    continue

                def send():
                    original = self.store.rows("SELECT name,body FROM resumes WHERE id=?", (a["resume"],))[0]
                    return platforms.send_resume(self.store.root, job, a["thread"], original)

                self.action(job, "发送简历", a["thread"], a["resume"], a["thread"], send)

    def run(self, reason, only_platform=None, interactive=False):
        cfg = self.store.settings()
        if only_platform:
            cfg = {
                **cfg,
                "outreach": False,
                "boards": "",
                "sites": "",
                "platforms": cfg["platforms"] if only_platform == "selected" else [only_platform],
            }
        profile = self.store.profile(full=True)
        rid = self.store.execute(
            "INSERT INTO runs(start,status,reports,reason) VALUES(?,'运行中','[]',?)", (now(), reason)
        )
        reports = []
        pending = self.store.get("verification_pending", {})

        def held_url(url):
            host = (urlsplit(url).hostname or "").lower()
            return any(
                host == platforms.PLATFORMS[k]["domain"]
                or host.endswith("." + platforms.PLATFORMS[k]["domain"])
                for k in pending
                if k in platforms.PLATFORMS
            )

        def report(source, count, error="", evaluated=None, filtered=0, leads=0):
            reports.append(
                dict(
                    source=source,
                    count=count,
                    error=error,
                    evaluated=evaluated,
                    filtered=filtered,
                    retained=True,
                    leads=leads,
                )
            )
            self.store.execute(
                "UPDATE runs SET reports=? WHERE id=?", (json.dumps(reports, ensure_ascii=False), rid)
            )

        try:
            self.current = "检查 HR 会话"
            if not only_platform:
                self.inbox({**cfg, "platforms": [k for k in cfg["platforms"] if k not in pending]}, report)
            if self.stopping():
                return
            self.current = "生成岗位搜索词"
            query = models.search_query(cfg, profile["text"])
            providers = []
            for name in dict.fromkeys(cfg["boards"].split()):
                providers.append((name, lambda n=name: sources.board(n), True))
            for url in dict.fromkeys(cfg["sites"].split()):
                if held_url(url):
                    report("官网 · " + urlsplit(url).netloc, 0, platforms.VERIFY_NOTE)
                    continue
                providers.append(
                    (
                        "官网 · " + urlsplit(url).netloc,
                        lambda u=url: sources.page_jobs(u, "企业官网", True),
                        False,
                    )
                )
            for key in cfg["platforms"]:
                term = platforms.platform_term(query, cfg["location"], cfg["provider"] == "local")
                self.store.put("last_search." + key, term)

                def collect_platform(k=key, q=term):
                    if interactive:
                        self.current = "请在官网窗口完成验证并保留搜索结果页，工作台将自动读取"
                        jobs = platforms.discover(self.store.root, k, q, interactive=True)
                        if k in pending:
                            del pending[k]
                            self.store.put("verification_pending", pending)
                        return jobs
                    if k in pending:
                        raise platforms.VerificationRequired()
                    return platforms.discover(self.store.root, k, q)

                providers.append((key, collect_platform, False))
            searches = [("Brave", cfg["brave_key"]), ("Tavily", cfg["tavily_key"])]
            local_search = self.store.get(searxng.KEY, {})
            if local_search.get("enabled"):
                searches.append(("SearXNG", local_search["endpoint"]))
            for name, connection in [] if only_platform else searches:
                if not connection:
                    continue

                def search(n=name, address=connection):
                    links = sources.search_links(n, address, query + " 企业官网 招聘 careers jobs")
                    found = []
                    for url in dict.fromkeys(links):
                        if self.stopping():
                            break
                        if held_url(url):
                            continue
                        try:
                            found.extend(sources.page_jobs(url, "搜索发现", True))
                        except Exception as exc:
                            report(n + " · " + str(urlsplit(url).hostname), 0, message(exc))
                    return found

                providers.append((name, search, False))
            if not providers:
                raise ValueError("请至少启用一个岗位来源")
            for label, collect, complete in providers:
                if self.stopping():
                    break
                self.current = "读取岗位 · " + label
                try:
                    jobs = collect()
                    if complete:
                        active = {str(j["external_id"]) for j in jobs}
                        with self.store.db() as db:
                            stored = db.execute(
                                "SELECT id,external_id FROM jobs WHERE source=? AND active=1", (label,)
                            ).fetchall()
                            db.executemany(
                                "UPDATE jobs SET active=0 WHERE id=?",
                                [(j["id"],) for j in stored if j["external_id"] not in active],
                            )
                    matching = [j for j in jobs if self.matches_prefs(cfg, j)]
                    processed = self.evaluate(cfg, profile, jobs, report)
                    leads = sum(bool(j.get("partial")) for j in jobs)
                    report(
                        label, len(jobs), evaluated=processed, filtered=len(jobs) - len(matching), leads=leads
                    )
                    self.current = f"{label} · 已评估 {processed} 个岗位"
                except Exception as exc:
                    if isinstance(exc, platforms.VerificationRequired) and label in platforms.PLATFORMS:
                        if label not in pending:
                            pending[label] = now()
                            self.store.put("verification_pending", pending)
                        matching = [j for j in exc.jobs if self.matches_prefs(cfg, j)]
                        processed = self.evaluate({**cfg, "outreach": False}, profile, exc.jobs, report)
                        report(
                            label,
                            len(exc.jobs),
                            message(exc),
                            evaluated=processed,
                            filtered=len(exc.jobs) - len(matching),
                            leads=sum(bool(j.get("partial")) for j in exc.jobs),
                        )
                    else:
                        report(label, 0, message(exc))
                    if label in platforms.PLATFORMS and local_search.get("enabled") and not self.stopping():
                        try:
                            indexed = platforms.indexed_jobs(label, term, local_search["endpoint"])
                            matching = [j for j in indexed if self.matches_prefs(cfg, j)]
                            processed = self.evaluate(cfg, profile, indexed, report)
                            report(
                                "SearXNG 索引 · " + platforms.PLATFORMS[label]["name"],
                                len(indexed),
                                evaluated=processed,
                                leads=sum(bool(j.get("partial")) for j in indexed),
                                filtered=len(indexed) - len(matching),
                            )
                        except Exception as err:
                            report("SearXNG 索引 · " + platforms.PLATFORMS[label]["name"], 0, message(err))
            # A notification awaiting SMTP should survive disappearance of the source posting.
            for a in self.store.rows(
                """SELECT a.id AS action_id,a.rowid AS action_seq,a.kind,j.* FROM actions a JOIN jobs j ON a.job=j.id
                        WHERE a.state='待处理' AND a.kind IN ('材料准备','交换联系方式')"""
            ):
                if (
                    not only_platform
                    and not self.stopping()
                    and (
                        notifications.mail_ready(cfg)
                        or cfg["windows_notifications"]
                        and a["action_seq"] > self.store.get("desktop_after_action", 0)
                    )
                ):
                    self.retry_notice(a, cfg)
        except Exception as exc:
            report("巡查", 0, message(exc))
        finally:
            errors = sum(bool(r["error"]) for r in reports)
            status = "部分完成" if errors and errors < len(reports) else "失败" if errors else "完成"
            if self.cancelled.is_set():
                status = "已暂停"
            self.store.execute("UPDATE runs SET end=?,status=? WHERE id=?", (now(), status, rid))
            if not only_platform:
                self.store.put("due", now() + self.store.settings()["interval"] * 60)

    def retry_notice(self, job, cfg):
        kind = job["kind"]
        body = (
            self.notice_body(job, json.loads(job["rating"]))
            if kind == "材料准备"
            else "请在原会话核对联系方式请求。\n" + job["url"]
        )
        aid = job["action_id"]
        self.store.execute("UPDATE actions SET state='执行中' WHERE id=?", (aid,))
        try:
            detail = self.deliver_notice(aid, cfg, job, kind + " · " + job["company"], body)
            state = "已完成"
        except platforms.NeedsSetup as exc:
            state, detail = "待处理", message(exc)
        except Exception as exc:
            state, detail = "待核对", message(exc)
        self.store.execute(
            "UPDATE actions SET state=?,detail=?,updated=? WHERE id=?", (state, detail, now(), aid)
        )

    @staticmethod
    def notice_body(job, rating):
        return f'{job["company"]} · {job["title"]}\n{rating["materials"]}\n\n岗位原文：{job["url"]}'

    def deliver_notice(self, aid, cfg, job, subject, body):
        receipts = {
            r["channel"]: r["receipt"]
            for r in self.store.rows("SELECT channel,receipt FROM notice_receipts WHERE action_id=?", (aid,))
        }
        channels = {}
        if cfg["windows_notifications"]:
            row = self.store.rows("SELECT rowid AS seq FROM actions WHERE id=?", (aid,))
            if row and row[0]["seq"] > self.store.get("desktop_after_action", 0):
                channels["desktop"] = lambda: notifications.desktop_notice(
                    subject, job["title"] + " · 详情见工作台「沟通与通知」"
                )
        if notifications.mail_ready(cfg) or (cfg["email_enabled"] and not channels and not receipts):
            channels["mail"] = lambda: email_notice(cfg, subject, body)
        errors = []
        for channel, send in channels.items():
            if channel in receipts:
                continue
            try:
                receipts[channel] = send()
                self.store.execute(
                    "INSERT OR REPLACE INTO notice_receipts VALUES(?,?,?)", (aid, channel, receipts[channel])
                )
            except Exception as exc:
                errors.append(exc)
        if errors:
            uncertain = next((e for e in errors if not isinstance(e, platforms.NeedsSetup)), None)
            if uncertain is not None:
                raise uncertain
            raise platforms.NeedsSetup("；".join([*receipts.values(), *(message(e) for e in errors)]))
        return "；".join(receipts.values()) or "已记录在工作台"

    @staticmethod
    def matches_prefs(cfg, job):
        words = [w.casefold() for w in re.split(r"[\s,，、]+", cfg["keywords"]) if w]
        place_ok = locations.matches(cfg["location"], job["location"], job["description"])
        if job.get("partial") and not job["location"]:
            place_ok = True  # Retain for human review when the source has not supplied a location.
        return place_ok and (
            not words or any(w in (job["title"] + " " + job["description"]).casefold() for w in words)
        )

    @staticmethod
    def baseline(cfg, local, job):
        rating = dict(local)
        if job.get("partial"):
            rating.update(
                mode="index" if job.get("indexed") else "listing",
                eligible=False,
                materials="请打开岗位链接核对原文后准备材料。",
            )
        elif cfg["provider"] != "local":
            rating.update(mode="preliminary", eligible=False)
        if not job.get("partial"):
            rating["materials"] = sources.prep_note(job["description"])
        return rating

    def refresh_rankings(self):
        """Update saved scores for the active resume without visiting sources or triggering actions."""
        with self.config_lock:
            profile = self.store.profile(full=True)
            if not profile:
                return
            cfg = self.store.settings()
            context = rating_context(cfg, profile["id"])
            signals = models.resume_signals(profile["text"])
            with self.store.db() as db:
                rows = db.execute(
                    """SELECT * FROM jobs WHERE json_extract(rating,'$.context') IS NOT ?
                               OR json_extract(rating,'$.pref_match') IS NULL""",
                    (context,),
                ).fetchall()
                for row in rows:
                    job = dict(row)
                    old = json.loads(job["rating"])
                    partial = old.get("mode") in ("index", "listing")
                    if partial:
                        job.update(partial=True, indexed=old["mode"] == "index")
                    fingerprint = job["fingerprint"]
                    if old.get("context") == context and not partial:
                        rating = old
                    else:
                        rating = self.baseline(cfg, models.local_rating(profile["text"], job, signals), job)
                        fingerprint = ""
                    rating.update(context=context, pref_match=self.matches_prefs(cfg, job))
                    db.execute(
                        "UPDATE jobs SET rating=?,fingerprint=? WHERE id=?",
                        (json.dumps(rating, ensure_ascii=False), fingerprint, job["id"]),
                    )

    def evaluate(self, cfg, profile, jobs, report):
        # Rank locally first to bound model cost. Subsequent patrols advance past cached evaluations.
        records = []
        signals = models.resume_signals(profile["text"])
        context = rating_context(cfg, profile["id"])
        for job in jobs:
            records.append((models.local_rating(profile["text"], job, signals), job))
        records.sort(key=lambda r: r[0]["score"], reverse=True)
        budget = 8
        evaluated = 0
        for local, job in records:
            if self.stopping():
                break
            jid = job_id(job)
            if job.get("partial"):
                previous = self.store.rows("SELECT rating FROM jobs WHERE id=?", (jid,))
                if previous:
                    mode = json.loads(previous[0]["rating"]).get("mode")
                    if mode not in ("index", "listing") or (mode == "listing" and job.get("indexed")):
                        continue
            fp = digest(context, job)
            cached = self.store.rows("SELECT rating FROM jobs WHERE id=? AND fingerprint=?", (jid, fp))
            pref_match = self.matches_prefs(cfg, job)
            saved = json.loads(cached[0]["rating"]) if cached else None
            if saved and saved.get("mode") != "preliminary" and "pref_match" in saved:
                rating = saved
            else:
                rating = self.baseline(cfg, local, job)
                if rating["mode"] == "preliminary" and pref_match and budget > 0:
                    budget -= 1
                    self.current = "模型评估 · " + job["title"]
                    try:
                        assessed = models.rate(cfg, profile["text"], job)
                        assessed["materials"] = rating["materials"] + "\n模型建议：" + assessed["materials"]
                        rating = assessed
                    except Exception as exc:
                        report(job["company"] + " · " + job["title"], 0, message(exc))
            rating.update(context=context, pref_match=pref_match)
            self.store.upsert(job, rating, fp)
            evaluated += int(not job.get("partial"))
            if not pref_match or not rating["eligible"] or rating["score"] < cfg["threshold"]:
                continue
            job = {**job, "id": jid}
            if job["source"] not in platforms.PLATFORMS:
                self.action(
                    job,
                    "材料准备",
                    job["url"],
                    profile["id"],
                    "",
                    lambda: self.deliver_notice(
                        digest(job["id"], "材料准备", job["url"]),
                        cfg,
                        job,
                        "岗位材料准备 · " + job["company"],
                        self.notice_body(job, rating),
                    ),
                )
                continue
            if not cfg["outreach"] or job.get("indexed"):
                continue
            local_midnight = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
            total = self.store.rows(
                """SELECT COUNT(*) AS n FROM actions WHERE kind='打招呼' AND state IN
                                 ('执行中','已完成','待核对') AND updated>=?""",
                (local_midnight,),
            )[0]["n"]
            if total >= cfg["daily_limit"]:
                continue
            proof = rating["evidence"].splitlines()[0][:180] if rating["evidence"] else ""
            if not proof or re.search(r"@|\d{7,}|https?://", proof):
                continue
            greeting = (
                f'您好，我对贵公司的{job["title"]}岗位感兴趣。我的相关经历是：{proof}。期待和您进一步交流。'
            )
            self.action(
                job,
                "打招呼",
                "once",
                profile["id"],
                "",
                lambda: platforms.greet(self.store.root, job, greeting),
            )
        return evaluated
