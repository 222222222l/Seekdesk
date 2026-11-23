import base64
import io
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from desk import backends, models, notifications, platforms, searxng, sources
from desk.engine import Engine, email_notice
from desk.storage import Store
from desk.web import DeskHandler, model_draft, validate_settings


PROFILE = "三年数据分析工作经验。使用 Python、SQL、Excel 和 Docker 搭建订单分析报表。"
JD = "数据分析师。需要 Python、SQL、Excel 和 Docker。请提交分析作品集。"
JOB = sources.item(
    "boss",
    "/job_detail/abc.html",
    "测试企业",
    "数据分析师",
    "https://www.zhipin.com/job_detail/abc.html",
    "上海",
    JD,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.rid = self.store.save_resume("求职简历.txt", PROFILE.encode(), PROFILE)
        self.cfg = {
            **self.store.settings(),
            "boards": "",
            "platforms": ["boss"],
            "outreach": True,
            "resume_auto": True,
            "threshold": 75,
            "smtp_host": "smtp.example.org",
            "mail_mode": "custom",
            "smtp_user": "sender@example.org",
            "email": "candidate@example.org",
        }
        self.store.put("settings", self.cfg)
        self.engine = Engine(self.store)

    def test_discover_greet_invite_original_file_and_notify_once(self):
        with patch.object(platforms, "discover", return_value=[JOB]), patch.object(
            platforms, "greet", return_value="hr-1"
        ) as greet, patch.object(
            platforms, "incoming", return_value=[("e1", "resume_request"), ("e2", "contact_request")]
        ), patch.object(
            platforms, "send_resume", return_value="hr-1"
        ) as send, patch(
            "desk.engine.email_notice", return_value="SMTP 已接收"
        ) as mail:
            self.engine.run("手动")
            self.assertEqual(send.call_count, 0)
            self.store.save_resume("新简历.txt", (PROFILE + "新版本").encode(), PROFILE + "新版本")
            self.engine.run("定时")
            self.engine.run("定时")
            self.assertEqual((1, 1, 1), (greet.call_count, send.call_count, mail.call_count))
            self.assertEqual(PROFILE.encode(), send.call_args.args[3]["body"])
            self.assertEqual("hr-1", send.call_args.args[2])
            self.assertIn("交换联系方式", mail.call_args.args[2])
            self.assertIn("Python", greet.call_args.args[2])
            self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)
            self.assertEqual(len(self.store.rows("SELECT * FROM actions WHERE state='已完成'")), 4)

    def test_platform_preview_and_partial_record_upgrades(self):
        self.store.put("settings", {**self.cfg, "location": "中国", "threshold": 0})
        self.store.put(searxng.KEY, {"enabled": True, "endpoint": "http://localhost:1234"})
        index = {**JOB, "location": "", "company": "雇主待核实", "partial": True, "indexed": True}
        with patch.object(
            platforms, "discover", side_effect=platforms.VerificationRequired()
        ) as direct, patch.object(platforms, "indexed_jobs", return_value=[index]) as indexed:
            self.engine.run("只读复查", "boss")
            self.engine = Engine(self.store)
            self.engine.run("只读复查", "boss")
            self.assertEqual(direct.call_count, 1)
            self.assertEqual(indexed.call_count, 2)
            self.assertIn("boss", self.store.get("verification_pending"))
        with patch.object(
            platforms, "discover", side_effect=platforms.NeedsSetup("搜索窗口已关闭")
        ), patch.object(platforms, "indexed_jobs", return_value=[]):
            self.engine.run("只读复查", "boss", True)
            self.assertIn("boss", self.store.get("verification_pending"))

        def rating():
            return json.loads(self.store.rows("SELECT rating FROM jobs")[0]["rating"])

        self.assertEqual(rating()["mode"], "index")
        self.assertFalse(rating()["eligible"])
        with patch.object(platforms, "discover", return_value=[JOB]) as collect:
            self.engine.run("只读复查", "boss", True)
            self.assertTrue(collect.call_args.kwargs["interactive"])
        self.assertNotIn("boss", self.store.get("verification_pending"))
        self.assertEqual(rating()["mode"], "local")
        self.assertTrue(rating()["eligible"])
        self.engine.evaluate(self.cfg, self.store.profile(full=True), [index], self.fail)
        self.assertEqual(rating()["mode"], "local")
        self.assertFalse(self.store.rows("SELECT id FROM actions"))
        self.assertFalse(self.store.get("due"))

    def test_all_discoveries_rank_across_preferences_budget_and_resume_changes(self):
        cfg = {**self.cfg, "provider": "api", "location": "上海"}
        self.store.put("settings", cfg)
        jobs = [{**JOB, "external_id": str(i)} for i in range(10)]
        jobs.extend(
            [
                {**JOB, "external_id": "outside", "location": "Toronto"},
                {**JOB, "external_id": "lead", "partial": True, "indexed": True},
                {**JOB, "external_id": "low", "description": "护理", "title": "护士", "location": "杭州"},
            ]
        )
        assessed = {**models.local_rating(PROFILE, JOB), "mode": "api", "score": 98}
        with patch.object(models, "search_query", return_value="数据分析"), patch.object(
            platforms, "discover", return_value=jobs
        ), patch.object(
            models, "rate", side_effect=[ValueError("模型超时")] + [dict(assessed) for _ in range(10)]
        ) as rate, patch.object(
            platforms, "greet"
        ) as greet:
            self.engine.run("只读复查", "boss")
            self.assertEqual(rate.call_count, 8)
            ratings = {
                r["external_id"]: json.loads(r["rating"])
                for r in self.store.rows("SELECT external_id,rating FROM jobs")
            }
            self.assertEqual(len(ratings), len(jobs))
            self.assertEqual(ratings["0"]["mode"], "preliminary")
            self.assertFalse(ratings["outside"]["pref_match"])
            self.assertGreater(ratings["lead"]["score"], 0)
            self.assertFalse(ratings["lead"]["eligible"])
            self.assertEqual(ratings["low"]["score"], 0)
            self.engine.run("只读复查", "boss")
            self.assertEqual(rate.call_count, 11)
            greet.assert_not_called()
        self.store.execute("UPDATE jobs SET active=0 WHERE external_id='0'")
        handler = DeskHandler.__new__(DeskHandler)
        handler.server = SimpleNamespace(store=self.store, engine=self.engine)
        rows = handler.state()["jobs"]
        self.assertEqual([r["score"] for r in rows], sorted((r["score"] for r in rows), reverse=True))
        self.store.save_resume("other.txt", b"other", "曾从事护理工作。")
        self.engine.refresh_rankings()
        self.assertFalse(self.store.rows("SELECT id FROM actions"))
        self.assertEqual(self.store.rows("SELECT active FROM jobs WHERE external_id='0'")[0]["active"], 0)
        rescored = handler.state()["jobs"]
        self.assertEqual(rescored[0]["title"], "护士")
        self.assertGreater(rescored[0]["score"], 0)
        self.assertTrue(all(r["current"] for r in rescored))
        self.assertTrue(all(r["score"] == 0 for r in rescored[1:]))

    def test_backend_profiles_credentials_and_directory_protocols(self):
        self.store.put(
            "settings",
            {
                **self.cfg,
                "provider": "deepseek",
                "api_url": "https://api.deepseek.com",
                "model": "saved-model",
                "api_key": "private",
            },
        )
        self.assertEqual(model_draft(self.store, {"provider": "deepseek"})["api_key"], "private")
        self.assertEqual(
            model_draft(self.store, {"provider": "deepseek", "api_url": "https://other.example"})["api_key"],
            "",
        )
        self.assertEqual(model_draft(self.store, {"provider": "openai"})["api_key"], "")
        for key, directory, response, expected, route in [
            (
                "openai",
                {"data": [{"id": "chat"}]},
                {"choices": [{"message": {"content": "ok"}}]},
                "chat",
                "/chat/completions",
            ),
            (
                "anthropic",
                {"data": [{"id": "sonnet"}]},
                {"content": [{"type": "text", "text": "ok"}]},
                "sonnet",
                "/messages",
            ),
            ("ollama", {"models": [{"name": "local"}]}, {"message": {"content": "ok"}}, "local", "/api/chat"),
            (
                "gemini",
                {
                    "models": [
                        {"name": "models/flash", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
                    ]
                },
                {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
                "flash",
                "/models/flash:generateContent",
            ),
        ]:
            cfg = {"provider": key, "api_url": backends.APIS[key][1], "model": expected}
            with self.subTest(key=key), patch.object(
                backends, "request", side_effect=[directory, response]
            ) as request:
                self.assertEqual(backends.api_models(cfg), [expected])
                self.assertEqual(backends.complete(cfg, "test"), "ok")
                self.assertEqual(request.call_args.args[1], route)

    def test_local_search_plugin_install_disable_and_official_index(self):
        url = "https://www.zhaopin.com/jobdetail/CC123456.htm"

        class Search(BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(200)
                handler.end_headers()
                data = {
                    "results": [
                        {"url": url, "title": "数据分析师", "content": "SQL"},
                        {"url": url, "title": "重复"},
                        {"url": "https://example.org/jobdetail/CC123.htm", "title": "外部结果"},
                    ]
                }
                handler.wfile.write(json.dumps(data).encode())

            def log_message(handler, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Search) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                address = f"http://127.0.0.1:{server.server_port}"
                cfg = searxng.operate(self.store, "install", {"mode": "url", "endpoint": address})
                self.assertTrue(cfg["enabled"])
                jobs = platforms.indexed_jobs("zhilian", "数据分析", address)
                self.assertEqual([j["url"] for j in jobs], [url])
                self.assertTrue(jobs[0]["indexed"])
                self.assertFalse(searxng.operate(self.store, "disable", {})["enabled"])
                self.assertEqual(searxng.operate(self.store, "remove", {})["mode"], "")
                with self.assertRaises(ValueError):
                    searxng.endpoint("http://example.org")
            finally:
                server.shutdown()

    def test_uncertain_receipt_does_not_repeat_on_restart(self):
        with patch.object(platforms, "discover", return_value=[JOB]), patch.object(
            platforms, "greet", side_effect=TimeoutError()
        ) as greet:
            self.engine.run("手动")
            Engine(Store(self.temp.name)).run("定时")
        self.assertEqual(greet.call_count, 1)
        self.assertEqual(self.store.rows("SELECT state FROM actions")[0]["state"], "待核对")

    def test_board_withdrawal_failure_and_notifications(self):
        self.store.put("settings", {**self.cfg, "boards": "greenhouse:example", "platforms": []})
        job = {**JOB, "source": "greenhouse:example", "external_id": "123"}
        with patch.object(
            sources, "board", side_effect=[[job], ValueError("来源暂不可用"), [], [job]]
        ), patch("desk.engine.email_notice", return_value="SMTP 已接收") as mail:
            self.engine.run("手动")
            self.engine.run("定时")
            self.assertEqual(1, self.store.rows("SELECT active FROM jobs")[0]["active"])
            self.engine.run("定时")
            self.assertEqual(0, self.store.rows("SELECT active FROM jobs")[0]["active"])
            self.engine.run("定时")
            self.assertEqual(1, mail.call_count)
            self.assertIn("作品集", mail.call_args.args[2])
            self.assertIn("https://", mail.call_args.args[2])

    def test_retained_invitation_after_enabling_send(self):
        self.store.put("settings", {**self.cfg, "resume_auto": False})
        with patch.object(platforms, "discover", return_value=[JOB]), patch.object(
            platforms, "greet", return_value="hr2"
        ), patch.object(platforms, "incoming", side_effect=[[("a", "resume_request")], []]), patch.object(
            platforms, "send_resume", return_value="hr2"
        ) as send:
            self.engine.run("手动")
            self.engine.run("定时")
            self.assertFalse(send.called)
            self.store.put("settings", self.cfg)
            self.engine.run("定时")
            self.assertTrue(send.called)

    def test_structured_search_and_resume_formats(self):
        doc = {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "JobPosting",
                    "title": "分析师",
                    "description": JD,
                    "hiringOrganization": {"name": "企业"},
                }
            ],
        }
        html = ('<script type="application/ld+json">' + json.dumps(doc) + "</script>").encode()
        with patch.object(sources, "fetch", return_value=(html, "https://example.org/careers/12")):
            self.assertEqual(sources.page_jobs("https://example.org", "搜索发现")[0]["description"], JD)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as docx:
            docx.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:t>'
                + PROFILE
                + "</w:t></w:p></w:document>",
            )
        self.assertEqual(models.resume_text("cv.docx", buf.getvalue()), PROFILE)
        self.assertFalse(
            models.local_rating("不会 Python；无 SQL 经验", {**JOB, "description": "需要 Python 和 SQL"})[
                "eligible"
            ]
        )

    def test_two_search_engines_one_position_and_material_notice(self):
        self.store.put(
            "settings", {**self.cfg, "platforms": [], "brave_key": "brave", "tavily_key": "tavily"}
        )
        role = {
            "@type": "JobPosting",
            "title": "数据分析师",
            "description": JD,
            "hiringOrganization": {"name": "测试企业"},
        }
        posting = "https://example.org/jobs/analyst"

        def fetch(url, **kw):
            if "api.search.brave.com" in url:
                self.assertEqual(kw["headers"]["X-Subscription-Token"], "brave")
                return json.dumps({"web": {"results": [{"url": posting}]}}).encode(), url
            if "api.tavily.com" in url:
                self.assertIn("query", kw["payload"])
                return json.dumps({"results": [{"url": posting}]}).encode(), url
            self.assertEqual(posting, url)
            return ('<script type="application/ld+json">' + json.dumps(role) + "</script>").encode(), url

        with patch.object(sources, "fetch", side_effect=fetch), patch(
            "desk.engine.email_notice", return_value="SMTP 已接收"
        ) as mail:
            self.engine.run("手动")
            self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)
            self.assertEqual(mail.call_count, 1)
            self.assertIn("请提交分析作品集", mail.call_args.args[2])

    def test_persisted_scheduler_due_time_and_single_flight(self):
        self.store.put("settings", {**self.cfg, "scheduled": True, "interval": 5})
        started, release = threading.Event(), threading.Event()

        def collect(*args):
            started.set()
            release.wait(5)
            return []

        with patch.object(platforms, "discover", side_effect=collect):
            scheduler = threading.Thread(target=self.engine.scheduler, daemon=True)
            scheduler.start()
            try:
                self.assertTrue(started.wait(4))
                with self.assertRaisesRegex(ValueError, "正在运行"):
                    self.engine.start()
                release.set()
                self.assertTrue(self.engine.lock.acquire(timeout=4))
                self.engine.lock.release()
                self.assertGreaterEqual(self.store.get("due"), int(time.time()) + 295)
                self.assertEqual(self.store.rows("SELECT reason FROM runs")[0]["reason"], "定时")
            finally:
                release.set()
                self.engine.stop.set()
                scheduler.join(3)

    def test_private_settings_and_local_api_match(self):
        self.store.put("settings", {**self.cfg, "api_key": "synthetic-secret"})
        self.assertEqual(self.store.settings()["api_key"], "synthetic-secret")
        if os.name == "nt":
            self.assertNotIn("synthetic-secret", self.store.get("settings")["api_key"])
        evidence = "使用 Python、SQL、Excel 和 Docker 搭建订单分析报表"
        result = dict(
            score=92,
            eligible=True,
            evidence=evidence,
            requirement="需要 Python、SQL、Excel 和 Docker",
            reason="匹配",
            materials="准备作品集",
        )

        class Model(BaseHTTPRequestHandler):
            def do_POST(handler):
                self.assertEqual(handler.path, "/v1/chat/completions")
                req = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                self.assertIn(JD, req["messages"][0]["content"])
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(
                    json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode()
                )

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Model) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cfg = {
                    **self.cfg,
                    "provider": "api",
                    "api_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "model": "local",
                }
                self.assertTrue(models.rate(cfg, PROFILE, JOB)["eligible"])
                result["evidence"] = "简历中不存在的成果"
                self.assertFalse(models.rate(cfg, PROFILE, JOB)["eligible"])
            finally:
                server.shutdown()

    def test_smtp_notice_uses_only_candidate_recipient(self):
        cfg = {
            **self.cfg,
            "smtp_port": 587,
            "smtp_user": "sender@example.org",
            "smtp_password": "example-only",
        }
        with patch("desk.engine.smtplib.SMTP") as transport:
            email_notice(cfg, "联系方式请求", "请到原平台确认联系方式请求。")
            client = transport.return_value
            client.starttls.assert_called_once()
            client.login.assert_called_once_with("sender@example.org", "example-only")
            mail = client.send_message.call_args.args[0]
            self.assertEqual(mail["To"], "candidate@example.org")
            self.assertEqual(mail["From"], "sender@example.org")
            self.assertFalse(list(mail.iter_attachments()))

    def test_quick_mail_and_legacy_configuration(self):
        cfg = {**self.cfg, "mail_mode": "auto", "email": "candidate@qq.com", "smtp_password": "test-code"}
        with patch("desk.engine.smtplib.SMTP_SSL") as transport:
            email_notice(cfg, "test", "test")
            self.assertEqual(transport.call_args.args[:2], ("smtp.qq.com", 465))
            transport.return_value.login.assert_called_once_with("candidate@qq.com", "test-code")
        self.assertEqual(validate_settings({"email": "other@163.com"}, cfg)["smtp_password"], "")
        previous = dict(self.cfg)
        del previous["mail_mode"]
        self.store.put("settings", previous)
        self.assertEqual(self.store.settings()["mail_mode"], "custom")

    def test_dual_channel_receipts_survive_retry_and_restart(self):
        cfg = {**self.cfg, "boards": "greenhouse:example", "platforms": [], "windows_notifications": True}
        self.store.put("settings", cfg)
        job = {**JOB, "source": "greenhouse:example"}
        with patch.object(sources, "board", return_value=[job]), patch.object(
            notifications, "desktop_notice", return_value="已提交到 Windows 通知中心"
        ) as desktop, patch("desk.engine.email_notice", side_effect=platforms.NeedsSetup("SMTP 登录失败")):
            self.engine.run("手动")
            self.assertEqual(self.store.rows("SELECT state FROM actions")[0]["state"], "待处理")
            with patch("desk.engine.email_notice", return_value="SMTP 已接收") as mail:
                Engine(self.store).run("手动")
                self.assertEqual(mail.call_count, 1)
            self.assertEqual(desktop.call_count, 1)
        receipt = self.store.rows("SELECT state,detail FROM actions")[0]
        self.assertEqual(receipt["state"], "已完成")
        self.assertIn("Windows", receipt["detail"])
        self.assertIn("SMTP", receipt["detail"])

    def test_desktop_only_receives_new_events(self):
        cfg = {**self.cfg, "mail_mode": "auto", "boards": "greenhouse:example", "platforms": []}
        self.store.put("settings", cfg)
        job = {**JOB, "source": "greenhouse:example"}
        new = {**job, "external_id": "new", "url": job["url"] + "?new"}
        with patch.object(sources, "board", side_effect=[[job], [job, new]]), patch.object(
            notifications, "desktop_notice", return_value="Windows 已接收"
        ) as desktop:
            self.engine.run("手动")
            self.store.put("settings", {**cfg, "windows_notifications": True, "email_enabled": False})
            with patch("desk.engine.email_notice") as mail:
                self.engine.run("手动")
                mail.assert_not_called()
            self.assertEqual(desktop.call_count, 1)


