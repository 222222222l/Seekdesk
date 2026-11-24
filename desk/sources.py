import html
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


# Only accept official, identifiable vacancy pages from search-engine results.
EMPLOYER_INDEXES = {
    "腾讯社招": ("careers.tencent.com", r"/jobdesc\.html", "postId"),
    "字节跳动社招": ("jobs.bytedance.com", r"/experienced/position/([0-9]+)/detail/?", ""),
    "网易招聘": ("hr.163.com", r"/job-detail\.html", "id"),
}


def employer_identity(address):
    try:
        url = urlsplit(address)
        if url.scheme != "https" or url.port not in (None, 443) or url.username or url.password:
            return None
    except ValueError:
        return None
    for name, (host, pattern, parameter) in EMPLOYER_INDEXES.items():
        if url.hostname != host:
            continue
        path = re.fullmatch(pattern, url.path)
        if not path:
            return None
        if not parameter:
            return name, path[1], f"https://{host}{url.path.rstrip('/')}"
        ids = [v for k, v in parse_qsl(url.query) if k == parameter]
        if len(ids) == 1 and re.fullmatch("[0-9]+", ids[0]):
            return name, ids[0], f"https://{host}{url.path}?{parameter}={ids[0]}"
    return None


def employer_index(name, query, base, *, stats=None):
    from . import models, searxng

    host = EMPLOYER_INDEXES[name][0]
    jobs = {}
    if stats is None:
        stats = {}
    stats.update(returned=0, official_details=0)
    for row in searxng.results(base, f"site:{host} {query} 招聘", limit=50):
        stats["returned"] += 1
        identity = employer_identity(row["url"])
        if identity and identity[0] == name:
            stats["official_details"] += 1
        title = row["title"]
        if not identity or identity[0] != name or not title or re.search("已下线|已关闭|已过期|停止招聘", title):
            continue
        _, post_id, canonical = identity
        summary = row["content"] or title
        if any(not models.keyword_hit(word, title + " " + summary) for word in query.split() if word != "招聘"):
            continue
        job = item(name, post_id, name.removesuffix("社招").removesuffix("招聘"), title, canonical, "", summary)
        jobs[post_id] = {**job, "partial": True, "indexed": True}
    return list(jobs.values())


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


class SourceHTTPError(ValueError):
    def __init__(self, status):
        super().__init__(f"来源返回 HTTP {status}")
        self.status = status


class RedirectLimit(ValueError):
    pass


class EmployerDetailsUnavailable(ValueError):
    def __init__(self, jobs):
        super().__init__("企业官网详情暂不可读，已保留本轮清单并停止读取后续详情")
        self.jobs = jobs


def fetch(url, *, headers=None, payload=None, limit=18_000_000, allow_url=None):
    for _ in range(5):
        if allow_url and not allow_url(url):
            raise ValueError("目标地址不在本轮来源范围内")
        public_url(url)
        with requests.request(
            "POST" if payload is not None else "GET",
            url,
            headers=headers,
            # Public vacancy reads must not pick up credentials from .netrc.
            auth=lambda request: request,
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
                raise SourceHTTPError(response.status_code)
            data = bytearray()
            for part in response.iter_content(65536):
                data.extend(part)
                if len(data) > limit:
                    raise ValueError("来源响应过大")
            return bytes(data), url
    raise RedirectLimit("来源跳转过多")


def plain(value):
    return BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)[:20000]


def item(source, ext, company, title, url, location, description):
    if source in ("企业官网", "搜索发现"):
        identity = employer_identity(url)
        if identity:
            source, ext, url = identity
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


