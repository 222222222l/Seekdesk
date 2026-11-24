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
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from desk import backends, filter_assistant, models, notifications, platforms, searxng, sources
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

    def test_index_only_patrol_and_manual_import_never_open_platform(self):
        cfg = {**self.cfg, "platforms": [], "platform_indexes": ["51job"], "employer_indexes": [], "source_regions": ["domestic"]}
        self.store.put("settings", cfg)
        self.store.put(searxng.KEY, {"enabled": True, "endpoint": "http://localhost:1234"})
        self.store.put("verification_pending", {"boss": 123})
        rows = [{"url": f"https://jobs.51job.com/shenzhen/{n}.html?s=1", "title": "SQL 分析师", "content": "SQL"}
                for n in range(12)]
        rows.extend([{"url": "https:///go/v1/example", "title": "SQL 岗位", "content": "SQL"},
                     {"url": "https://example.org/", "title": "SQL 岗位", "content": "SQL"}])
        with patch.object(platforms, "discover", side_effect=AssertionError("unexpected direct patrol")), patch.object(
            sources, "fetch", side_effect=AssertionError("unexpected target visit")
        ), patch.object(searxng, "results", return_value=rows), patch.object(
            models, "search_query", return_value="SQL"
        ), patch.object(sources, "search_links", return_value=[rows[0]["url"]]):
            self.engine.run("定时")
            self.engine.run("定时")
            reports = [json.loads(r["reports"])[0] for r in self.store.rows("SELECT reports FROM runs ORDER BY id")]
            self.assertEqual((reports[0]["index_leads"], reports[0]["full"], reports[0]["qualified"]), (12, 0, 0))
            self.assertIn("14 条候选，其中官方职位详情链接 12 条", reports[0]["note"])
            self.assertEqual((len(reports[0]["added"]), reports[1]["unchanged"]), (12, 12))
            self.engine.cancelled.set()
            result = self.engine.import_cards("测试岗位 " + JOB["url"] + "?tracking=123\nSQL 岗位 " + rows[0]["url"])
            self.assertEqual(result, {"added": 1, "existing": 1})
            self.assertEqual(self.store.get("verification_pending"), {"boss": 123})
            self.assertFalse(self.store.rows("SELECT id FROM actions"))
            with self.assertRaisesRegex(ValueError, "第 2 行"):
                self.engine.import_cards("测试岗位 https://www.zhipin.com/job_detail/more.html\n错误 https://example.org/")
            self.assertEqual(len(self.store.rows("SELECT id FROM jobs")), 13)

    def test_held_platform_skip_and_explicit_release(self):
        self.store.put("verification_pending", {"boss": 1, "liepin": 2})
        with patch.object(platforms, "discover") as visit:
            self.engine.run("定时")
            visit.assert_not_called()
            report = json.loads(self.store.rows("SELECT reports FROM runs")[0]["reports"])[0]
            self.assertEqual((report["outcome"], report["phase"], report["error"]), ("skipped", "本地检查", ""))
            handler = SimpleNamespace(path="/api/platform/resume", server=SimpleNamespace(
                config_lock=self.engine.config_lock, store=self.store, engine=self.engine))
            DeskHandler.submit(handler, {"platform": "boss"})
            self.assertEqual(self.store.get("verification_pending"), {"liepin": 2})
            visit.assert_not_called()

    def test_employer_search_requires_consistent_detail_and_keeps_cards_on_failure(self):
        def reply(data):
            return json.dumps({"Code": 200, "Data": data}).encode(), ""
        listing = reply({"Posts": [{"PostId": str(n), "RecruitPostName": "分析师", "LocationName": "深圳",
                                    "Responsibility": "SQL"} for n in (123, 456)]})
        detail = reply({"PostId": "123", "Responsibility": "分析数据", "Requirement": "熟悉 SQL"})
        with patch.object(sources, "fetch", side_effect=[listing, detail, sources.SourceHTTPError(403)]) as fetch:
            with self.assertRaises(sources.EmployerDetailsUnavailable) as failed:
                sources.tencent_jobs("数据分析")
            full, card = failed.exception.jobs
            self.assertFalse(full["partial"])
            self.assertTrue(card["partial"])
            self.assertIn("任职要求", full["description"])
            self.assertEqual(full["url"], "https://careers.tencent.com/jobdesc.html?postId=123")
            self.assertEqual(fetch.call_count, 3)
        with patch.object(sources, "fetch", return_value=reply({"Count": 0})):
            with self.assertRaisesRegex(ValueError, "清单格式"):
                sources.tencent_jobs("数据分析")

    def test_bosch_china_checks_location_and_uses_public_posting_id(self):
        rows = [{"id": "123", "name": "测试工程师", "location": {"country": "cn", "city": "苏州"}},
                {"id": "456", "name": "海外测试岗", "location": {"country": "de", "city": "Berlin"}},
                {"id": "789", "name": "仓库主管", "location": {"country": "cn", "city": "济南"}}]
        detail = {**rows[0], "postingUrl": "https://jobs.smartrecruiters.com/BoschGroup/123-test",
                  "jobAd": {"sections": {"jobDescription": {"text": "<p>设计产品</p>"},
                                         "qualifications": {"text": "掌握 SQL"}}}}
        with patch.object(sources, "fetch", side_effect=[(json.dumps({"content": rows, "totalFound": 120}), ""), (json.dumps(detail), "")]) as fetch:
            stats = {}
            jobs = sources.bosch_china_jobs("测试", 100, stats=stats)
            self.assertEqual(stats, {"scanned": 3, "next": 103})
            self.assertEqual([(j["title"], j["location"], j["partial"]) for j in jobs], [("测试工程师", "苏州", False)])
            self.assertEqual(jobs[0]["url"], detail["postingUrl"])
            self.assertIn("country=cn", fetch.call_args_list[0].args[0])
            self.assertTrue(fetch.call_args_list[1].args[0].endswith("/postings/123"))

    def test_employer_indexes_only_accept_canonical_official_details(self):
        addresses = {
            "腾讯社招": "https://careers.tencent.com/jobdesc.html?postId=123",
            "字节跳动社招": "https://jobs.bytedance.com/experienced/position/123/detail",
            "网易招聘": "https://hr.163.com/job-detail.html?id=123",
        }
        urls = list(addresses.values()) + ["https://hr.163.com/job-detail.html", "https://hr.163.com.evil.example/job-detail.html?id=123"]
        rows = [dict(url=url, title="产品经理", content="负责产品") for url in urls]
        with patch.object(searxng, "results", return_value=rows), patch.object(sources, "fetch") as fetch:
            for name, url in addresses.items():
                jobs = sources.employer_index(name, "产品经理", "http://localhost:8888")
                self.assertEqual([(j["external_id"], j["url"], j["indexed"], j["partial"]) for j in jobs],
                                 [("123", url, True, True)])
            fetch.assert_not_called()

    def test_model_filter_generation_apply_followup_and_clear(self):
        cfg = {**self.cfg, "provider": "pi", "keywords": "护理", "location": "北京"}
        self.store.put("settings", cfg)
        saved = {
            **JOB,
            "title": "智能体产品经理",
            "location": "杭州",
            "description": "负责游戏智能体和 SQL 产品。与工业设计团队合作。月薪：25-35K",
        }
        self.store.upsert(saved, {}, "")
        handler = DeskHandler.__new__(DeskHandler)
        handler.server = SimpleNamespace(
            store=self.store, engine=self.engine, config_lock=self.engine.config_lock
        )
        handler.path = "/api/filters/generate"

        def payload(request):
            return {"request": request, "revision": handler.state()["filter_revision"]}

        rules = {k: [] for k in filter_assistant.FIELDS}
        rules.update(
            titles=["产品经理"],
            any_terms=["智能体", "AIGC"],
            all_terms=["SQL"],
            exclude_titles=["工业设计"],
            cities=["上海", "杭州"],
            prefer=["游戏"],
            queries=["智能体 产品经理", "AIGC 产品经理"],
            min_monthly_cny=20000,
        )
        before = payload("找上海或杭州的智能体产品经理，月薪两万以上")
        with patch.object(models, "model_json", return_value=rules) as generate:
            self.assertTrue(handler.submit(before)["applied"])
            self.assertNotIn(self.cfg["smtp_user"], str(generate.call_args.args[-1]))
        report = json.loads(self.store.rows("SELECT rating FROM jobs")[0]["rating"])
        self.assertTrue(report["pref_match"])
        self.assertEqual(report["preferred"], ["游戏"])
        applied = self.store.settings()
        self.assertEqual(models.search_query(applied, PROFILE, sequence=1), "AIGC 产品经理")
        self.assertFalse(self.engine.matches_prefs(applied, {**saved, "description": "SQL 智能体，薪资面议"}))
        self.assertFalse(self.engine.matches_prefs(applied, {**saved, "title": "工业设计产品经理"}))
        self.assertEqual(self.store.rows("SELECT id FROM actions"), [])
        with self.assertRaisesRegex(ValueError, "已变更"):
            handler.submit(before)
        with patch.object(
            models, "model_json", return_value={**rules, "unresolved": ["地铁通勤时间"]}
        ) as gen:
            self.assertFalse(handler.submit(payload("仅限通勤半小时"))["applied"])
            self.assertEqual(gen.call_args.args[-1]["现行条件"], rules)
        with patch.object(models, "model_json", return_value={**rules, "outreach": True}):
            with self.assertRaisesRegex(ValueError, "未知字段"):
                handler.submit(payload("增加岗位方向"))
        self.assertEqual(self.store.settings()["filter_assistant"], applied["filter_assistant"])
        with patch.object(models, "model_json", return_value={**rules, "cities": ["广州"]}), patch.object(
            self.engine, "_refresh_rankings", side_effect=ValueError("模拟重算失败")
        ), self.assertRaisesRegex(ValueError, "重算失败"):
            handler.submit(payload("地点改为广州"))
        self.assertEqual(self.store.settings()["filter_assistant"], applied["filter_assistant"])
        with patch.object(models, "model_json", return_value={**rules, "cities": ["广州"]}):
            handler.submit(payload("地点改为广州"))
        self.assertFalse(json.loads(self.store.rows("SELECT rating FROM jobs")[0]["rating"])["pref_match"])
        handler.path = "/api/filters/clear"
        handler.submit(payload(""))
        self.assertFalse(self.store.settings()["filter_assistant"].get("rules"))
        self.assertEqual(self.store.settings()["keywords"], "护理")
        self.assertFalse(self.store.rows("SELECT id FROM runs"))

    def test_target_role_skills_location_and_search_gates(self):
        cv = "游戏 AI 产品经理\n负责游戏 AIGC 内容管线，开发基于大语言模型的智能体与 Agent 工作流。"
        cfg = {**self.cfg, "keywords": "AIGC、agent", "location": "上海、杭州", "provider": "pi"}
        positive = {
            **JOB,
            "title": "智能体产品经理",
            "location": "Hangzhou",
            "description": "规划生成式 AI 产品路线图，开发大语言模型智能体与 AIGC 内容工具。",
        }
        negative = {
            **positive,
            "title": "工业产品设计师（手表）",
            "location": "深圳",
            "description": "负责产品设计。上海总部推广人工智能、AIGC 工具。",
        }
        signals = models.resume_signals(cv, cfg["keywords"])
        self.assertEqual(signals[-1], "游戏 AI 产品经理")
        self.assertTrue(self.engine.matches_prefs(cfg, positive))
        self.assertFalse(self.engine.matches_prefs(cfg, negative))
        self.assertFalse(self.engine.matches_prefs(cfg, {
            **positive, "title": "机械产品经理", "description": "岗位职责：规划手表外观。公司介绍：总部设有 AIGC 实验室。"
        }))
        unrelated = models.local_rating(cv, negative, signals)
        self.assertFalse(unrelated["eligible"])
        self.assertLess(unrelated["score"], 50)
        matched = models.local_rating(cv, positive, signals)
        self.assertTrue(matched["eligible"])
        self.assertGreaterEqual(matched["score"], 75)
        partial = models.local_rating(cv, {**positive, "partial": True}, signals)
        self.assertFalse(partial["eligible"])
        self.assertLess(partial["score"], 50)
        cards = {str(i): {"title": "工业设计师", "summary": "负责手表外观设计"} for i in range(10)}
        cards["relevant"] = {"title": positive["title"], "summary": positive["description"]}
        ranked = platforms.review_candidates("AIGC 产品经理", cards)
        self.assertEqual(len(ranked), 8)
        self.assertEqual(ranked[0][0], "relevant")
        self.assertFalse(models.local_rating("求职意向：产品经理", positive)["eligible"])
        self.assertFalse(
            models.local_rating(
                "2024年入职，参与 AIGC 产品设计。",
                {**positive, "description": "要求5年以上工作经验，负责 AIGC 产品设计。"},
            )["eligible"]
        )
        with patch.object(models, "model_json") as model:
            for i, expected in enumerate(("AIGC 产品经理", "agent 产品经理")):
                query = models.search_query(cfg, cv, sequence=i)
                self.assertEqual(platforms.platform_term(query, cfg["location"]), expected)
            model.assert_not_called()
        rejected = {**negative, "location": "上海"}
        with patch.object(models, "rate") as model:
            self.engine.evaluate(cfg, {"id": "fixture", "text": cv}, [rejected], self.fail)
            model.assert_not_called()
        self.assertFalse(json.loads(self.store.rows("SELECT rating FROM jobs")[0]["rating"])["eligible"])
        self.assertFalse(self.store.rows("SELECT id FROM actions"))

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
            self.assertEqual((1, 1, 2), (greet.call_count, send.call_count, mail.call_count))
            self.assertEqual(PROFILE.encode(), send.call_args.args[3]["body"])
            self.assertEqual("hr-1", send.call_args.args[2])
            self.assertTrue(any("联系方式" in call.args[2] for call in mail.call_args_list))
            self.assertTrue(any("简历已发送" in call.args[1] for call in mail.call_args_list))
            self.assertIn("Python", greet.call_args.args[2])
            self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 1)
            self.assertEqual(len(self.store.rows("SELECT * FROM actions WHERE state='已完成'")), 5)

    def test_source_regions_scope_patrol_and_saved_opportunities(self):
        sites = {"domestic_sites": "https://jobs.example.cn", "overseas_sites": "https://careers.example.com"}
        domestic = {**JOB, "source": "企业官网", "url": sites["domestic_sites"] + "/jobs/1"}
        overseas = {**JOB, "source": "greenhouse:example", "url": "https://example.org/jobs/2"}
        for selected in (["domestic"], ["overseas"], ["domestic", "overseas"]):
            cfg = {**self.cfg, **sites, "source_regions": selected, "boards": "greenhouse:example", "outreach": False}
            self.store.put("settings", cfg)
            with patch.object(platforms, "discover", return_value=[JOB]) as platform, patch.object(
                sources, "board", return_value=[overseas]
            ) as board, patch.object(sources, "page_jobs", return_value=[domestic]) as site, patch(
                "desk.engine.email_notice", return_value="accepted"
            ):
                self.engine.run("定时")
                self.assertEqual(platform.called, "domestic" in selected)
                self.assertEqual(board.called, "overseas" in selected)
                self.assertEqual({call.args[0] for call in site.call_args_list},
                                 {sites[k + "_sites"] for k in selected})
            handler = SimpleNamespace(server=SimpleNamespace(store=self.store, engine=self.engine))
            rows = DeskHandler.state(handler)["jobs"]
            expected = ({"企业官网", "boss"} if "domestic" in selected else set()) | (
                {"greenhouse:example"} if "overseas" in selected else set()
            )
            self.assertEqual({j["source"] for j in rows}, expected)
        with self.assertRaises(ValueError):
            validate_settings({"source_regions": []}, cfg)

    def test_resume_alert_retries_without_sending_twice(self):
        self.store.put("settings", {**self.cfg, "email_enabled": False, "windows_notifications": False})
        with patch.object(platforms, "discover", return_value=[JOB]), patch.object(
            platforms, "greet", return_value="hr-1"
        ), patch.object(platforms, "incoming", return_value=[("e1", "resume_request")]), patch.object(
            platforms, "send_resume", return_value="hr-1"
        ) as send:
            self.engine.run("定时")
            with patch.object(notifications, "desktop_notice", side_effect=platforms.NeedsSetup("系统通知未就绪")):
                self.engine.run("定时")
                self.assertEqual(self.store.rows("SELECT state FROM actions WHERE kind='投递提醒'")[0]["state"], "待处理")
            if os.name != "nt":
                self.store.put("settings", {**self.store.settings(), "windows_notifications": True})
            with patch.object(notifications, "desktop_notice", return_value="已提交") as desktop:
                Engine(self.store).run("定时")
                self.engine.run("定时")
                desktop.assert_called_once()
                self.assertIn("简历已发送", desktop.call_args.args[0])
            send.assert_called_once()
            self.assertEqual(self.store.rows("SELECT state FROM actions WHERE kind='投递提醒'")[0]["state"], "已完成")

    def test_manual_submission_invitation_sends_only_the_reminder(self):
        self.store.put("settings", {**self.cfg, "resume_auto": False})
        with patch.object(platforms, "discover", return_value=[JOB]), patch.object(
            platforms, "greet", return_value="hr"
        ), patch.object(platforms, "incoming", return_value=[("invite", "resume_request")]), patch.object(
            platforms, "send_resume"
        ) as send, patch("desk.engine.email_notice", return_value="已接收") as mail:
            for _ in range(3):
                self.engine.run("定时")
            send.assert_not_called()
            mail.assert_called_once()
            self.assertIn("HR 邀请投递简历", mail.call_args.args[1])

    def test_public_patrol_stops_at_access_restriction_without_loading_login(self):
        with patch.object(sources, "fetch", side_effect=sources.SourceHTTPError(403)) as fetch, patch.object(
            platforms, "session", side_effect=AssertionError("禁止打开缓存账号")
        ) as session, patch.object(platforms, "greet") as greet:
            self.engine.run("定时")
            self.engine.run("定时")
            fetch.assert_called_once()
            session.assert_not_called()
            greet.assert_not_called()
            self.assertIn("boss", self.store.get("verification_pending"))

    def test_public_postings_only_open_chat_for_a_qualified_role(self):
        postings = [{
            "@type": "JobPosting", "title": title, "description": description,
            "url": "https://www.zhipin.com/job_detail/" + slug + ".html",
            "hiringOrganization": {"name": "测试企业"},
            "jobLocation": {"address": {"addressLocality": "上海"}},
        } for slug, title, description in [
            ("data", JOB["title"], JD), ("driver", "货车司机", "负责长途货运和日常车辆保养")
        ]]
        doc = '<script type="application/ld+json">' + json.dumps(postings) + "</script>"
        with patch.object(sources, "fetch", return_value=(doc.encode(), platforms.search_url("boss", "分析"))) as fetch, patch.object(
            platforms, "session", side_effect=AssertionError("公开搜索不可访问账号")
        ), patch.object(platforms, "greet", return_value="hr-1") as greet:
            self.engine.run("定时")
            fetch.assert_called_once()
            greet.assert_called_once()
            self.assertEqual(greet.call_args.args[1]["external_id"], "/job_detail/data.html")
            self.assertEqual(len(self.store.rows("SELECT id FROM jobs")), 2)

    def test_platform_preview_and_partial_record_upgrades(self):
        self.store.put("settings", {**self.cfg, "location": "中国", "threshold": 0})
        self.store.put(searxng.KEY, {"enabled": True, "endpoint": "http://localhost:1234"})
        index = {**JOB, "location": "", "company": "雇主待核实", "partial": True, "indexed": True}
        with patch.object(
            platforms, "discover", side_effect=platforms.VerificationRequired()
        ) as direct, patch.object(platforms, "indexed_jobs", return_value=[index]) as indexed:
            self.engine.run("只读复查", "boss")
            recorded = json.loads(
                self.store.rows("SELECT reports FROM runs ORDER BY id DESC LIMIT 1")[0]["reports"]
            )
            self.assertEqual(len(recorded[-1]["added"]), 1)
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
        with patch.object(platforms, "discover", return_value=[index]):
            self.engine.run("只读复查", "boss", True)
            self.assertIn("boss", self.store.get("verification_pending"))
        with patch.object(platforms, "discover", return_value=[JOB]) as collect:
            self.engine.run("只读复查", "boss", True)
            self.assertTrue(collect.call_args.kwargs["interactive"])
            upgraded = json.loads(
                self.store.rows("SELECT reports FROM runs ORDER BY id DESC LIMIT 1")[0]["reports"]
            )
            self.assertEqual(upgraded[-1]["updated"], recorded[-1]["added"])
        self.assertIn("boss", self.store.get("verification_pending"))
        self.assertEqual(rating()["mode"], "local")
        self.assertTrue(rating()["eligible"])
        self.engine.evaluate(self.cfg, self.store.profile(full=True), [index], self.fail)
        self.assertEqual(rating()["mode"], "local")
        self.assertFalse(self.store.rows("SELECT id FROM actions"))
        self.assertFalse(self.store.get("due"))

    def test_patrol_keeps_saved_cards_visible_alongside_verification_errors(self):
        card = {**JOB, "partial": True}
        with patch.object(platforms, "discover", side_effect=platforms.VerificationRequired([card])):
            self.engine.run("只读复查", "boss", True)
            self.engine.run("只读复查", "boss", True)
        reports = [
            json.loads(row["reports"])[0] for row in self.store.rows("SELECT reports FROM runs ORDER BY id")
        ]
        self.assertTrue(reports[0]["error"])
        self.assertEqual((reports[0]["count"], reports[0]["leads"], len(reports[0]["added"])), (1, 1, 1))
        self.assertEqual((reports[1]["added"], reports[1]["updated"], reports[1]["unchanged"]), ([], [], 1))
        self.assertTrue(
            all(row["status"] == "部分完成" for row in self.store.rows("SELECT status FROM runs"))
        )
        with patch.object(platforms, "discover", return_value=[{**card, "title": "更新后的岗位名称"}]):
            self.engine.run("只读复查", "boss", True)
        latest = json.loads(
            self.store.rows("SELECT reports FROM runs ORDER BY id DESC LIMIT 1")[0]["reports"]
        )[0]
        self.assertEqual(latest["updated"], reports[0]["added"])

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
                        {"url": "https://www.zhaopin.com/jobdetail/unrelated.htm", "title": "工业设计师", "content": "外观设计"},
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