class LoginRoutingTests(unittest.TestCase):
    def test_manual_login_starts_at_official_url_and_keeps_the_patrol_profile(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            platforms, "sync_playwright"
        ) as driver, patch.object(platforms, "browser_route", return_value=[]), patch.object(
            platforms.subprocess, "Popen"
        ) as launch:
            driver.return_value.__enter__.return_value.chromium.executable_path = "chromium"
            root = Path(tmp)
            for key, site in platforms.PLATFORMS.items():

                def wait():
                    self.assertTrue(platforms.LOCKS[key].locked())
                    return 0

                launch.return_value.wait.side_effect = wait
                platforms.login_window(root, key, lambda _: None)
                command = launch.call_args.args[0]
                self.assertEqual(command[-1], site.get("login", site["home"]))
                self.assertIn(f"--user-data-dir={(root / 'profiles' / key).resolve()}", command)
                self.assertNotIn("about:blank", command)
                self.assertFalse(any("remote-debugging" in arg for arg in command))
                self.assertFalse(platforms.LOCKS[key].locked())

    def test_local_proxy_transport_failure_and_direct_session_reuse(self):
        with patch.dict(platforms._ROUTES, {}, clear=True), patch.object(
            platforms, "proxy_bypass", return_value=False
        ), patch.object(
            platforms, "getproxies", return_value={"https": "http://127.0.0.1:7897"}
        ), patch.object(
            platforms.requests, "Session"
        ) as factory:
            probe = factory.return_value.__enter__.return_value
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.status_code = 200
            probe.get.side_effect = [platforms.requests.ConnectionError("connection closed"), response]
            self.assertEqual(platforms.browser_route("boss"), ["--no-proxy-server"])
            self.assertEqual(platforms.browser_route("boss"), ["--no-proxy-server"])
            self.assertEqual(probe.get.call_count, 2)
            self.assertNotIn("proxies", probe.get.call_args.kwargs)

    def test_responding_proxy_is_preserved(self):
        with patch.dict(platforms._ROUTES, {}, clear=True), patch.object(
            platforms, "proxy_bypass", return_value=False
        ), patch.object(
            platforms, "getproxies", return_value={"https": "http://localhost:7897"}
        ), patch.object(
            platforms.requests, "Session"
        ) as factory:
            probe = factory.return_value.__enter__.return_value
            probe.get.return_value.__enter__.return_value.status_code = 403
            self.assertEqual(platforms.browser_route("liepin"), [])
            self.assertEqual(probe.get.call_count, 1)


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_text_pdf_upload(self):
        page = self.browser.new_page()
        try:
            page.set_content("<main>Candidate resume. Python SQL analytics.</main>")
            self.assertIn("Python SQL analytics", models.resume_text("candidate.pdf", page.pdf()))
        finally:
            page.close()

    def test_liepin_headhunter_and_zhilian_verified_search(self):
        cases = [
            (
                "liepin",
                "/a/123456.shtml",
                '<div class="job-apply-container"><span class="job-title">数据分析师</span><div class="job-properties"><span>苏州-太仓</span></div></div>'
                '<div class="job-intro-container"><p class="paragraph">需要 SQL</p></div>'
                '<div class="company-info-container"><div class="company-card"><span class="content">示例公司</span></div></div>',
            ),
            (
                "zhilian",
                "/jobdetail/CCL123J001.htm",
                '<h1>数据分析师</h1><div class="describestip__content">需要 SQL</div><div class="company__title">示例公司</div>',
            ),
        ]
        for key, path, details in cases:

            @contextmanager
            def fake_session(*args, **kwargs):
                ctx = self.browser.new_context()

                def response(route):
                    html = (
                        details
                        if route.request.url.endswith(path)
                        else (
                            '<title>Security Verification</title><div class="job-list-box"></div>'
                            '<script>setTimeout(()=>{document.title="搜索";let a=document.createElement("a");'
                            f'a.href="{path}";a.textContent="数据分析师";'
                            'document.querySelector(".job-list-box").append(a)},100)</script>'
                        )
                    )
                    route.fulfill(content_type="text/html; charset=utf-8", body=html)

                ctx.route("**/*", response)
                try:
                    yield ctx
                finally:
                    ctx.close()

            with self.subTest(key=key), patch.object(platforms, "session", fake_session):
                job = platforms.discover(None, key, "数据分析师", interactive=True)[0]
                self.assertEqual(
                    (job["external_id"], job["description"], job["company"]), (path, "需要 SQL", "示例公司")
                )
                self.assertFalse(job.get("partial"))
        self.assertIsNone(re.fullmatch(platforms.ROLE_PATHS["boss"], "/job_detail/"))

    def test_detail_verification_preserves_cards_and_stops_navigation(self):
        ctx = self.browser.new_context()
        self.addCleanup(ctx.close)
        visits = []

        def gate(route):
            visits.append(route.request.url)
            route.fulfill(content_type="text/html", body="<title>Security Verification</title>")

        ctx.route("**/*", gate)
        cards = []
        for suffix in ("first", "second"):
            path = "/job_detail/" + suffix + ".html"
            cards.append(
                (
                    path,
                    dict(
                        url="https://www.zhipin.com" + path,
                        title="数据分析",
                        company="测试企业",
                        location="上海",
                        summary="岗位卡片",
                    ),
                )
            )
        with self.assertRaises(platforms.VerificationRequired) as captured:
            platforms.collect_details(ctx, "boss", cards)
        self.assertEqual(visits, [cards[0][1]["url"]])
        self.assertEqual(len(captured.exception.jobs), 2)
        self.assertTrue(all(job.get("partial") for job in captured.exception.jobs))

    def test_platform_controls_and_conversation_binding(self):
        details = (
            '<h1>数据分析师</h1><div class="job-sec-text">'
            + JD
            + '</div><div class="company-info"><b class="name">测试企业</b></div><a href="/chat?chatId=hr-1">继续沟通</a>'
        )
        chat = """<div id="chat-input" contenteditable="true"></div><button>发送</button>
                <div data-direction="incoming" data-mid="e1"><div class="message-card" data-request-type="resume_request">希望收到您的附件简历</div></div>
                <div data-direction="outgoing"><div class="message-card" data-request-type="contact_request">交换微信</div></div>
                <input type="file" accept=".pdf,.docx,.txt"><script>
                const output=t=>{let el=document.createElement('p');el.dataset.direction='outgoing';el.dataset.status='sent';el.textContent=t;document.body.append(el)};
                document.querySelector('button').onclick=()=>output(document.querySelector('#chat-input').innerText);
                document.querySelector('input').onchange=e=>output(e.target.files[0].name);
                </script>"""

        @contextmanager
        def local_session(*args, **kwargs):
            ctx = self.browser.new_context()

            def route(req):
                url = req.request.url
                content = (
                    chat
                    if "/chat?" in url
                    else details if "/job_detail/" in url else '<a href="' + JOB["url"] + '">数据分析师</a>'
                )
                req.fulfill(content_type="text/html; charset=utf-8", body=content)

            ctx.route("**/*", route)
            try:
                yield ctx
            finally:
                ctx.close()

        with patch.object(platforms, "session", local_session):
            job = platforms.discover(None, "boss", "数据分析师")[0]
            ident = platforms.greet(None, job, "您好，我使用 SQL 分析订单。")
            self.assertEqual("chatId:hr-1", ident)
            self.assertEqual([("e1", "resume_request")], platforms.incoming(None, job, ident))
            self.assertEqual(
                ident,
                platforms.send_resume(None, job, ident, {"name": "cv.txt", "body": b"synthetic resume"}),
            )
            with self.assertRaisesRegex(ValueError, "会话标识"):
                platforms.send_resume(None, job, "wrong", {"name": "cv.txt", "body": b"synthetic"})

    def test_desktop_mobile_workbench_and_legacy(self):
        with tempfile.TemporaryDirectory() as d, ThreadingHTTPServer(("127.0.0.1", 0), DeskHandler) as server:
            server.store = Store(d)
            server.config_lock = threading.RLock()
            server.engine = Engine(server.store, server.config_lock)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = f"http://127.0.0.1:{server.server_port}"
            page = self.browser.new_page(viewport={"width": 1360, "height": 920})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                page.goto(origin)
                page.get_by_text("岗位巡查雷达", exact=True).wait_for()
                page.get_by_role("button", name="上传我的简历").click()
                page.locator("#upload").set_input_files(
                    {"name": "cv.txt", "mimeType": "text/plain", "buffer": PROFILE.encode()}
                )
                page.get_by_text("已解析", exact=True).wait_for()
                page.locator('a[href="#sources"]').first.click()
                page.locator("[name=boards]").fill("greenhouse:example")
                page.get_by_role("button", name="保存岗位来源").click()
                page.get_by_text("设置已保存", exact=True).wait_for()
                page.locator('nav a[href="#overview"]').click()
                with patch.object(sources, "board", return_value=[{**JOB, "source": "greenhouse:example"}]):
                    page.get_by_role("button", name="立即巡查").first.click()
                    page.locator("button.job").first.wait_for(timeout=15000)
                page.locator("button.job").first.click()
                page.get_by_role("link", name="打开原始岗位").wait_for()
                self.assertIn("作品集", page.locator("#detail-body").inner_text())
                page.get_by_role("button", name="关闭", exact=True).click()
                page.goto(origin + "/#jobs")
                page.locator("#job-filter").select_option("high")
                self.assertIn("高匹配", page.locator("button.job").inner_text())
                page.locator("#job-filter").select_option("low")
                self.assertEqual(page.locator("button.job").count(), 0)
                page.goto(origin + "/#settings")
                if os.name == "nt":
                    page.get_by_label("邮件提醒", exact=True).uncheck()
                    page.get_by_label("Windows 桌面通知").check()
                    self.assertFalse(page.locator("[name=email]").is_visible())
                    with page.expect_response("**/api/settings") as saved:
                        page.get_by_role("button", name="保存模型与任务").click()
                    self.assertEqual(saved.value.status,200)
                    page.get_by_label("Windows 桌面通知").wait_for()
                    page.reload()
                    page.get_by_label("Windows 桌面通知").wait_for()
                    self.assertTrue(page.get_by_label("Windows 桌面通知").is_checked())
                    self.assertFalse(page.get_by_label("邮件提醒", exact=True).is_checked())
                page.locator("#backend-provider").select_option("ollama")
                page.route(
                    "**/api/models/scan",
                    lambda route: route.fulfill(json={"models": ["fixture-chat"], "note": "测试目录"}),
                )
                page.get_by_role("button", name="扫描模型").click()
                page.locator("#backend-list").select_option("fixture-chat")
                page.get_by_role("button", name="使用此后端和模型").click()
                page.get_by_text("当前 · 本地 Ollama / fixture-chat", exact=True).wait_for()
                self.assertEqual(server.store.settings()["model"], "fixture-chat")
                page.locator("#backend-provider").select_option("local")
                page.get_by_role("button", name="使用此后端和模型").click()
                page.get_by_text("当前 · 本地关键词匹配", exact=True).wait_for()
                page.set_viewport_size({"width": 390, "height": 844})
                for key in ("overview", "jobs", "resume", "sources", "settings", "activity"):
                    page.goto(origin + "/#" + key)
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth<=innerWidth"), key)
                page.goto(origin + "/review.html")
                page.get_by_role("button", name="载入公开样本与虚构履历").click()
                self.assertEqual(page.locator("article").count(), 5)
                self.assertEqual([], errors)
                self.assertEqual(page.request.post(origin + "/api/run", data={}).status, 403)
                self.assertEqual(page.request.get(origin + "/.workbench/desk.sqlite3").status, 404)
            finally:
                page.close()
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
