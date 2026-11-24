"""Use the candidate's ordinary visible website controls and isolated browser profiles."""

import base64
import hashlib
import os
import re
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

from . import models, sources
from .sources import item


PLATFORMS = {
    "boss": dict(
        name="BOSS 直聘",
        domain="zhipin.com",
        home="https://www.zhipin.com/",
        login="https://www.zhipin.com/web/user/",
        search="https://www.zhipin.com/web/geek/jobs?query=",
        link="/job_detail/",
        jd=".job-sec-text, .job-detail .text",
        company=".company-info .name, .sider-company .company-name, .sider-company .company-info a, .boss-info-attr .boss-company, .job-company-info .company-name",
    ),
    "liepin": dict(
        name="猎聘",
        domain="liepin.com",
        home="https://www.liepin.com/",
        search="https://www.liepin.com/zhaopin/?key=",
        link="/job/",
        jd=".job-intro-container > .paragraph:first-child, .job-intro-content, .job-description",
        company=".company-info-container .company-card .content, .company-info-container .company-name",
    ),
    "zhilian": dict(
        name="智联招聘",
        domain="zhaopin.com",
        home="https://www.zhaopin.com/",
        login="https://passport.zhaopin.com/login",
        search="https://www.zhaopin.com/sou/kw",
        link="/jobdetail/",
        jd=".describestip__content, .job-detail-description",
        company=".company__title, .company-name, a[href*=companydetail]",
    ),
}
LOCKS = {k: threading.Lock() for k in PLATFORMS}
RESTORE_SESSION = "--restore-last-session"


class NeedsSetup(ValueError):
    """No external write has been attempted."""


VERIFY = "官网要求人工安全验证"
VERIFY_NOTE = "平台后台直连已暂停，请人工核对。公开索引可独立巡查"


class IncompleteReview(NeedsSetup):
    def __init__(self, jobs, reason, *, outcome="incomplete", phase=""):
        super().__init__(reason)
        self.jobs = jobs
        self.outcome = outcome
        self.phase = phase


class VerificationRequired(IncompleteReview):
    def __init__(self, jobs=(), reason=VERIFY, *, outcome="verification", phase=""):
        super().__init__(jobs, reason, outcome=outcome, phase=phase)


def access_error(reason, jobs=()):
    return VerificationRequired(jobs, reason, outcome="verification" if reason == VERIFY else "login_required")


def on_site(url, site):
    host = (urlsplit(url).hostname or "").lower()
    return url.startswith("https://") and (host == site["domain"] or host.endswith("." + site["domain"]))


def visible(locator):
    for node in locator.all():
        if node.is_visible():
            return node
    return None


def text_of(page, selector):
    node = visible(page.locator(selector))
    return node.inner_text().strip() if node else ""


def browser_executable(pw):
    if os.name == "nt":
        import winreg

        command = r"SOFTWARE\Clients\StartMenuInternet\Google Chrome\shell\open\command"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, command) as entry:
                    value = winreg.QueryValueEx(entry, None)[0].strip()
            except OSError:
                continue
            candidate = Path(value.split('"')[1] if value.startswith('"') else value)
            if candidate.name.lower() == "chrome.exe" and candidate.is_absolute() and candidate.is_file():
                return str(candidate)
    return pw.chromium.executable_path


@contextmanager
def browser_context(pw, executable, profile, key, *, interactive=False):
    # Restore session cookies but suppress Chrome's automatic tab restoration.
    args = [
        f"--user-data-dir={profile}",
        "--remote-debugging-pipe",
        "--enable-automation",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-background-networking",
        RESTORE_SESSION,
        "--no-startup-window",
    ]
    if not interactive:
        args.append("--headless=new")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    ctx = pw.chromium.launch_persistent_context(
        str(profile),
        executable_path=executable,
        args=args,
        ignore_default_args=True,
        headless=not interactive,
        viewport={"width": 1360, "height": 880},
        locale="zh-CN",
        accept_downloads=False,
    )
    try:
        ctx.set_default_timeout(7500)
        yield ctx
    finally:
        if ctx.browser is None or ctx.browser.is_connected():
            ctx.close()