_browser_context = platforms.browser_context

def local_test_browser(pw, executable, profile, key, **options):
    return _browser_context(pw, executable, profile, key, interactive=False)


class LoginRoutingTests(unittest.TestCase):
    def test_liepin_review_starts_at_search_with_one_tab_and_follows_redirect(self):
        requests = []
        path = "/job/123.shtml"

        class LocalSite(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith("/search?"):
                    self.send_response(302)
                    self.send_header("Location", "/security-check.html" if self.path == "/search?key=gate" else "/results")
                    if self.path == "/search?key=Python":
                        self.send_header("Set-Cookie", "review=fixture; Path=/; HttpOnly")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = (
                    f'<title>搜索结果</title><a href="{path}">分析师</a>'
                    if self.path == "/results"
                    else '<h1>分析师</h1><div class="job-description">SQL</div><b class="company-name">企业</b>'
                )
                if self.path == path and "review=fixture" not in self.headers.get("Cookie", ""):
                    html = "<title>用户登录</title>"
                self.wfile.write(html.encode())

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), LocalSite) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = f"http://127.0.0.1:{server.server_port}"
            root = Path(tmp)
            try:
                with patch.dict(platforms.PLATFORMS["liepin"], search=origin + "/search?key="), patch.object(
                    platforms, "on_site", side_effect=lambda url, site: url.startswith(origin + "/")
                ), patch.object(platforms, "browser_context", local_test_browser):
                    # Reproduce a profile left by the previous about:blank preparation.
                    with platforms.session(root, "liepin") as ctx:
                        ctx.new_page()
                    url = platforms.search_url("liepin", "SQL")
                    with platforms.session(root, "liepin", interactive=True) as ctx:
                        page = platforms.review_start_page(ctx, url, platforms.PLATFORMS["liepin"])
                        page.wait_for_url(origin + "/results")
                        self.assertEqual(ctx.pages, [page])
                        self.assertEqual(page.locator("a").inner_text(), "分析师")
                    self.assertEqual(requests.count("/search?key=SQL"), 1)
                    self.assertEqual(requests.count("/results"), 1)
                    # The previous review left an official (nonblank) page in Chrome's session.
                    # A new review should fetch its own search once, then return the card without opening details.
                    requests.clear()
                    jobs = platforms.discover(root, "liepin", "Python", interactive=True)
                    self.assertEqual([job["external_id"] for job in jobs], [path])
                    self.assertTrue(jobs[0]["partial"])
                    self.assertEqual(
                        [p for p in requests if p != "/favicon.ico"],
                        ["/search?key=Python", "/results"],
                    )
                    requests.clear()
                    with self.assertRaises(platforms.VerificationRequired):
                        platforms.discover(root, "liepin", "gate", interactive=True)
                    self.assertEqual(requests.count("/search?key=gate"), 1)
                    self.assertNotIn(path, requests)
            finally:
                server.shutdown()

    def test_manual_login_starts_at_official_url_and_keeps_the_patrol_profile(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            platforms, "sync_playwright"
        ) as driver, patch.object(
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
                self.assertIn("--restore-last-session", command)
                self.assertNotIn("about:blank", command)
                self.assertFalse(any("remote-debugging" in arg for arg in command))
                self.assertFalse(platforms.LOCKS[key].locked())
                prefs = root / "profiles" / key / "Default" / "Preferences"
                prefs.parent.mkdir()
                prefs.write_text("{}", encoding="utf-8")
                platforms.login_window(root, key, lambda _: None)
                self.assertEqual(launch.call_args.args[0][-1], site["home"])

    def test_restart_restores_session_without_reopening_tabs_and_respects_logout(self):
        visits = []

        class LocalSite(BaseHTTPRequestHandler):
            def do_GET(self):
                visits.append(self.path)
                self.send_response(200)
                if self.path == "/login":
                    self.send_header("Set-Cookie", "user=fixture; Path=/; HttpOnly")
                elif self.path == "/logout":
                    self.send_header("Set-Cookie", "user=; Path=/; Max-Age=0")
                self.end_headers()
                self.wfile.write(b"member" if "user=fixture" in self.headers.get("Cookie", "") else b"guest")

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), LocalSite) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with sync_playwright() as pw:
                    self.assertTrue(
                        platforms.prepare_visible_profile(
                            pw, platforms.browser_executable(pw), Path(tmp) / "profiles" / "boss", "boss"
                        )
                    )
                for key, path, expected in (
                    ("boss", "/login", "guest"),
                    ("boss", "/account", "member"),
                    ("liepin", "/account", "guest"),
                    ("boss", "/logout", "member"),
                    ("boss", "/account", "guest"),
                ):
                    with self.subTest(key=key, path=path), platforms.session(Path(tmp), key) as ctx:
                        self.assertEqual(ctx.pages, [])
                        page = ctx.new_page()
                        page.goto(origin + path)
                        self.assertEqual(page.locator("body").inner_text(), expected)
                        if path == "/login":
                            page.close()
                self.assertEqual(
                    [p for p in visits if p != "/favicon.ico"],
                    ["/login", "/account", "/account", "/logout", "/account"],
                )
            finally:
                server.shutdown()

    def test_browser_startup_has_no_platform_probe(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(platforms.requests, "Session") as probe, patch.object(
            platforms, "sync_playwright"
        ) as driver, patch.object(platforms.subprocess, "Popen") as launch:
            launch.return_value.wait.return_value = 0
            driver.return_value.__enter__.return_value.chromium.executable_path = "chromium"
            platforms.login_window(Path(tmp), "boss", lambda _: None)
            probe.assert_not_called()
            self.assertNotIn("--no-proxy-server", launch.call_args.args[0])


class BrowserTests(unittest.TestCase):

    def test_sources_import_and_independent_index_controls(self):
        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), DeskHandler) as server:
            server.store = Store(tmp)
            server.store.save_resume("cv.txt", PROFILE.encode(), PROFILE)
            server.store.put("verification_pending", {"boss": 1})
            server.config_lock = threading.RLock()
            server.engine = Engine(server.store, server.config_lock)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            page = self.browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/#sources")
                page.get_by_label("猎聘", exact=True).uncheck()
                with page.expect_response(lambda r: r.url.endswith("/api/settings") and r.ok):
                    page.get_by_role("button", name="保存岗位来源").click()
                self.assertNotIn("liepin", server.store.settings()["platform_indexes"])
                self.assertFalse(server.store.settings()["platforms"])
                page.get_by_role("button", name="我已核对，解除后台直连暂停").click()
                page.get_by_role("button", name="我已核对，解除后台直连暂停").wait_for(state="detached")
                page.locator('form[data-form="import-cards"] textarea').fill("样例岗位 " + JOB["url"])
                with page.expect_response(lambda r: r.url.endswith("/api/jobs/import") and r.ok):
                    page.get_by_role("button", name="导入岗位", exact=True).click()
                self.assertEqual(server.store.rows("SELECT title FROM jobs")[0]["title"], "样例岗位")
            finally:
                page.close()
                server.shutdown()

    def test_region_checkboxes_filter_existing_jobs_and_survive_reload(self):
        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), DeskHandler) as server:
            server.store = Store(tmp)
            server.store.upsert({**JOB, "title": "国内样例岗位"}, {}, "")
            server.store.upsert({**JOB, "source": "greenhouse:example", "title": "海外平台的上海岗位"}, {}, "")
            server.config_lock = threading.RLock()
            server.engine = Engine(server.store, server.config_lock)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            page = self.browser.new_page()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                page.goto(origin + "/#sources")
                page.get_by_label("国内平台", exact=True).uncheck()
                with page.expect_response(lambda r: r.url.endswith("/api/settings") and r.status == 200):
                    page.get_by_role("button", name="保存岗位来源").click()
                page.reload()
                self.assertFalse(page.get_by_label("国内平台", exact=True).is_checked())
                self.assertTrue(page.get_by_label("海外平台", exact=True).is_checked())
                page.goto(origin + "/#jobs")
                page.get_by_text("海外平台的上海岗位", exact=True).wait_for()
                self.assertEqual(page.get_by_text("国内样例岗位", exact=True).count(), 0)
            finally:
                page.close()
                server.shutdown()

    @contextmanager
    def review_fixture(self, session):
        # Route fixture navigations inside the isolated incognito context.
        # LoginRoutingTests exercises actual window creation with a persistent profile.
        def open_search(ctx, url, site):
            page = ctx.new_page()
            page.goto(url)
            return page

        with patch.object(platforms, "session", session), patch.object(platforms, "review_start_page", open_search):
            yield

    def test_filter_assistant_in_workbench(self):
        rules = {k: [] for k in filter_assistant.FIELDS}
        rules.update(
            titles=["数据分析师"], cities=["上海"], prefer=["SQL"], queries=["数据分析师"], min_monthly_cny=0
        )
        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(
            ("127.0.0.1", 0), DeskHandler
        ) as server:
            server.store = Store(tmp)
            server.store.save_resume("cv.txt", PROFILE.encode(), PROFILE)
            server.store.put("settings", {**server.store.settings(), "provider": "pi"})
            server.store.upsert(JOB, {}, "")
            server.config_lock = threading.RLock()
            server.engine = Engine(server.store, server.config_lock)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            page = self.browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/#resume")
                panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="自然语言筛选助手")
                )
                panel.get_by_role("textbox").fill("上海数据分析师，SQL 优先")
                with patch.object(models, "model_json", return_value=rules):
                    panel.get_by_role("button", name="生成并应用条件").click()
                    panel.get_by_text("已启用", exact=True).wait_for()
                panel.get_by_role("link", name="查看符合条件的岗位（1）").click()
                page.get_by_role("heading", name="符合助手条件的岗位").wait_for()
                self.assertEqual(page.locator("[data-job]").count(), 1)
                page.locator('nav a[href="#resume"]').click()
                panel.get_by_role("button", name="停用并恢复手动偏好").click()
                panel.get_by_text("已启用", exact=True).wait_for(state="detached")
            finally:
                page.close()
                server.shutdown()

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
                            '<title>搜索</title><div class="job-list-box"></div>'
                            '<script>setTimeout(()=>{document.title="搜索";let a=document.createElement("a");'
                            f'a.href="{path}";a.textContent="数据分析师";'
                            'document.querySelector(".job-list-box").append(a)},100)</script>'
                        )
                    )
                    route.fulfill(content_type="text/html; charset=utf-8", body=html)

                ctx.route("**/*", response)
                try:
                    self.review_context = ctx
                    yield ctx
                finally:
                    ctx.close()

            @contextmanager
            def open_detail(count):
                self.review_context.pages[0].locator("a").first.click()
                yield lambda: True

            with self.subTest(key=key), self.review_fixture(fake_session):
                job = platforms.discover(None, key, "数据分析师", interactive=True, review_window=open_detail)[0]
                self.assertEqual(
                    (job["external_id"], job["description"], job["company"]), (path, "需要 SQL", "示例公司")
                )
                self.assertFalse(job.get("partial"))
        self.assertIsNone(re.fullmatch(platforms.ROLE_PATHS["boss"], "/job_detail/"))

    def test_liepin_review_reports_blank_after_official_page(self):
        @contextmanager
        def fixture(*args, **kwargs):
            with self.browser.new_context() as ctx:
                ctx.route("**/*", lambda r: r.fulfill(
                    content_type="text/html", body="<script>setTimeout(() => location.replace('about:blank'), 200)</script>"
                ))
                yield ctx

        with self.review_fixture(fixture), self.assertRaisesRegex(
            platforms.IncompleteReview, "空白页"
        ):
            platforms.discover(None, "liepin", "SQL", interactive=True)

    def test_search_redirect_to_verification_is_not_followed(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.is_redirect = True
        response.headers = {"Location": "/web/common/security-check.html?next=jobs"}
        with patch.object(sources, "public_url"), patch.object(sources.requests, "request", return_value=response) as request:
            with self.assertRaises(platforms.VerificationRequired):
                platforms.public_discover("boss", "SQL")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], platforms.search_url("boss", "SQL"))

    def test_visible_review_stops_a_site_reload_loop(self):
        visits = []

        @contextmanager
        def looping_session(*args, **kwargs):
            ctx = self.browser.new_context()

            def loop(route):
                visits.append(route.request.url)
                route.fulfill(
                    content_type="text/html", body="<script>setTimeout(()=>location.reload(), 150)</script>"
                )

            ctx.route("**/*", loop)
            try:
                yield ctx
            finally:
                ctx.close()

        with self.review_fixture(looping_session), self.assertRaisesRegex(
            platforms.VerificationRequired, "反复跳转"
        ):
            platforms.discover(None, "boss", "测试", interactive=True)
        self.assertLess(len(visits), 14)
        self.assertGreaterEqual(len(visits), 4)

    def test_body_only_challenge_on_unchanged_listing_preserves_eight_cards(self):
        visits = []
        @contextmanager
        def fixture(*args, **kwargs):
            self.assertTrue(kwargs["interactive"])
            with self.browser.new_context() as ctx:
                self.review_context = ctx
                def respond(route):
                    visits.append(route.request.url)
                    html = ''.join(f'<a href="/job/{i}.shtml">Analyst</a>' for i in range(8))
                    route.fulfill(content_type="text/html", body=html)
                ctx.route("**/*", respond)
                yield ctx

        @contextmanager
        def show_challenge(count):
            self.assertEqual(count, 8)
            self.review_context.pages[0].evaluate(
                """() => document.body.innerHTML = '<p>当前 IP 地址可能存在异常访问行为，完成验证即可正常使用。</p>'"""
            )
            yield lambda: True

        with self.review_fixture(fixture), self.assertRaises(platforms.VerificationRequired) as gate:
            platforms.discover(None, "liepin", "SQL", interactive=True, review_window=show_challenge)
        self.assertEqual(len(gate.exception.jobs), 8)
        self.assertEqual(visits, [platforms.search_url("liepin", "SQL")])

    def test_boss_review_accepts_same_path_with_changing_query(self):
        visits = []

        @contextmanager
        def fixture(*args, **kwargs):
            with self.browser.new_context() as ctx:
                def respond(route):
                    visits.append(route.request.url)
                    if len(visits) <= 5:
                        html = f"<script>setTimeout(()=>location.replace('?step={len(visits)}'),100)</script>"
                    elif len(visits) == 6:
                        html = '<a href="/job_detail/sql.html">分析师</a>'
                    else:
                        html = '<h1>分析师</h1><div class="job-sec-text">SQL</div><div class="company-info"><b class="name">企业</b></div>'
                    route.fulfill(content_type="text/html; charset=utf-8", body=html)
                ctx.route("**/*", respond)
                yield ctx

        with self.review_fixture(fixture):
            jobs = platforms.discover(None, "boss", "SQL", interactive=True)
        self.assertEqual(len(visits), 6)
        self.assertTrue(jobs[0]["partial"])

    def test_boss_visible_review_reads_only_the_job_opened_by_the_user(self):
        visits = []
        paths = ["/job_detail/one.html", "/job_detail/two.html"]
        @contextmanager
        def fixture(*args, **kwargs):
            self.assertTrue(kwargs["interactive"])
            with self.browser.new_context() as ctx:
                def respond(route):
                    path = urlsplit(route.request.url).path
                    visits.append(path)
                    html = (
                        '<h1>Analyst</h1><div class="job-sec-text">SQL</div><div class="company-info"><b class="name">Test Co</b></div>'
                        if path in paths else "".join(f'<a href="{p}">Analyst</a>' for p in paths)
                    )
                    route.fulfill(content_type="text/html", body=html)
                ctx.route("**/*", respond)
                self.review_context = ctx
                yield ctx

        @contextmanager
        def manual_window(count):
            self.assertEqual(count, 2)
            self.assertEqual(len(visits), 1)
            page = self.review_context.pages[0]
            page.locator("a").first.click()
            checks = iter([False, True])
            yield lambda: next(checks)

        with self.review_fixture(fixture):
            jobs = platforms.discover(None, "boss", "SQL", interactive=True, review_window=manual_window)
        self.assertEqual(visits, ["/web/geek/jobs", paths[0]])
        self.assertEqual(jobs[0]["description"], "SQL")
        self.assertFalse(jobs[0].get("partial"))
        self.assertTrue(jobs[1]["partial"])

    def test_finish_visible_review_from_workbench(self):
        visits = []
        class Listing(BaseHTTPRequestHandler):
            def do_GET(self):
                visits.append(urlsplit(self.path).path)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b'<a href="/job_detail/example.html">Analyst</a>')

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(
            ("127.0.0.1", 0), DeskHandler
        ) as server, ThreadingHTTPServer(("127.0.0.1", 0), Listing) as listing:
            server.store = Store(tmp)
            server.store.save_resume("cv.txt", PROFILE.encode(), PROFILE)
            server.config_lock = threading.RLock()
            server.engine = Engine(server.store, server.config_lock)
            for http in (server, listing):
                threading.Thread(target=http.serve_forever, daemon=True).start()
            origin = f"http://127.0.0.1:{listing.server_port}"
            page = self.browser.new_page()
            try:
                with patch.dict(platforms.PLATFORMS["boss"], search=origin + "/search?key="), patch.object(
                    platforms, "on_site", side_effect=lambda url, site: url.startswith(origin + "/")
                ), patch.object(platforms, "browser_context", local_test_browser):
                    page.goto(f"http://127.0.0.1:{server.server_port}/#sources")
                    page.locator('[data-act="preview-boss"]').click()
                    page.get_by_role("button", name="完成窗口核对").click(timeout=20000)
                    page.get_by_text(re.compile("读取 1 个岗位")).first.wait_for(timeout=15000)
                reports = json.loads(server.store.rows("SELECT reports FROM runs")[0]["reports"])
                self.assertEqual((reports[0]["count"], reports[0]["leads"], reports[0]["error"]), (1, 1, ""))
                self.assertEqual([path for path in visits if path != "/favicon.ico"], ["/search"])
            finally:
                server.engine.stop.set()
                page.close()
                server.shutdown()
                listing.shutdown()

    def test_public_detail_failures_keep_cards_and_limit_navigation(self):
        paths = [f"/job/{n}.shtml" for n in range(1, 9)]
        for failures, requests_expected, partial_count in (([5], 8, 1), ([5, 6], 6, 4)):
            visited = []
            def fetch(url, **kwargs):
                path = urlsplit(url).path
                if path == "/zhaopin/":
                    body = "".join(f'<a href="{p}">Analyst</a>' for p in paths)
                else:
                    visited.append(path)
                    if len(visited) in failures:
                        raise OSError("fixture connection reset")
                    body = '<h1>Analyst</h1><div class="job-description">SQL</div><div class="company-info-container"><b class="company-name">Co</b></div>'
                return body.encode(), url
            with self.subTest(failures=failures), patch.object(sources, "fetch", side_effect=fetch):
                with self.assertRaises(platforms.IncompleteReview) as result:
                    platforms.discover(None, "liepin", "SQL")
                self.assertEqual(visited, paths[:requests_expected])
                self.assertEqual(len(result.exception.jobs), 8)
                self.assertEqual(sum(bool(j.get("partial")) for j in result.exception.jobs), partial_count)

    def test_public_detail_rate_limit_preserves_cards_without_following_more(self):
        index = b'<a href="/job/1.shtml">One</a><a href="/job/2.shtml">Two</a>'
        with patch.object(sources, "fetch", side_effect=[(index, platforms.search_url("liepin", "SQL")), sources.SourceHTTPError(429)]) as fetch:
            with self.assertRaisesRegex(platforms.VerificationRequired, "HTTP 429") as result:
                platforms.public_discover("liepin", "SQL")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(result.exception.jobs), 2)

    def test_public_body_only_gate_wins_over_job_links(self):
        html = '<title>招聘</title><div>当前 IP 地址可能存在异常访问行为</div><a href="/job/1.shtml">Analyst</a>'
        with patch.object(sources, "fetch", return_value=(html, platforms.search_url("liepin", "SQL"))) as fetch:
            with self.assertRaises(platforms.VerificationRequired):
                platforms.public_discover("liepin", "SQL")
        fetch.assert_called_once()

    def test_visible_listing_reads_embedded_description_without_opening_detail(self):
        visits = []
        path = "/job/456.shtml"
        posting = {"@type": "JobPosting", "url": path, "title": "Analyst", "description": "SQL analytics",
                   "hiringOrganization": {"name": "Test Co"}}

        @contextmanager
        def fixture(*args, **kwargs):
            self.assertTrue(kwargs["interactive"])
            with self.browser.new_context() as ctx:
                def respond(route):
                    visits.append(route.request.url)
                    route.fulfill(content_type="text/html", body='<script type="application/ld+json">' +
                                  json.dumps(posting) + "</script>")
                ctx.route("**/*", respond)
                yield ctx

        with self.review_fixture(fixture):
            jobs = platforms.discover(None, "liepin", "SQL", interactive=True)
        self.assertEqual(visits, [platforms.search_url("liepin", "SQL")])
        self.assertEqual([(j["external_id"], j["description"], j.get("partial")) for j in jobs],
                         [(path, "SQL analytics", None)])

    def test_visible_body_gate_precedes_any_card_capture(self):
        @contextmanager
        def fixture(*args, **kwargs):
            with self.browser.new_context() as ctx:
                ctx.route("**/*", lambda r: r.fulfill(content_type="text/html; charset=utf-8",
                    body='<title>招聘</title><p>当前 IP 地址可能存在异常访问行为</p><a href="/job/1.shtml">Analyst</a>'))
                yield ctx

        with self.review_fixture(fixture), self.assertRaises(platforms.VerificationRequired) as gate:
            platforms.discover(None, "liepin", "SQL", interactive=True)
        self.assertFalse(gate.exception.jobs)

    def test_visible_review_never_reopens_profile_for_details(self):
        @contextmanager
        def fixture(*args, **kwargs):
            if not kwargs.get("interactive"):
                raise platforms.NeedsSetup("平台窗口正被使用")
            with self.browser.new_context() as ctx:
                ctx.route("**/*", lambda r: r.fulfill(
                    content_type="text/html", body='<a href="/job/123.shtml">Analyst</a>'
                ))
                yield ctx

        with self.review_fixture(fixture):
            jobs = platforms.discover(None, "liepin", "SQL", interactive=True)
        self.assertEqual([job["external_id"] for job in jobs], ["/job/123.shtml"])
        self.assertTrue(jobs[0]["partial"])

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

        def public_fetch(url, **kwargs):
            self.assertTrue(kwargs["allow_url"](url))
            content = details if "/job_detail/" in url else '<a href="' + JOB["url"] + '">数据分析师</a>'
            return content.encode(), url

        with patch.object(sources, "fetch", side_effect=public_fetch), patch.object(
            platforms, "session", side_effect=AssertionError("免登录发现不得打开账号资料")
        ):
            job = platforms.discover(None, "boss", "数据分析师")[0]
        with patch.object(platforms, "session", local_session):
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
                current_rating = json.loads(server.store.rows("SELECT rating FROM jobs LIMIT 1")[0]["rating"])
                server.store.upsert(
                    {**JOB, "title": "旧版工业设计摘要", "external_id": "archived-lead"},
                    {**current_rating, "score": 79, "mode": "index", "pref_match": False, "eligible": False},
                    "legacy",
                )
                page.reload()
                page.locator("button.job").first.wait_for()
                self.assertEqual(page.locator("button.job").count(), 1)
                self.assertNotIn("旧版工业设计摘要", page.locator("button.job").inner_text())
                page.goto(origin + "/#jobs")
                page.get_by_text("旧版工业设计摘要", exact=True).wait_for()
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
