import json
import os
import re
import smtplib
import ssl
import threading
import time
from contextlib import contextmanager
from email.message import EmailMessage
from urllib.parse import urlsplit

from . import filter_assistant, locations, models, notifications, platforms, regions, searxng, sources
from .storage import digest, job_id, now, rating_context


def message(exc):
    # Library errors can include URLs, prompts or credentials.
    if (type(exc) is ValueError or isinstance(exc, (platforms.NeedsSetup, sources.SourceHTTPError, sources.RedirectLimit))) and len(str(exc)) < 190:
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
        self.review_window_run = None
        self.review_done = threading.Event()
        self.store.execute(
            "INSERT OR IGNORE INTO kv(key,value) SELECT 'delivery_notice_since',COALESCE(MAX(rowid),0) FROM actions"
        )
        self.refresh_rankings()
        self.store.execute("UPDATE runs SET status='中断',end=? WHERE end IS NULL", (now(),))
        self.store.execute("UPDATE actions SET state='待处理' WHERE state='执行中' AND kind='投递提醒'")
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

    @contextmanager
    def visible_review(self, rid, key, count):
        with self.config_lock:
            self.review_done.clear()
            self.current = (
                f"{platforms.PLATFORMS[key]['name']}已读到 {count} 张卡片。可打开岗位详情核对，"
                "关闭复查窗口或点「完成窗口核对」继续。窗口最多保留三分钟"
            )
            self.review_window_run = rid
        try:
            yield lambda: self.review_done.is_set() or self.stopping()
        finally:
            with self.config_lock:
                self.review_window_run = None
                self.current = "正在整理已读取的岗位"

    def finish_review(self, rid):
        with self.config_lock:
            if type(rid) is not int or rid != self.review_window_run:
                raise ValueError("该复查窗口已结束")
            self.review_done.set()

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
        except platforms.VerificationRequired as exc:
            self.hold_platform(job["source"])
            state, detail = "待处理", message(exc)
        except platforms.NeedsSetup as exc:
            state, detail = "待处理", message(exc)
        except Exception as exc:
            state, detail = "待核对", message(exc)
        self.store.execute(
            "UPDATE actions SET state=?,detail=?,thread=?,updated=? WHERE id=?",
            (state, detail, thread, now(), aid),
        )

    def hold_platform(self, key):
        pending = self.store.get("verification_pending", {})
        if key not in pending:
            self.store.put("verification_pending", {**pending, key: now()})

    def inbox(self, cfg, report):
        sent = self.store.rows(
            """SELECT job,thread,resume FROM actions WHERE kind='打招呼' AND state='已完成'"""
        )
        for a in sent:
            if self.stopping():
                return
            job = self.store.rows("SELECT * FROM jobs WHERE id=?", (a["job"],))[0]
            if job["source"] not in cfg["platforms"] or job["source"] in self.store.get("verification_pending", {}):
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
            except platforms.VerificationRequired as exc:
                self.hold_platform(job["source"])
                report("会话 · " + job["company"], 0, message(exc))
            except Exception as exc:
                report("会话 · " + job["company"], 0, message(exc))
        if cfg["resume_auto"]:
            invitations = self.store.rows(
                """SELECT DISTINCT job,thread,resume FROM actions
                            WHERE kind='简历邀请' AND state='已完成'"""
            )
            for a in invitations:
                job = self.store.rows("SELECT * FROM jobs WHERE id=?", (a["job"],))[0]
                if job["source"] not in cfg["platforms"] or job["source"] in self.store.get("verification_pending", {}):
                    continue

                def send():
                    original = self.store.rows("SELECT name,body FROM resumes WHERE id=?", (a["resume"],))[0]
                    return platforms.send_resume(self.store.root, job, a["thread"], original)

                self.action(job, "发送简历", a["thread"], a["resume"], a["thread"], send)
        # Derive reminders from durable receipts so a restart after sending
        # cannot lose the notification or cause a second submission.
        for a in self.store.rows(
            """SELECT a.id AS event,a.kind,a.resume,a.thread,j.* FROM actions a JOIN jobs j ON j.id=a.job
               WHERE a.kind IN ('简历邀请','发送简历') AND a.state='已完成' AND a.rowid>? AND NOT EXISTS
               (SELECT 1 FROM actions n WHERE n.job=a.job AND n.kind=? AND n.thread=a.id)""",
            (self.store.get("delivery_notice_since", 0), "投递提醒"),
        ):
            if a["kind"] == "简历邀请" and cfg["resume_auto"]:
                continue
            self.action(
                a, "投递提醒", a["event"], a["resume"], a["event"],
                lambda: self.delivery_notice(digest(a["id"], "投递提醒", a["event"]), cfg, a, a["kind"]),
            )

    def delivery_notice(self, aid, cfg, job, event_kind):
        subject = "简历已发送" if event_kind == "发送简历" else "HR 邀请投递简历"
        body = f'{job["company"]} · {job["title"]}\n{subject}，请在平台会话中核对。\n{job["url"]}'
        return self.deliver_notice(aid, cfg, job, subject + " · " + job["company"], body, required=True)

    def run(self, reason, only_platform=None, interactive=False):
        cfg = self.store.settings()
        cfg["platforms"] = [k for k in cfg["platforms"] if regions.includes(cfg, k)]
        if only_platform in platforms.PLATFORMS and not regions.includes(cfg, only_platform):
            raise ValueError("该平台不在已选择的来源地区内")
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
        visible_navigation = {}

        def held_url(url):
            host = (urlsplit(url).hostname or "").lower()
            return any(
                host == platforms.PLATFORMS[k]["domain"]
                or host.endswith("." + platforms.PLATFORMS[k]["domain"])
                for k in pending
                if k in platforms.PLATFORMS
            )

        def report(source, count, error="", evaluated=None, filtered=0, leads=0, **results):
            if interactive and source == only_platform and visible_navigation:
                results["navigation"] = dict(visible_navigation)
            reports.append(
                dict(
                    source=source,
                    count=count,
                    error=error,
                    evaluated=evaluated,
                    filtered=filtered,
                    retained=True,
                    leads=leads,
                    **results,
                )
            )
            self.store.execute(
                "UPDATE runs SET reports=? WHERE id=?", (json.dumps(reports, ensure_ascii=False), rid)
            )

        try:
            self.current = "检查 HR 会话"
            if not only_platform:
                self.inbox({**cfg, "platforms": [k for k in cfg["platforms"] if k not in pending]}, report)
                pending = self.store.get("verification_pending", {})
            if self.stopping():
                return
            self.current = "生成岗位搜索词"
            query = models.search_query(cfg, profile["text"], sequence=rid - 1)
            rules = cfg.get("filter_assistant", {}).get("rules")
            places = rules["cities"] if rules else locations.choices(cfg["location"])
            variants = rules["queries"] if rules else models.keyword_options(cfg["keywords"])
            web_place = places[((rid - 1) // max(1, len(variants))) % len(places)] if places else ""
            providers = []
            for name in dict.fromkeys(cfg["boards"].split()):
                if regions.includes(cfg, name):
                    providers.append((name, lambda n=name: sources.board(n), True))

            site_urls = [] if only_platform else (
                cfg["sites"].split() + cfg["domestic_sites"].split() + cfg["overseas_sites"].split()
            )
            explicit_hosts = {urlsplit(url).hostname for url in site_urls}
            employer_hosts = {site[0] for site in sources.EMPLOYER_INDEXES.values()}

            def public_site(url):
                host = (urlsplit(url).hostname or "").lower().rstrip(".")
                if host in employer_hosts and host not in explicit_hosts:
                    return False
                return regions.includes(cfg, "", url) and not any(
                    host == site["domain"] or host.endswith("." + site["domain"]) for site in platforms.INDEX_SITES.values()
                )

            for url in dict.fromkeys(site_urls):
                if not regions.includes(cfg, "", url):
                    continue
                if held_url(url):
                    report("官网 · " + urlsplit(url).netloc, 0, phase="本地检查", outcome="skipped", note="本地已跳过，该平台后台直连仍暂停")
                    continue
                providers.append(
                    (
                        "官网 · " + urlsplit(url).netloc,
                        lambda u=url: sources.page_jobs(u, "企业官网", True, allow_url=public_site),
                        False,
                    )
                )
            term = platforms.platform_term(query, "、".join(places))
            if not only_platform and cfg["tencent_careers"] and regions.includes(cfg, "腾讯社招"):
                providers.append(("腾讯社招", lambda: sources.tencent_jobs(query), False))
            employer_scan = {}
            if not only_platform and cfg["bosch_china"] and regions.includes(cfg, "博世中国岗位"):
                def collect_bosch():
                    cursor = "bosch.offset." + digest(query)
                    jobs = sources.bosch_china_jobs(query, self.store.get(cursor, 0), stats=employer_scan)
                    self.store.put(cursor, employer_scan["next"])
                    return jobs

                providers.append(("博世中国岗位", collect_bosch, False))
            for key in cfg["platforms"]:
                self.store.put("last_search." + key, term)

                def collect_platform(k=key, q=term):
                    if interactive:
                        self.current = "正在读取官网当前页面，遇到登录或验证提示会停止"
                        return platforms.discover(
                            self.store.root, k, q, interactive=True,
                            review_window=lambda count: self.visible_review(rid, k, count),
                            diagnostics=visible_navigation,
                        )
                    return platforms.discover(self.store.root, k, q)

                providers.append((key, collect_platform, False))
            searches = [("Brave", cfg["brave_key"]), ("Tavily", cfg["tavily_key"])]
            local_search = self.store.get(searxng.KEY, {})
            index_stats = {}
            if local_search.get("enabled"):
                searches.append(("SearXNG", local_search["endpoint"]))
                for employer in cfg["employer_indexes"] if not only_platform else []:
                    if regions.includes(cfg, employer):
                        label = "官网索引 · " + employer
                        stats = index_stats[label] = {}
                        providers.append((label, lambda n=employer, s=stats: sources.employer_index(n, term, local_search["endpoint"], stats=s), False))
                for key in cfg["platform_indexes"]:
                    if interactive or not regions.includes(cfg, key) or (only_platform and key not in cfg["platforms"]):
                        continue
                    label = "SearXNG 索引 · " + platforms.INDEX_SITES[key]["name"]
                    stats = index_stats[label] = {}
                    providers.append((label, lambda k=key, s=stats: platforms.indexed_jobs(k, term, local_search["endpoint"], stats=s), False))
            for name, connection in [] if only_platform else searches:
                if not connection:
                    continue

                def search(n=name, address=connection):
                    links = sources.search_links(
                        n, address, f"{query} {web_place} 企业官网 招聘 careers jobs"
                    )
                    found = []
                    for url in dict.fromkeys(links):
                        if self.stopping():
                            break
                        if not public_site(url):
                            continue
                        try:
                            found.extend(sources.page_jobs(url, "搜索发现", True, allow_url=public_site))
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
                phase = ("可见搜索页" if interactive else "公开搜索页") if label in platforms.PLATFORMS else (
                    "第三方索引" if label in index_stats else "公开来源")
                if not interactive and label in platforms.PLATFORMS and label in self.store.get("verification_pending", {}):
                    report(label, 0, phase="本地检查", outcome="skipped", note="本地已跳过，该平台后台直连仍暂停")
                    continue
                try:
                    unique = {}
                    for job in collect():
                        if regions.includes(cfg, job["source"], job["url"]):
                            jid = job_id(job)
                            if jid not in unique or unique[jid].get("partial"):
                                unique[jid] = job
                    jobs = list(unique.values())
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
                    results = self.evaluate(cfg, profile, jobs, report)
                    leads = sum(bool(j.get("partial")) for j in jobs)
                    note = ""
                    if label == "博世中国岗位":
                        note = f"本页检查 {employer_scan['scanned']} 个官网清单条目，已按本轮搜索词核对标题和地点"
                    stats = index_stats.get(label)
                    if stats:
                        note = f"索引累计返回 {stats['returned']} 条候选，其中官方职位详情链接 {stats['official_details']} 条"
                    if interactive and label in platforms.PLATFORMS:
                        note = "本轮读取了搜索页已有内容和手动打开的岗位详情"
                        if label in pending:
                            note += "，此前待验证的后台直连继续暂停"
                    report(label, len(jobs), filtered=len(jobs) - len(matching), leads=leads, note=note,
                           phase=phase, outcome="ok" if jobs else "empty", **results)
                    self.current = f"{label} · 已匹配 {results['evaluated']} 个岗位"
                except Exception as exc:
                    if isinstance(exc, (platforms.IncompleteReview, sources.EmployerDetailsUnavailable)):
                        if isinstance(exc, platforms.VerificationRequired):
                            self.hold_platform(label)
                            pending = self.store.get("verification_pending", {})
                        matching = [j for j in exc.jobs if self.matches_prefs(cfg, j)]
                        results = self.evaluate({**cfg, "outreach": False}, profile, exc.jobs, report)
                        report(
                            label,
                            len(exc.jobs),
                            str(exc),
                            **results,
                            filtered=len(exc.jobs) - len(matching),
                            leads=sum(bool(j.get("partial")) for j in exc.jobs),
                            phase=getattr(exc, "phase", "") or ("企业岗位详情" if isinstance(exc, sources.EmployerDetailsUnavailable) else phase),
                            outcome=getattr(exc, "outcome", "incomplete"),
                        )
                    else:
                        report(label, 0, message(exc), phase=phase, outcome="failed")
            # A notification awaiting SMTP should survive disappearance of the source posting.
            for a in self.store.rows(
                """SELECT a.id AS action_id,a.rowid AS action_seq,a.kind,a.thread,j.* FROM actions a JOIN jobs j ON a.job=j.id
                        WHERE a.state='待处理' AND a.kind IN ('材料准备','交换联系方式','投递提醒')"""
            ):
                if (
                    not only_platform
                    and not self.stopping()
                    and (
                        notifications.mail_ready(cfg)
                        or (a["kind"] == "投递提醒" and os.name == "nt")
                        or cfg["windows_notifications"]
                        and (a["action_seq"] > self.store.get("desktop_after_action", 0) or a["kind"] == "投递提醒")
                    )
                ):
                    self.retry_notice(a, cfg)
        except Exception as exc:
            report("巡查", 0, message(exc))
        finally:
            errors = sum(bool(r["error"]) for r in reports)
            has_results = any(r["count"] > 0 for r in reports)
            status = "完成"
            if errors:
                status = "部分完成" if has_results or errors < len(reports) else "失败"
            elif reports and all(r.get("outcome") == "skipped" for r in reports):
                status = "已跳过"
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
            if kind == "投递提醒":
                event = self.store.rows("SELECT kind FROM actions WHERE id=?", (job["thread"],))[0]
                detail = self.delivery_notice(aid, cfg, job, event["kind"])
            else:
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

    def deliver_notice(self, aid, cfg, job, subject, body, *, required=False):
        receipts = {
            r["channel"]: r["receipt"]
            for r in self.store.rows("SELECT channel,receipt FROM notice_receipts WHERE action_id=?", (aid,))
        }
        channels = {}
        if cfg["windows_notifications"] or (required and os.name == "nt" and not notifications.mail_ready(cfg)):
            row = self.store.rows("SELECT rowid AS seq FROM actions WHERE id=?", (aid,))
            if row and (required or row[0]["seq"] > self.store.get("desktop_after_action", 0)):
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
        if required and not receipts:
            raise platforms.NeedsSetup("投递提醒待发送，请启用 Windows 通知或配置邮箱")
        return "；".join(receipts.values()) or "已记录在工作台"

    @staticmethod
    def matches_prefs(cfg, job):
        rules = cfg.get("filter_assistant", {}).get("rules")
        if rules:
            reasons, _ = filter_assistant.assess(rules, job)
            return not reasons
        words = models.keyword_options(cfg["keywords"])
        text = job["title"] + " " + models.role_description(job["description"])
        return locations.matches(cfg["location"], job["location"], job["description"], job["title"]) and (
            not words or any(models.keyword_hit(w, text) for w in words)
        )

    @staticmethod
    def apply_preferences(rating, cfg, job, context):
        rules = cfg.get("filter_assistant", {}).get("rules")
        if rules:
            reasons, preferred = filter_assistant.assess(rules, job)
            matches = not reasons
            rating["preferred"] = preferred
        else:
            matches = Engine.matches_prefs(cfg, job)
            reasons = ["未满足或未证明工作地点、职位关键词偏好"]
        rating.update(context=context, pref_match=matches)
        if not matches:
            rating["eligible"] = False
            rating["score"] = min(35, rating["score"])
            rating["gate_reason"] = "；".join(reasons)

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
            with self.store.db() as db:
                self._refresh_rankings(db, self.store.settings(), self.store.profile(full=True))

    def set_filter_assistant(self, assistant):
        """Commit the active conditions and all local rescoring together."""
        with self.config_lock:
            if self.lock.locked():
                raise ValueError("巡查正在运行，请在结束后应用筛选条件")
            cfg = {**self.store.settings(), "filter_assistant": assistant}
            profile = self.store.profile(full=True)
            with self.store.db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO kv VALUES('filter_assistant', ?)",
                    (json.dumps(assistant, ensure_ascii=False),),
                )
                self._refresh_rankings(db, cfg, profile)

    def _refresh_rankings(self, db, cfg, profile):
        if not profile:
            return
        context = rating_context(cfg, profile["id"])
        signals = models.resume_signals(profile["text"], models.role_preferences(cfg))
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
            self.apply_preferences(rating, cfg, job, context)
            db.execute(
                "UPDATE jobs SET rating=?,fingerprint=? WHERE id=?",
                (json.dumps(rating, ensure_ascii=False), fingerprint, job["id"]),
            )

    def import_cards(self, text):
        if not isinstance(text, str) or len(text) > 100_000 or not 1 <= len(text.strip().splitlines()) <= 100:
            raise ValueError("每次可导入 1 至 100 行岗位名称和链接")
        jobs = {}
        for line_no, line in enumerate(text.strip().splitlines(), 1):
            if not line.strip():
                continue
            fields = line.strip().rsplit(None, 1)
            identity = platforms.listing_identity(fields[-1]) if len(fields) == 2 else None
            if not identity or len(fields[0]) > 250:
                raise ValueError(f"第 {line_no} 行需要岗位名称、空格和受支持平台的 HTTPS 职位详情链接")
            key, path = identity
            url = fields[-1].split("?", 1)[0].split("#", 1)[0]
            job = sources.item(key, path, "雇主待核实", fields[0], url, "", fields[0])
            jobs[identity] = {**job, "partial": True}
        with self.config_lock:
            if self.lock.locked():
                raise ValueError("请在本轮巡查结束后导入")
            cfg = self.store.settings()
            if "domestic" not in cfg["source_regions"]:
                raise ValueError("请先在来源与账号中启用国内平台")
            profile = self.store.profile(full=True)
            if not profile:
                raise ValueError("请先上传简历")
            results = self.evaluate({**cfg, "outreach": False}, profile, jobs.values(), None, manual_import=True)
            return {"added": len(results["added"]), "existing": len(jobs) - len(results["added"])}

    def evaluate(self, cfg, profile, jobs, report, *, manual_import=False):
        # Rank locally first to bound model cost. Subsequent patrols advance past cached evaluations.
        records = []
        signals = models.resume_signals(profile["text"], models.role_preferences(cfg))
        context = rating_context(cfg, profile["id"])
        for job in jobs:
            records.append((models.local_rating(profile["text"], job, signals), job))
        records.sort(key=lambda r: r[0]["score"], reverse=True)
        budget = 8
        evaluated = 0
        qualified, index_leads, cards, full = set(), set(), set(), set()
        added, updated, observed = set(), set(), set()
        for local, job in records:
            if not manual_import and self.stopping():
                break
            jid = job_id(job)
            observed.add(jid)
            if job.get("indexed"):
                index_leads.add(jid)
            elif job.get("partial"):
                cards.add(jid)
            else:
                full.add(jid)
            rows = self.store.rows(
                "SELECT company,title,url,location,description,rating,fingerprint,active FROM jobs WHERE id=?",
                (jid,),
            )
            previous = rows[0] if rows else None
            old_rating = json.loads(previous["rating"]) if previous else None
            mode = old_rating.get("mode") if old_rating else None
            if previous and job.get("partial"):
                if manual_import or mode not in ("index", "listing") or (mode == "listing" and job.get("indexed")):
                    continue
            fp = digest(context, job)
            pref_match = self.matches_prefs(cfg, job)
            saved = old_rating if previous and previous["fingerprint"] == fp else None
            if saved and saved.get("mode") != "preliminary" and "pref_match" in saved:
                rating = saved
            else:
                rating = self.baseline(cfg, local, job)
                if rating["mode"] == "preliminary" and pref_match and not local["hard_block"] and budget > 0:
                    budget -= 1
                    self.current = "模型评估 · " + job["title"]
                    try:
                        assessed = models.rate(cfg, profile["text"], job)
                        assessed["materials"] = rating["materials"] + "\n模型建议：" + assessed["materials"]
                        rating = assessed
                    except Exception as exc:
                        report(job["company"] + " · " + job["title"], 0, message(exc))
            self.apply_preferences(rating, cfg, job, context)
            self.store.upsert(job, rating, fp)
            if previous is None:
                added.add(jid)
            elif jid not in added and (
                not previous["active"]
                or any(
                    previous[k] != job.get(k, "")
                    for k in ("company", "title", "url", "location", "description")
                )
                or (mode in ("index", "listing") and mode != rating.get("mode"))
            ):
                updated.add(jid)
            evaluated += int(not job.get("partial"))
            if job.get("partial") or not pref_match or not rating["eligible"] or rating["score"] < cfg["threshold"]:
                continue
            qualified.add(jid)
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
            if (not cfg["outreach"] or job.get("indexed") or job.get("partial")
                    or job["source"] not in cfg["platforms"]
                    or job["source"] in self.store.get("verification_pending", {})):
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
        return dict(
            evaluated=evaluated,
            qualified=len(qualified), index_leads=len(index_leads), cards=len(cards), full=len(full),
            added=sorted(added),
            updated=sorted(updated),
            unchanged=len(observed - added - updated),
        )