@contextmanager
def session(root, key, *, interactive=False, require_profile=False):
    lock = LOCKS[key]
    if not lock.acquire(blocking=False):
        raise NeedsSetup("平台窗口正被使用，请关闭登录窗口后重试")
    try:
        with sync_playwright() as pw:
            profile = (root / "profiles" / key).resolve()
            if require_profile and not (profile / "Default" / "Preferences").is_file():
                raise NeedsSetup("没有已保存的平台登录资料，请先在来源与账号中登录")
            executable = browser_executable(pw)
            if interactive:
                prepare_visible_profile(pw, executable, profile, key)
            with browser_context(pw, executable, profile, key, interactive=interactive) as ctx:
                yield ctx
    finally:
        lock.release()


def prepare_visible_profile(pw, executable, profile, key):
    # Chrome protects this preference. Let its own settings page write it so
    # clicking X on the last native login window preserves session-only cookies.
    with browser_context(pw, executable, profile, key) as ctx:
        page = ctx.new_page()
        try:
            page.goto("chrome://settings/onStartup")
            return page.evaluate(
                """async () => {
                const settings = chrome.settingsPrivate;
                const name = 'session.restore_on_startup';
                const pref = await settings.getPref(name);
                if (pref.value === 1) return true;
                if (pref.enforcement === 'ENFORCED') return false;
                await new Promise(resolve => settings.setPref(name, 1, '', resolve));
                return (await settings.getPref(name)).value === 1;
            }"""
            )
        except BrowserError:
            return False
        finally:
            for restored in ctx.pages:
                if restored != page and restored.url == "about:blank":
                    restored.close()
            page.close()


def login_window(root, key, status, search=None):
    # Hand the official URL to Chromium at startup. Human authentication runs in its
    # own browser process, using the same per-platform profile as subsequent patrols.
    site = PLATFORMS[key]
    lock = LOCKS[key]
    if not lock.acquire(blocking=False):
        raise NeedsSetup("该平台窗口正在使用，请切换到已打开的窗口")
    try:
        profile = (root / "profiles" / key).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        returning = (profile / "Default" / "Preferences").is_file()
        start_url = site["home"] if returning else site.get("login", site["home"])
        with sync_playwright() as pw:
            executable = browser_executable(pw)
            restore_enabled = prepare_visible_profile(pw, executable, profile, key)
        command = [
            executable,
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
            RESTORE_SESSION,
            search_url(key, search) if search else start_url,
        ]
        try:
            browser = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise NeedsSetup("未找到 Chromium，请使用启动工作台.cmd 安装浏览器") from exc
        status(
            "已打开平台官网，请查看登录状态"
            if restore_enabled
            else "已打开官网，浏览器设置限制了关闭后的会话恢复"
        )
        if browser.wait() != 0:
            raise NeedsSetup("登录窗口异常退出，请重新打开")
        return restore_enabled
    finally:
        lock.release()


def visit(page, url, site):
    if not on_site(url, site):
        raise NeedsSetup("页面不属于所选招聘平台")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception as exc:
        raise NeedsSetup("平台网页连接失败，请检查本机网络并在登录窗口核对") from exc
    if not on_site(page.url, site):
        raise NeedsSetup("平台跳转到登录或验证页面，请重新登录")


def platform_term(query, location):
    places = {p.strip().casefold() for p in re.split(r"[,，、;；|/\n]+", location) if p.strip()}
    words = [w for w in re.split(r"[\s,，、]+", query) if w and w.casefold() not in places]
    return " ".join(words) or "招聘"


def search_url(key, query):
    term = (
        base64.b32hexencode(query.encode("utf-16-be")).decode().rstrip("=")
        if key == "zhilian"
        else quote(query)
    )
    return PLATFORMS[key]["search"] + term


ROLE_PATHS = {
    "boss": r"^/job_detail/[A-Za-z0-9_-]+\.html$",
    "liepin": r"^/(?:job|a)/[0-9]+\.shtml$",
    "zhilian": r"^/jobdetail/[A-Za-z0-9_-]+\.htm[l]?$",
}
INDEX_SITES = {**PLATFORMS, "51job": dict(name="前程无忧", domain="jobs.51job.com", link="/")}
INDEX_PATHS = {**ROLE_PATHS, "51job": r"^/[a-zA-Z0-9_-]+/[0-9]+\.html$"}


