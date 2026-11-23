import html
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


def url_key(url):
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        raise ValueError("无效的网页地址")
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.startswith("utm_") and k not in ("ref", "source")
    ]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(query), ""))


def public_url(url):
    p = urlsplit(url_key(url))
    if p.port not in (None, 443, 80) or not all(
        ipaddress.ip_address(row[4][0]).is_global
        for row in socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    ):
        raise ValueError("仅支持公开网站地址")
    return url


def fetch(url, *, headers=None, payload=None, limit=18_000_000):
    for _ in range(5):
        public_url(url)
        with requests.request(
            "POST" if payload is not None else "GET",
            url,
            headers=headers,
            json=payload,
            timeout=(5, 16),
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.is_redirect:
                if headers or payload is not None:
                    raise ValueError("认证接口发生跳转")
                url = urljoin(url, response.headers["Location"])
                continue
            if not response.ok:
                raise ValueError(f"来源返回 HTTP {response.status_code}")
            data = bytearray()
            for part in response.iter_content(65536):
                data.extend(part)
                if len(data) > limit:
                    raise ValueError("来源响应过大")
            return bytes(data), url
    raise ValueError("来源跳转过多")


def plain(value):
    return BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)[:20000]


def item(source, ext, company, title, url, location, description):
    path = urlsplit(url_key(url))
    parts = path.path.strip("/").split("/")
    if source in ("企业官网", "搜索发现") and len(parts) >= 2:
        if path.hostname in ("jobs.lever.co", "jobs.eu.lever.co"):
            source, ext = "lever:" + parts[0], parts[1]
        elif (
            path.hostname in ("boards.greenhouse.io", "job-boards.greenhouse.io")
            and len(parts) >= 3
            and parts[1] == "jobs"
        ):
            source, ext = "greenhouse:" + parts[0], parts[2]
    return dict(
        source=source,
        external_id=str(ext),
        company=str(company)[:250],
        title=str(title)[:250],
        url=url_key(url),
        location=str(location or "")[:250],
        description=description[:20000],
    )