def tencent_jobs(query):
    host = "https://careers.tencent.com/"

    def read(endpoint, **params):
        url = host + "tencentcareer/api/post/" + endpoint + "?" + urlencode(params)
        try:
            raw, _ = fetch(url, allow_url=lambda target: target.startswith(host), limit=2_000_000)
        except requests.RequestException as exc:
            raise ValueError("腾讯招聘官网连接失败，请检查本机网络。已保存的岗位可继续查看") from exc
        response = json.loads(raw)
        if not isinstance(response, dict) or response.get("Code") != 200 or not isinstance(response.get("Data"), dict):
            raise ValueError("腾讯社招未返回可用岗位数据")
        return response["Data"]

    data = read("Query", keyword=query, pageIndex=1, pageSize=8, language="zh-cn", area="cn")
    postings = data.get("Posts")
    if not isinstance(postings, list):
        raise ValueError("腾讯社招清单格式已变化")
    jobs = {}
    for row in postings:
        post_id = str(row.get("PostId", ""))
        title = row.get("RecruitPostName")
        if not post_id.isdecimal() or not title:
            continue
        job = item("腾讯社招", post_id, "腾讯", title, host + "jobdesc.html?postId=" + post_id,
                   row.get("LocationName"), plain(row.get("Responsibility")) or title)
        jobs[post_id] = {**job, "partial": True}
    if postings and not jobs:
        raise ValueError("腾讯社招清单缺少可识别的职位编号或名称")
    for post_id, job in jobs.items():
        try:
            detail = read("ByPostId", postId=post_id, language="zh-cn")
        except (ValueError, OSError, requests.RequestException) as exc:
            raise EmployerDetailsUnavailable(list(jobs.values())) from exc
        if str(detail.get("PostId", "")) != post_id:
            continue
        duties, requirements = plain(detail.get("Responsibility")), plain(detail.get("Requirement"))
        if duties and requirements:
            job.update(description=f"岗位职责\n{duties}\n任职要求\n{requirements}", partial=False)
    return list(jobs.values())


def bosch_china_jobs(query, offset=0, *, stats=None):
    from .models import keyword_hit

    endpoint = "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings"
    params = urlencode({"country": "cn", "q": query, "limit": 100, "offset": offset})

    def read(url):
        raw, _ = fetch(url, allow_url=lambda target: target.startswith(endpoint + "?") or target.startswith(endpoint + "/"),
                       limit=3_000_000)
        return json.loads(raw)

    data = read(endpoint + "?" + params)
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise ValueError("博世中国招聘清单格式已变化")
    count = len(data["content"])
    if stats is not None:
        stats["scanned"] = count
        total = data.get("totalFound")
        stats["next"] = offset + count if count and isinstance(total, int) and offset + count < total else 0
    terms = [word for word in query.split() if word not in ("岗位", "职位", "招聘")]
    jobs = {}
    for row in data["content"]:
        post_id = str(row.get("id", ""))
        location = row.get("location") or {}
        if not post_id.isdecimal() or not row.get("name") or str(location.get("country", "")).lower() != "cn":
            continue
        if not all(keyword_hit(word, row["name"] + " " + str(location.get("city") or "")) for word in terms):
            continue
        job = item("博世中国岗位", post_id, "博世", row["name"], "https://jobs.smartrecruiters.com/BoschGroup/" + post_id,
                   location.get("city"), row["name"])
        jobs[post_id] = {**job, "partial": True}
    for post_id, job in list(jobs.items())[:8]:
        try:
            detail = read(endpoint + "/" + post_id)
        except (ValueError, OSError, requests.RequestException) as exc:
            raise EmployerDetailsUnavailable(list(jobs.values())) from exc
        if str(detail.get("id")) != post_id or (detail.get("location") or {}).get("country", "").lower() != "cn":
            continue
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        duties = plain((sections.get("jobDescription") or {}).get("text"))
        requirements = plain((sections.get("qualifications") or {}).get("text"))
        url = str(detail.get("postingUrl") or "")
        if re.fullmatch(r"https://jobs\.smartrecruiters\.com/BoschGroup/" + post_id + r"(?:-[^/?#]*)?/?", url):
            job["url"] = url
        if duties and requirements:
            job.update(description=f"岗位职责\n{duties}\n任职要求\n{requirements}", partial=False)
    return list(jobs.values())


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


def structured_jobs(soup, final, source):
    return jobs_from_scripts((s.get_text() for s in soup.find_all("script", type="application/ld+json")), final, source)


def jobs_from_scripts(scripts, final, source):
    result = []
    structured = False
    for script in scripts:
        try:
            data = json.loads(script)
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
    return result, structured


def page_jobs(url, source, follow=False, *, allow_url=None):
    raw, final = fetch(url, limit=3_000_000, allow_url=allow_url)
    soup = BeautifulSoup(raw, "html.parser")
    result, structured = structured_jobs(soup, final, source)
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
            if allow_url and not allow_url(target):
                continue
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
                    result.extend(page_jobs(target, source, allow_url=allow_url))
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