def listing_identity(url):
    try:
        address = urlsplit(url)
        allowed = not (address.username or address.password) and address.port in (None, 443)
    except ValueError:
        return None
    if not allowed:
        return None
    for key, site in INDEX_SITES.items():
        if on_site(url, site) and re.fullmatch(INDEX_PATHS[key], address.path):
            return key, address.path
    return None
LIST_SELECTORS = {
    "boss": 'a[href*="/job_detail/"][href*=".html"]',
    "liepin": 'a[href*="/job/"][href*=".shtml"],a[href*="/a/"][href*=".shtml"]',
    "zhilian": 'a[href*="/jobdetail/"][href*=".htm"]',
}
JOB_TITLE = ".job-apply-container .job-title, h1, .job-name .name, .summary-plane__title"
JOB_CITY = ".job-apply-container .job-properties > span:first-child, .text-city, .job-address, .job-address-text, .summary-plane__info--location"


def access_state(url, title="", body=""):
    path = urlsplit(url).path.lower()
    if re.search(r"/(?:captcha|security|verify|verifycode)(?:[/._-]|$)", path) or any(
        word in title.lower() for word in ("security verification", "安全验证", "安全中心", "验证中心", "账号行为异常")
    ):
        return VERIFY
    text = "".join(body.lower().split())
    if any(s in text for s in ("当前ip地址可能存在异常访问行为", "已暂时禁止访问", "您的账号存在异常访问行为")) or (
        "请完成以下验证" in text and "点击按钮进行验证" in text
    ):
        return VERIFY
    if (
        re.search(r"/(?:login|passport)(?:[/._-]|$)", path)
        or url.split("?", 1)[0].rstrip("/") == PLATFORMS["boss"]["login"].rstrip("/")
        or (urlsplit(url).hostname or "").startswith("passport.")
        or any(word in title for word in ("用户登录", "登录注册", "注册登录"))
    ):
        return "官网要求在当前浏览器会话登录"
    return ""


def page_state(page):
    if page.url == "about:blank":
        return "官网跳回空白页"
    return access_state(*page.evaluate("() => [location.href, document.title, document.body?.innerText || '']"))


class ReviewNavigation:
    def __init__(self, ctx, site, diagnostics=None):
        self.listing = None
        self.site = site
        self.recent = deque(maxlen=32)
        self.loop = False
        self.blocked = None
        self.diagnostics = {} if diagnostics is None else diagnostics
        self.diagnostics.update(documents=0, redirects=0)
        ctx.on("response", self.record)

    def record(self, response):
        if response.request.resource_type != "document" or not on_site(response.url, self.site):
            return
        try:
            frame = response.frame
        except BrowserError:
            # Chromium may report the initial redirect before creating a frame.
            if not response.request.is_navigation_request():
                return
            frame = None
        if frame is not None and frame.parent_frame is not None:
            return
        self.diagnostics["documents"] += 1
        self.diagnostics["last_status"] = response.status
        reason = access_state(response.url)
        if reason:
            self.blocked = access_error(reason)
        elif response.status in (401, 403, 429):
            self.blocked = VerificationRequired(reason=f"官网限制访问（HTTP {response.status}）", outcome="http_restriction")
        if 300 <= response.status < 400:
            self.diagnostics["redirects"] += 1
            target = urljoin(response.url, response.headers.get("location", ""))
            reason = access_state(target)
            if reason:
                self.blocked = access_error(reason)
            return
        if self.listing is not None and frame != self.listing.main_frame:
            return
        url = urlsplit(response.url)
        if url.scheme not in ("https", "http"):
            return
        current = time.monotonic()
        route = (url.netloc, url.path, url.query)
        self.recent.append((current, route))
        recent = [r for stamp, r in self.recent if current - stamp <= 20]
        # A sign-in flow may visit the same path with successive query parameters.
        # Stop a reload or repeating redirect chain even if documents load slowly.
        same_page = len(recent) >= 3 and recent[-3:] == [route] * 3
        cycle = any(
            len(recent) >= 2 * size and recent[-size:] == recent[-2 * size:-size]
            for size in (2, 3)
        )
        rapid_loads = sum(current - stamp <= 8 for stamp, _ in self.recent)
        if same_page or cycle or rapid_loads >= 16:
            self.loop = True

    def check_page(self):
        if self.blocked:
            raise self.blocked
        if not self.listing.is_closed() and not on_site(self.listing.url, self.site):
            if self.listing.url == "about:blank":
                raise IncompleteReview([], "官网搜索页已跳转到空白页，复查已停止", outcome="blank_page")
            if self.listing.url.startswith("chrome-error:"):
                raise IncompleteReview([], "浏览器报告官网页面连接失败", outcome="connection_failed")
            raise NeedsSetup("搜索页已离开平台官网，复查已停止")


