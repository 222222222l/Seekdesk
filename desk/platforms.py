"""Use the candidate's ordinary visible website controls and isolated browser profiles."""

import base64
import hashlib
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import getproxies, proxy_bypass

import requests
from playwright.sync_api import Error as BrowserError, sync_playwright

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
_ROUTES = {}


class NeedsSetup(ValueError):
    """No external write has been attempted."""


VERIFY = "官网要求人工安全验证"
VERIFY_NOTE = "官网待验证，已暂停此平台的后台直连，请点「可见窗口复查」"


class VerificationRequired(NeedsSetup):
    def __init__(self, jobs=()):
        super().__init__(VERIFY_NOTE)
        self.jobs = jobs


def browser_route(key):
    """Use direct transport only for a broken local proxy, after checking public HTTPS."""
    site = PLATFORMS[key]
    url = site.get("login", site["home"])
    proxy = getproxies().get("https", "")
    if urlsplit(proxy).hostname not in {"localhost", "127.0.0.1", "::1"} or proxy_bypass(
        urlsplit(url).hostname
    ):
        return []
    stamp, saved_proxy, direct = _ROUTES.get(key, (0, "", False))
    if saved_proxy != proxy or time.monotonic() - stamp >= 300:
        direct = False
        with requests.Session() as probe:
            probe.trust_env = False
            try:
                with probe.get(
                    url, proxies={"https": proxy}, timeout=(3, 4), allow_redirects=False, stream=True
                ):
                    pass
            except (requests.ConnectionError, requests.Timeout) as exc:
                if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                    try:
                        with probe.get(url, timeout=(3, 6), allow_redirects=False, stream=True) as response:
                            direct = response.status_code < 400
                    except requests.RequestException:
                        pass
            except requests.RequestException:
                pass
        _ROUTES[key] = time.monotonic(), proxy, direct
    return ["--no-proxy-server"] if direct else []


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
def session(root, key, *, interactive=False):
    lock = LOCKS[key]
    if not lock.acquire(blocking=False):
        raise NeedsSetup("平台窗口正被使用，请关闭登录窗口后重试")
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(root / "profiles" / key),
                executable_path=browser_executable(pw),
                args=browser_route(key),
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
    finally:
        lock.release()


def login_window(root, key, status, search=None):
    # Hand the official URL to Chromium at startup. Human authentication runs in its
    # own browser process, using the same per-platform profile as subsequent patrols.
    site = PLATFORMS[key]
    lock = LOCKS[key]
    if not lock.acquire(blocking=False):
        raise NeedsSetup("该平台窗口正在使用，请切换到已打开的窗口")
    try:
        with sync_playwright() as pw:
            executable = browser_executable(pw)
        profile = (root / "profiles" / key).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            executable,
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
            *browser_route(key),
            search_url(key, search) if search else site.get("login", site["home"]),
        ]
        try:
            browser = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise NeedsSetup("未找到 Chromium，请使用启动工作台.cmd 安装浏览器") from exc
        status("已启动独立登录窗口，请在官网完成登录")
        if browser.wait() != 0:
            raise NeedsSetup("登录窗口异常退出，请重新打开")
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


def platform_term(query, location, local=False):
    # Location is a separate preference. Domestic platforms treat a multi-skill string as a literal query.
    words = [w for w in re.split(r"[\s,，、]+", query) if w and w != location]
    return (words[0] if local and words else " ".join(words)) or query.strip()


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
LIST_SELECTORS = {
    "boss": 'a[href*="/job_detail/"][href*=".html"]',
    "liepin": 'a[href*="/job/"][href*=".shtml"],a[href*="/a/"][href*=".shtml"]',
    "zhilian": 'a[href*="/jobdetail/"][href*=".htm"]',
}
JOB_TITLE = ".job-apply-container .job-title, h1, .job-name .name, .summary-plane__title"
JOB_CITY = ".job-apply-container .job-properties > span:first-child, .text-city, .job-address, .job-address-text, .summary-plane__info--location"


def page_state(page):
    if page.url == "about:blank":
        return "官网跳回空白页"
    path = urlsplit(page.url).path.lower()
    title = page.title().lower()
    if any(word in path for word in ("captcha", "/security", "/verify")) or any(
        word in title for word in ("security verification", "安全验证", "安全中心", "验证中心")
    ):
        return VERIFY
    if any(word in title for word in ("用户登录", "登录注册", "注册登录")):
        return "官网要求在当前浏览器会话登录"
    return ""