def board(name):
    kind, slug = name.split(":", 1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", slug):
        raise ValueError("无效的企业招聘板标识")
    if kind == "greenhouse":
        raw, _ = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        return [
            item(
                name,
                j["id"],
                slug,
                j["title"],
                j["absolute_url"],
                (j.get("location") or {}).get("name"),
                plain(j.get("content")),
            )
            for j in json.loads(raw)["jobs"]
        ]
    if kind == "lever":
        raw, _ = fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        return [
            item(
                name,
                j["id"],
                slug,
                j["text"],
                j["hostedUrl"],
                j.get("categories", {}).get("location"),
                plain(
                    " ".join(
                        [
                            j.get("descriptionPlain", "") or "",
                            *(str(v.get("content", "")) for v in j.get("lists", [])),
                            j.get("additionalPlain", "") or "",
                        ]
                    )
                ),
            )
            for j in json.loads(raw)
        ]
    raise ValueError("请选择 Greenhouse 或 Lever 招聘板")


def postings(obj):
    if isinstance(obj, list):
        for node in obj:
            yield from postings(node)
    elif isinstance(obj, dict):
        types = obj.get("@type", [])
        if "JobPosting" in (types if isinstance(types, list) else [types]):
            yield obj
        for val in obj.values():
            if isinstance(val, (dict, list)):
                yield from postings(val)


def page_jobs(url, source, follow=False):
    raw, final = fetch(url, limit=3_000_000)
    soup = BeautifulSoup(raw, "html.parser")
    result = []
    structured = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        for j in postings(data):
            structured = True
            expires = j.get("validThrough")
            if expires and str(expires)[:10] < datetime.now(timezone.utc).date().isoformat():
                continue
            org = j.get("hiringOrganization") or {}
            org = org[0] if isinstance(org, list) and org else org
            company = org.get("name") if isinstance(org, dict) else str(org)
            if not j.get("title") or not j.get("description") or not company:
                continue
            link = url_key(urljoin(final, j.get("url") or final))
            identifier = j.get("identifier")
            ext = identifier.get("value") if isinstance(identifier, dict) else identifier
            location = j.get("jobLocation") or {}
            location = location[0] if isinstance(location, list) and location else location
            addr = location.get("address", {}) if isinstance(location, dict) else {}
            city = addr.get("addressLocality", "") if isinstance(addr, dict) else str(addr)
            result.append(
                item(
                    source,
                    link + "#" + str(ext or j["title"]),
                    company,
                    j["title"],
                    link,
                    city,
                    plain(
                        j["description"]
                        + " "
                        + str(
                            j.get("applicationContact", {}).get("description", "")
                            if isinstance(j.get("applicationContact"), dict)
                            else ""
                        )
                    ),
                )
            )
    if not result and not structured:
        heading = soup.find("h1")
        article = soup.select_one("main, article, [role=main]") or soup.body
        if heading and article:
            title = heading.get_text(" ", strip=True)
            description = article.get_text("\n", strip=True)[:15000]
            has_requirements = re.search(
                r"岗位要求|任职要求|申请条件|qualifications|what you.ll bring|requirements", description, re.I
            )
            single_role = 0 < len(title) < 160 and not re.fullmatch(
                r"(?:careers|jobs|open positions|加入我们|招聘职位|社会招聘)", title, re.I
            )
            if (
                has_requirements
                and single_role
                and not re.search(r"(?:expired|职位已下线|停止招聘)", title, re.I)
            ):
                org = soup.find("meta", property="og:site_name")
                company = (org.get("content") if org else "") or urlsplit(final).hostname
                result.append(
                    item(source, url_key(final) + "#" + title, company, title, final, "", description)
                )
    if not result and follow:
        links = []
        employers = set()
        for a in soup.select("a[href]"):
            target = urljoin(final, a["href"])
            parsed = urlsplit(target)
            parts = parsed.path.strip("/").split("/")
            if parts[0] and parsed.hostname in (
                "jobs.lever.co",
                "boards.greenhouse.io",
                "job-boards.greenhouse.io",
            ):
                kind = "lever" if parsed.hostname == "jobs.lever.co" else "greenhouse"
                if re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]) and parts[0] != "embed":
                    employers.add(kind + ":" + parts[0])
            if (
                urlsplit(target).netloc == urlsplit(final).netloc
                and target.split("#")[0] != final
                and re.search(r"jobs?|careers?|recruit|职位|招聘|position", target + " " + a.get_text(), re.I)
                and target not in links
            ):
                links.append(target)
        failures = []
        for employer in sorted(employers)[:2]:
            try:
                result.extend(board(employer))
            except (ValueError, OSError, requests.RequestException) as exc:
                failures.append(exc)
        if not result:
            for target in links[:5]:
                try:
                    result.extend(page_jobs(target, source))
                except (ValueError, OSError, requests.RequestException) as exc:
                    failures.append(exc)
        if failures and not result:
            raise failures[0]
    return result


def search_links(engine, key, query):
    if engine == "SearXNG":
        from .searxng import search

        return search(key, query)
    if engine == "Brave":
        url = "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": 6})
        raw, _ = fetch(url, headers={"X-Subscription-Token": key})
        return [r["url"] for r in json.loads(raw).get("web", {}).get("results", [])]
    raw, _ = fetch(
        "https://api.tavily.com/search",
        headers={"Authorization": "Bearer " + key},
        payload={"query": query, "max_results": 6},
    )
    return [r["url"] for r in json.loads(raw)["results"]]


def prep_note(description):
    parts = re.split(r"(?<=[。！？；.!?;])\s*|\n+", description)
    relevant = [
        p.strip()[:350]
        for p in parts
        if re.search(
            r"portfolio|cover letter|work sample|please (?:attach|submit|include)|作品集|求职信|成绩单|请.{0,12}(?:附|提供|提交)",
            p,
            re.I,
        )
    ]
    if relevant:
        return (
            "岗位原文摘录："
            + " / ".join(relevant[:3])
            + "\n准备建议：逐项核对所需材料，选用与岗位职责直接相关的案例。"
        )
    return "准备建议：整理简历和相关项目案例。页面未识别出明确的额外材料要求，请打开岗位原文核对。"