def review_start_page(ctx, url, site):
    if not on_site(url, site):
        raise NeedsSetup("复查起始地址必须属于所选平台官网")
    # Create one window already pointed at the search. Supplying the URL on
    # Chrome's command line can also reopen the previous review's tabs.
    channel = ctx.browser.new_browser_cdp_session()
    try:
        with ctx.expect_page(timeout=25000) as opened:
            channel.send("Target.createTarget", {"url": url, "newWindow": True})
        page = opened.value
        page.wait_for_url(lambda address: address != "about:blank", wait_until="commit", timeout=25000)
        if page.url.startswith("chrome-error:"):
            raise IncompleteReview([], "浏览器报告官网页面连接失败", outcome="connection_failed")
        if not on_site(page.url, site):
            raise NeedsSetup("浏览器未能到达官网搜索页，请在巡查记录中打开官网搜索页核对")
        return page
    except BrowserError as exc:
        raise NeedsSetup("官网搜索窗口未能打开或已关闭，请在巡查记录中打开官网搜索页核对") from exc
    finally:
        channel.detach()


def review_candidates(query, candidates, limit=8):
    # Preserve source order on ties, then spend the detail budget on stronger matches first.
    tokens = list(dict.fromkeys(models.terms(query) + query.split()))

    def relevance(entry):
        card = entry[1]
        title = card["title"]
        summary = models.role_description(card.get("summary") or card.get("description", ""))
        title_hits = sum(models.keyword_hit(t, title) for t in tokens)
        coverage = sum(models.keyword_hit(t, title + " " + summary) for t in tokens)
        return coverage, title_hits

    return sorted(candidates.items(), key=relevance, reverse=True)[:limit]


def public_page(url, site):
    seen = set()

    def allow_public_url(target):
        # fetch calls this before every request, including redirect destinations.
        reason = access_state(target)
        if reason:
            raise access_error(reason)
        if not on_site(target, site):
            return False
        address = target.split("#", 1)[0]
        if address in seen:
            raise VerificationRequired(reason="公开页面出现循环跳转，已暂停平台直连", outcome="redirect_loop")
        seen.add(address)
        return True

    try:
        raw, final = sources.fetch(url, limit=3_000_000, allow_url=allow_public_url)
    except sources.RedirectLimit as exc:
        raise VerificationRequired(reason="公开页面连续跳转，已暂停平台直连", outcome="redirect_loop") from exc
    except sources.SourceHTTPError as exc:
        if exc.status in (401, 403, 429):
            raise VerificationRequired(reason=f"公开页面限制访问（HTTP {exc.status}），已暂停平台直连", outcome="http_restriction") from exc
        raise
    except requests.RequestException as exc:
        raise IncompleteReview([], "平台官网连接失败", outcome="connection_failed") from exc
    doc = BeautifulSoup(raw, "html.parser")
    title = doc.title.get_text(strip=True) if doc.title else ""
    reason = access_state(final, title, doc.get_text(" ", strip=True))
    if reason:
        raise access_error(reason)
    return doc, final