def discover(root, key, query, *, interactive=False):
    site = PLATFORMS[key]
    with session(root, key, interactive=interactive) as ctx:
        listing = ctx.new_page()
        visit(listing, search_url(key, query), site)
        initial_challenge = page_state(listing) == VERIFY
        if not interactive and initial_challenge:
            raise VerificationRequired()
        links = listing.locator(LIST_SELECTORS[key])
        try:
            links.first.wait_for(state="attached", timeout=180000 if interactive else 13000)
        except BrowserError:
            if listing.is_closed():
                if initial_challenge:
                    raise VerificationRequired()
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
        if key == "liepin" and listing.locator(".job-list-box").count():
            links = listing.locator(".job-list-box").locator(LIST_SELECTORS[key])
        cards = links.evaluate_all(
            r"""els=>els.filter(a=>a.getClientRects().length).map(a=>{
           const card=a.closest('.job-card-wrapper,.job-card-box,.job-card-pc-container,.joblist-box__item,li')||a;
           const text=q=>card.querySelector(q)?.innerText.trim()||'';
           return {url:a.href,title:text('.job-name,.job-title,.jobinfo__name,.job-title-box .ellipsis-1')||a.getAttribute('title')||a.innerText.trim().split('\n')[0]||'',
              company:text('.company-name,.company-info .company-name,.companyinfo__name'),
              location:text('.job-area,.job-dq-box,.jobinfo__other-info .jobinfo__other-info-item'),
              summary:card.innerText.trim().slice(0,2000)};
        })"""
        )
        unique = {}
        for c in cards:
            path = urlsplit(c["url"]).path
            if on_site(c["url"], site) and re.fullmatch(ROLE_PATHS[key], path):
                unique.setdefault(path, c)
        if not unique:
            raise NeedsSetup("搜索页面中没有具体职位链接")
        return collect_details(ctx, key, list(unique.items())[:8], interactive)


CLOSED = ("已暂停招聘", "已结束招聘", "职位已关闭", "已下线", "职位已过期")


def collect_details(ctx, key, cards, interactive=False):
    site = PLATFORMS[key]
    found = []
    detail = ctx.new_page()
    limit_reached = ""
    for path, c in cards:
        title = desc = employer = city = ""
        if not limit_reached:
            try:
                visit(detail, c["url"], site)
                if not interactive and page_state(detail) == VERIFY:
                    raise VerificationRequired()
                if any(word in detail.title() for word in CLOSED):
                    continue
                jd = detail.locator(site["jd"]).first
                try:
                    jd.wait_for(state="visible", timeout=5000)
                except BrowserError:
                    state = "" if detail.is_closed() else page_state(detail)
                    if not interactive or not state:
                        raise
                    detail.bring_to_front()
                    try:
                        jd.wait_for(state="visible", timeout=180000)
                    except BrowserError:
                        if state == VERIFY:
                            raise VerificationRequired()
                        raise
                state = page_state(detail)
                if state == VERIFY:
                    raise VerificationRequired()
                if urlsplit(detail.url).path == path and not state:
                    header = detail.title()
                    if any(w in header for w in CLOSED):
                        continue
                    title = text_of(detail, JOB_TITLE)
                    desc = text_of(detail, site["jd"])
                    employer = text_of(detail, site["company"])
                    city = text_of(detail, JOB_CITY)
            except VerificationRequired:
                limit_reached = VERIFY
            except (BrowserError, NeedsSetup):
                limit_reached = "详情窗口已关闭" if detail.is_closed() else page_state(detail)
        title = title or c["title"]
        employer = employer or c["company"]
        if not title:
            continue
        partial = not desc or not employer
        job = item(
            key, path, employer or "雇主待核实", title, c["url"], city or c["location"], desc or c["summary"]
        )
        if partial:
            job["partial"] = True
        if c.get("indexed"):
            job["indexed"] = True
        found.append(job)
    if limit_reached == VERIFY:
        raise VerificationRequired(found)
    if not found and not any(c.get("indexed") for _, c in cards):
        raise NeedsSetup(limit_reached or "已找到具体职位链接，但页面未提供可核实的岗位内容")
    return found


def indexed_jobs(key, query, base):
    from .searxng import results

    site = PLATFORMS[key]
    jobs = {}
    prefix = site["domain"] + site["link"]
    for path_filter in (prefix.rstrip("/"), prefix):
        for row in results(base, f"site:{path_filter} {query} 招聘", limit=50):
            url = row["url"]
            path = urlsplit(url).path
            if (
                not on_site(url, site)
                or not re.fullmatch(ROLE_PATHS[key], path)
                or not row["title"]
                or any(word in row["title"] for word in CLOSED)
            ):
                continue
            job = item(key, path, "雇主待核实", row["title"], url, "", row["content"] or row["title"])
            jobs[path] = {**job, "partial": True, "indexed": True}
            if len(jobs) >= 8:
                break
        if jobs:
            break
    return list(jobs.values())


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
    with session(root, job["source"]) as ctx:
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
    with session(root, job["source"]) as ctx:
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
    with session(root, job["source"]) as ctx:
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