def public_discover(key, query):
    site = PLATFORMS[key]
    doc, listing_url = public_page(search_url(key, query), site)
    candidates = {}
    verified = {}
    postings, _ = sources.structured_jobs(doc, listing_url, key)
    for job in postings:
        path = urlsplit(job["url"]).path
        if on_site(job["url"], site) and re.fullmatch(ROLE_PATHS[key], path):
            verified[path] = {**job, "external_id": path}
            candidates[path] = job
    for link in doc.select(LIST_SELECTORS[key]):
        url = urljoin(listing_url, link.get("href", ""))
        path = urlsplit(url).path
        title = link.get("title") or link.get_text(" ", strip=True)
        if title and on_site(url, site) and re.fullmatch(ROLE_PATHS[key], path):
            candidates.setdefault(path, dict(url=url, title=title, company="", location="", summary=title))
    if not candidates:
        raise IncompleteReview([], "公开搜索页未提供可读岗位，可使用搜索索引或企业官网", outcome="no_readable_cards")
    cards = review_candidates(query, candidates, limit=None)
    found = []
    missed = consecutive = 0
    for index, (path, card) in enumerate(cards):
        if path in verified:
            found.append(verified[path])
            continue
        if index >= 8:
            found.extend(reviewed_jobs(key, cards[index:], verified))
            break
        details = {}
        try:
            doc, final = public_page(card["url"], site)
            consecutive = 0
            if urlsplit(final).path == path:
                if doc.title and any(term in doc.title.get_text() for term in CLOSED):
                    continue
                postings, has_metadata = sources.structured_jobs(doc, final, key)
                if has_metadata:
                    matching = [j for j in postings if on_site(j["url"], site) and urlsplit(j["url"]).path == path]
                    if len(matching) == 1:
                        found.append({**matching[0], "external_id": path})
                    continue
                for field, selector in (("title", JOB_TITLE), ("description", site["jd"]),
                                        ("employer", site["company"]), ("city", JOB_CITY)):
                    node = doc.select_one(selector)
                    if node:
                        details[field] = node.get_text(" ", strip=True)
        except VerificationRequired as exc:
            found.extend(reviewed_jobs(key, cards[index:], verified))
            raise VerificationRequired(found, str(exc), outcome=exc.outcome, phase="公开岗位详情") from exc
        except (ValueError, OSError, requests.RequestException) as exc:
            missed += 1
            consecutive += 1
            if consecutive >= 2:
                found.extend(reviewed_jobs(key, cards[index:], verified))
                raise IncompleteReview(found, "连续两个公开详情读取失败，已停止并保留岗位卡片", phase="公开岗位详情",
                                       outcome="connection_failed" if isinstance(exc, requests.RequestException) or getattr(exc, "outcome", "") == "connection_failed" else "incomplete")
        job = card_job(key, card, path, **details)
        if not details.get("title"):
            job["partial"] = True
        found.append(job)
    if missed:
        raise IncompleteReview(found, f"{missed} 个公开详情读取失败，对应岗位已保留为卡片")
    return found


def discover(root, key, query, *, interactive=False, review_window=None, diagnostics=None):
    if not interactive:
        return public_discover(key, query)
    site = PLATFORMS[key]
    url = search_url(key, query)
    verified = {}
    with session(root, key, interactive=True) as ctx:
        navigation = ReviewNavigation(ctx, site, diagnostics)
        try:
            listing = review_start_page(ctx, url, site)
        except NeedsSetup as exc:
            if navigation.blocked:
                raise navigation.blocked from exc
            raise
        navigation.listing = listing
        try:
            cards = search_cards(listing, key, query, navigation, verified)
            if cards and review_window:
                with review_window(len(cards)) as finished:
                    try:
                        read_open_jobs(ctx, key, cards, navigation, verified, finished)
                    except IncompleteReview as exc:
                        exc.phase = "读取当前窗口"
                        raise
        finally:
            if not listing.is_closed():
                try:
                    listing.close()
                except BrowserError:
                    pass  # The user can close the window at the same time.
    return reviewed_jobs(key, cards, verified)


def reviewed_jobs(key, cards, verified):
    return [
        verified.get(path) or card_job(key, card, path)
        for path, card in cards if card["title"] or path in verified
    ]


def read_open_jobs(ctx, key, cards, navigation, verified, finished):
    # Only observe documents the user has opened. Bind all text to its URL in
    # one DOM snapshot so an in-flight navigation cannot mix two positions.
    site = PLATFORMS[key]
    by_path = dict(cards)
    selectors = {"title": JOB_TITLE, "employer": site["company"], "city": JOB_CITY, "description": site["jd"]}
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if navigation.blocked:
            navigation.blocked.jobs = reviewed_jobs(key, cards, verified)
            raise navigation.blocked
        pages = [page for page in ctx.pages if not page.is_closed()]
        if not pages:
            break
        if navigation.loop:
            raise VerificationRequired(reviewed_jobs(key, cards, verified), "官网搜索页持续重复加载，复查已停止，已读取的岗位已保留", outcome="redirect_loop")
        if not navigation.listing.is_closed():
            if navigation.listing.url == "about:blank":
                raise IncompleteReview(reviewed_jobs(key, cards, verified), "官网搜索页已转为空白页，已保留卡片", outcome="blank_page")
            if navigation.listing.url.startswith("chrome-error:"):
                raise IncompleteReview(reviewed_jobs(key, cards, verified), "浏览器报告官网连接失败，已保留卡片", outcome="connection_failed")
        for page in pages:
            path = urlsplit(page.url).path
            if not on_site(page.url, site):
                continue
            try:
                state = page_state(page)
                if state:
                    raise access_error(state, reviewed_jobs(key, cards, verified))
                if path not in by_path or path in verified:
                    continue
                data = page.evaluate("""selectors => {
                    const fields = {};
                    for (const [name, selector] of Object.entries(selectors)) {
                        const el = [...document.querySelectorAll(selector)].find(node => node.getClientRects().length);
                        fields[name] = el?.innerText.trim() || '';
                    }
                    return {url: location.href, heading: document.title, fields};
                }""", selectors)
            except BrowserError:
                continue
            if (
                on_site(data["url"], site)
                and urlsplit(data["url"]).path == path
                and data["fields"]["description"]
                and not any(word in data["heading"] for word in CLOSED)
            ):
                job = card_job(key, by_path[path], path, **data["fields"])
                if not job.get("partial"):
                    verified[path] = job
        if finished():
            break
        try:
            ctx.wait_for_event("page", timeout=300)
        except BrowserTimeout:
            pass
        except BrowserError:
            break


def search_cards(listing, key, query, navigation, verified):
    site = PLATFORMS[key]
    deadline = time.monotonic() + 180
    try:
        while True:
            navigation.check_page()
            remaining = max(1, (deadline - time.monotonic()) * 1000)
            try:
                snapshot = listing.evaluate(CARD_SNAPSHOT, {"selector": LIST_SELECTORS[key], "key": key})
                reason = access_state(snapshot["url"], snapshot["title"], snapshot["body"])
                if reason:
                    raise access_error(reason)
                if on_site(snapshot["url"], site):
                    postings, _ = sources.jobs_from_scripts(snapshot["scripts"], snapshot["url"], key)
                    for job in postings:
                        path = urlsplit(job["url"]).path
                        if on_site(job["url"], site) and re.fullmatch(ROLE_PATHS[key], path):
                            verified[path] = {**job, "external_id": path}
                    cards = snapshot["cards"]
                    if cards or verified:
                        break
            except BrowserError as exc:
                if "execution context was destroyed" not in str(exc).lower():
                    raise
            if navigation.loop:
                raise VerificationRequired(
                    reason="官网搜索页连续加载仍未显示岗位，已停止本次反复跳转", outcome="redirect_loop"
                )
            if time.monotonic() >= deadline:
                raise BrowserTimeout("等待搜索结果超时")
            listing.wait_for_timeout(min(250, remaining))
    except BrowserError:
        if listing.is_closed():
            if navigation.blocked:
                raise navigation.blocked
            raise NeedsSetup("搜索窗口已关闭，本次读取已结束")
        reason = page_state(listing)
        if reason == VERIFY:
            raise VerificationRequired()
        if reason:
            raise NeedsSetup(reason + "，请用「可见窗口复查」处理后再巡查")
        body = listing.locator("body").inner_text()
        if any(
            msg in body
            for msg in ("暂时没有合适的职位", "暂无符合条件的职位", "没有找到相关职位", "暂无搜索结果")
        ):
            return []
        raise NeedsSetup("搜索页没有可识别的职位卡片，请用「可见窗口复查」核对搜索词及页面状态")
    unique = dict(verified)
    for c in cards:
        path = urlsplit(c["url"]).path
        if on_site(c["url"], site) and re.fullmatch(ROLE_PATHS[key], path):
            unique.setdefault(path, c)
    if not unique:
        raise NeedsSetup("搜索页面中没有具体职位链接")
    return review_candidates(query, unique, limit=None)


CARD_SNAPSHOT = r"""({selector, key}) => {
    const root = key === 'liepin' && document.querySelector('.job-list-box') || document;
    const cards = Array.from(root.querySelectorAll(selector)).filter(a => a.getClientRects().length).map(a => {
        const card = a.closest('.job-card-wrapper,.job-card-box,.job-card-pc-container,.joblist-box__item,li') || a;
        const text = q => card.querySelector(q)?.innerText.trim() || '';
        return {url: a.href, title: text('.job-name,.job-title,.jobinfo__name,.job-title-box .ellipsis-1')
                || a.getAttribute('title') || a.innerText.trim().split('\n')[0] || '',
            company: text('.company-name,.company-info .company-name,.companyinfo__name'),
            location: text('.job-area,.job-dq-box,.jobinfo__other-info .jobinfo__other-info-item'),
            summary: card.innerText.trim().slice(0,2000)};
    });
    const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'), s => s.textContent);
    return {url: location.href, title: document.title, body: document.body?.innerText || '', scripts,
            cards: cards.some(c => c.title) ? cards : []};
}"""


CLOSED = ("已暂停招聘", "已结束招聘", "职位已关闭", "已下线", "职位已过期")


def card_job(key, card, path, *, title="", employer="", city="", description=""):
    job = item(
        key, path, employer or card["company"] or "雇主待核实", title or card["title"], card["url"],
        city or card["location"], description or card["summary"],
    )
    if not description or not (employer or card["company"]):
        job["partial"] = True
    if card.get("indexed"):
        job["indexed"] = True
    return job


def indexed_jobs(key, query, base, *, stats=None):
    from .searxng import results

    site = INDEX_SITES[key]
    jobs = {}
    prefix = site["domain"] + site["link"]
    # Indexes can ignore the query or return a related link with a different role.
    # Require visible evidence for the search terms before saving an unverified lead.
    requested = [w for w in query.split() if w not in ("招聘", "职位", "岗位")]
    if stats is None:
        stats = {}
    stats.update(returned=0, official_details=0)
    for path_filter in ((prefix.rstrip("/"),) if site["link"] == "/" else (prefix.rstrip("/"), prefix)):
        for row in results(base, f"site:{path_filter} {query} 招聘", limit=50):
            stats["returned"] += 1
            url = row["url"]
            identity = listing_identity(url)
            if identity and identity[0] == key:
                stats["official_details"] += 1
            if (
                not identity or identity[0] != key
                or not row["title"]
                or any(word in row["title"] for word in CLOSED)
            ):
                continue
            path = identity[1]
            evidence = row["title"] + " " + models.role_description(row["content"])
            if requested and not all(models.keyword_hit(w, evidence) for w in requested):
                continue
            job = item(key, path, "雇主待核实", row["title"], url.split("?", 1)[0], "", row["content"] or row["title"])
            jobs[path] = {**job, "partial": True, "indexed": True}
        if jobs:
            break
    return [job for _, job in review_candidates(query, jobs, limit=None)]


# Chat actions require a unique, stable conversation identity exposed by the UI.
def thread_id(page):
    for key in ("chatId", "friendId", "geekId", "conversationId", "bossId"):
        values = parse_qs(urlsplit(page.url).query).get(key)
        if values and values[0]:
            return key + ":" + values[0]
    active = page.locator(
        ".user-list .active, .chat-list .active, [aria-selected=true][data-conversation-id]"
    )
    for el in active.all():
        if el.is_visible():
            for attr in ("data-conversation-id", "data-friend-id", "data-id", "data-uid"):
                val = el.get_attribute(attr)
                if val:
                    return attr + ":" + val
    return ""


def open_chat(ctx, job, existing):
    site = PLATFORMS[job["source"]]
    page = ctx.new_page()
    visit(page, job["url"], site)
    if page_state(page):
        raise VerificationRequired(reason="已保存的账号需要登录或验证，此平台的自动访问已暂停")
    page.locator(site["jd"]).first.wait_for(timeout=8000)
    title = text_of(page, JOB_TITLE)
    company = text_of(page, site["company"])
    if title != job["title"] or company != job["company"]:
        raise NeedsSetup("职位或公司与已保存的岗位不一致")
    pattern = r"^(继续沟通|继续聊|进入沟通)$" if existing else r"^(立即沟通|聊一聊|继续沟通|立即开聊)$"
    controls = page.get_by_text(re.compile(pattern))
    control = visible(controls)
    if not control:
        raise NeedsSetup("当前页面没有可识别的沟通入口，请核对登录状态")
    target = control.get_attribute("href") or ""
    control.click()
    page.wait_for_timeout(1000)
    editor = None
    for tab in reversed(ctx.pages):
        if not on_site(tab.url, site):
            continue
        editor = visible(
            tab.locator(
                "#chat-input, .chat-input [contenteditable=true], .chat-input textarea, textarea.chat-input"
            )
        )
        if editor:
            page = tab
            break
    ident = thread_id(page)
    if not ident or not editor or (existing and existing != ident):
        raise ValueError("会话标识无法核实，请在平台核对沟通状态")
    if not existing:
        key, _, val = ident.partition(":")
        peer_link = val in parse_qs(urlsplit(target).query).get(key, [])
        header = text_of(page, ".chat-job-info, .chat-header .job-info, [data-chat-job]")
        job_link = any(
            urlsplit(link).path == urlsplit(job["url"]).path
            for link in page.locator(".chat-header a[href], .chat-job-info a[href]").evaluate_all(
                "(a)=>a.map(e=>e.href)"
            )
        )
        if not (peer_link or job_link or (title in header and company in header)):
            raise ValueError("未能在当前会话核实岗位与收件人，请在平台检查")
    return page, editor, ident


OUTGOING = ".message-item.item-myself, .message-item.is-self, .message-item.me, [data-direction=outgoing]"
INCOMING = ".message-item.item-friend, .message-item.is-other, .message-item.other, [data-direction=incoming]"


def outbound(page, text):
    return page.locator(OUTGOING).filter(has_text=text)


def receipt(page, text, index):
    node = outbound(page, text).nth(index)
    node.wait_for(state="visible", timeout=7500)
    page.wait_for_function(
        """el => {
        const status = el.getAttribute('data-status') || el.getAttribute('data-message-status') || '';
        const label = el.querySelector('.message-status,.status,[data-status]')?.textContent || '';
        return /^(sent|success|delivered|read)$/.test(status) || /^(已发送|已送达|已读|未读)$/.test(label.trim());
    }""",
        arg=node.element_handle(),
        timeout=6000,
    )


def greet(root, job, greeting):
    with session(root, job["source"], require_profile=True) as ctx:
        page, editor, ident = open_chat(ctx, job, None)
        before = outbound(page, greeting).count()
        if not before:
            editor.fill(greeting)
            button = visible(page.get_by_text("发送", exact=True))
            if not button:
                raise ValueError("未找到发送按钮，请检查平台会话")
            button.click()
        receipt(page, greeting, 0)
        return ident


def incoming(root, job, ident):
    with session(root, job["source"], require_profile=True) as ctx:
        page, _, _ = open_chat(ctx, job, ident)
        events = []
        for node in page.locator(INCOMING).all():
            msg = node.inner_text().strip()
            if re.search(r"已撤回|已失效|已过期|已拒绝", msg):
                continue
            # Only structured incoming request cards qualify; free text never releases a file.
            card = node.locator(".card-wrap,.message-card,.card-message,[data-request-type]")
            if not card.count():
                continue
            kind = card.first.get_attribute("data-request-type") or ""
            if kind not in ("resume_request", "contact_request"):
                if re.search(r"(?:想要|希望收到|请求发送|获取|接收|查看|发一份).{0,10}(?:附件)?简历", msg):
                    kind = "resume_request"
                elif re.search(r"交换.{0,3}(?:微信|电话|联系方式)|请求.{0,5}联系方式", msg):
                    kind = "contact_request"
                else:
                    continue
            eid = (
                node.get_attribute("data-mid")
                or node.get_attribute("data-message-id")
                or hashlib.sha256(msg.encode()).hexdigest()
            )
            events.append((eid, kind))
        return events


def send_resume(root, job, ident, resume):
    with session(root, job["source"], require_profile=True) as ctx:
        page, _, _ = open_chat(ctx, job, ident)
        files = page.locator('input[type=file][accept*="pdf" i],input[type=file][accept*="doc" i]')
        if files.count() != 1:
            control = visible(page.get_by_text(re.compile(r"^(上传附件简历|发送附件|上传简历)$")))
            if control:
                control.click()
        if files.count() != 1:
            raise NeedsSetup("未找到唯一的附件上传控件，请在原会话发送")
        before = outbound(page, resume["name"]).count()
        files.set_input_files(
            {"name": resume["name"], "mimeType": "application/octet-stream", "buffer": resume["body"]}
        )
        confirm = visible(page.get_by_role("button", name=re.compile(r"^(确认发送|发送附件)$")))
        if confirm:
            confirm.click()
        receipt(page, resume["name"], before)
        return ident
