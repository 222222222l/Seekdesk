"""Optional local SearXNG search provider and its Compose installation lifecycle."""

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

import requests

from .storage import digest

KEY = "plugins.searxng"
TEMPLATE = Path(__file__).resolve().parents[1] / "plugins" / "searxng" / "compose.yaml"
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
LOCK = threading.Lock()
DEFAULT = dict(mode="", project="", endpoint="", enabled=False, note="尚未安装", checked=0)


def state(store):
    cfg = {**DEFAULT, **store.get(KEY, {})}
    existing = Path.home() / "searxng-dsh"
    return {
        **cfg,
        "busy": LOCK.locked(),
        "suggested_project": str(existing) if (existing / "docker-compose.yml").is_file() else "",
    }


def endpoint(value):
    if not isinstance(value, str):
        raise ValueError("请输入本机 SearXNG 地址")
    u = urlsplit(value.strip())
    if (
        u.scheme not in ("http", "https")
        or u.hostname not in ("127.0.0.1", "::1", "localhost")
        or u.username is not None
        or u.password is not None
        or u.query
        or u.fragment
    ):
        raise ValueError("实例地址须为本机 HTTP 或 HTTPS 地址，例如 http://127.0.0.1:8888")
    if u.port == 0:
        raise ValueError("端口必须大于 0")
    return u.geturl().rstrip("/")


def results(base, query, limit=6):
    with requests.Session() as client:
        client.trust_env = False
        try:
            with client.get(
                endpoint(base) + "/search",
                params={"q": query, "format": "json", "categories": "general"},
                timeout=(3, 30),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code == 403:
                    raise ValueError(
                        "SearXNG 拒绝 JSON 查询，请在 settings.yml 的 search.formats 中启用 json"
                    )
                if response.status_code != 200:
                    raise ValueError(f"SearXNG 返回 HTTP {response.status_code}")
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw) > 3_000_000:
                        raise ValueError("SearXNG 响应超过大小限制")
        except requests.RequestException as exc:
            raise ValueError("无法查询本地 SearXNG，请检查服务、端口和上游搜索超时") from exc
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("SearXNG 未返回 JSON，请核对地址及 search.formats") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("该地址未返回 SearXNG 搜索结果")
    links = []
    seen = set()
    for row in data["results"]:
        url = row.get("url") if isinstance(row, dict) else None
        if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in seen:
            seen.add(url)
            links.append(
                {
                    "url": url,
                    "title": str(row.get("title") or "")[:250],
                    "content": str(row.get("content") or "")[:2000],
                }
            )
        if len(links) == limit:
            break
    if not links and data.get("unresponsive_engines"):
        raise ValueError("本地 JSON 接口可用，但上游引擎未返回结果，请检查 SearXNG 网络或代理设置")
    return links


def search(base, query):
    return [row["url"] for row in results(base, query)]


def compose_file(project):
    if not isinstance(project, str) or not project.strip():
        raise ValueError("请选择含 Compose 文件的本地 SearXNG 项目目录")
    path = Path(project.strip()).expanduser()
    if not path.is_absolute() or str(path).startswith("\\\\"):
        raise ValueError("请填写本机项目的绝对路径")
    path = path.resolve()
    if path.is_file() and path.name in COMPOSE_FILES:
        return path
    for name in COMPOSE_FILES:
        if (path / name).is_file():
            return path / name
    raise ValueError("目录中未找到 compose.yaml 或 docker-compose.yml")


def prepare_managed(store, proxy=""):
    outgoing = ""
    if proxy:
        u = urlsplit(proxy)
        if (
            u.scheme not in ("http", "https", "socks5h")
            or not u.hostname
            or u.username is not None
            or u.password is not None
            or u.path not in ("", "/")
            or u.query
            or u.fragment
        ):
            raise ValueError("请填写容器可访问的代理地址和端口")
        outgoing = (
            "\noutgoing:\n  request_timeout: 8.0\n  proxies:\n    all://:\n      - "
            + json.dumps(proxy)
            + "\n"
        )
    root = store.root / "plugins" / "searxng"
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    settings = config / "settings.yml"
    if not settings.exists():
        settings.write_text(
            "use_default_settings: true\nserver:\n  secret_key: "
            + secrets.token_hex(32)
            + "\nsearch:\n  formats:\n    - html\n    - json\n"
            + outgoing,
            encoding="utf-8",
        )
    elif outgoing and outgoing.strip() not in settings.read_text(encoding="utf-8"):
        raise ValueError("检测到保留的 settings.yml，请先在该文件中更新 outgoing.proxies")
    manifest = root / "compose.yaml"
    if not manifest.exists():
        shutil.copyfile(TEMPLATE, manifest)
    return str(manifest.resolve())


def docker(args, timeout=45):
    executable = shutil.which("docker")
    if not executable:
        raise ValueError("未找到 Docker CLI，请安装 Docker Desktop 或连接已有实例")
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Docker 操作超时，请检查镜像下载或 Docker Desktop 状态后重试") from exc
    if result.returncode:
        detail = result.stderr.lower()
        if "port is already allocated" in detail or "address already in use" in detail:
            raise ValueError("SearXNG 映射端口被占用，请核对已有实例地址")
        if "no such service" in detail:
            raise ValueError("未找到名为 searxng 的 Compose 服务，可以通过实例地址接入")
        if any(s in detail for s in ("cannot connect", "failed to connect", "docker daemon", "npipe:")):
            raise ValueError("Docker 引擎未就绪，请启动 Docker Desktop 后重试")
        raise ValueError("Docker Compose 操作失败，请检查项目配置、镜像下载及容器名称冲突")
    return result.stdout.strip()


def compose(store, cfg, *args):
    host = os.environ.get("DOCKER_HOST") or docker(
        ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    )
    if not host.startswith(("unix://", "npipe://")) and urlsplit(host).hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise ValueError("当前 Docker 上下文连接远程主机，请切换至本机引擎")
    prefix = ["compose", "-f", str(compose_file(cfg["project"]))]
    if cfg["mode"] == "managed":
        prefix += ["-p", "seekdesk-search-" + digest(str(store.root.resolve()))[:12]]
    return docker([*prefix, *args], timeout=300)


def mapped_endpoint(store, cfg):
    address = compose(store, cfg, "port", "searxng", "8080").splitlines()
    if not address:
        raise ValueError("Compose 未映射容器 8080 端口，请填写本机实例地址")
    port = int(address[0].rsplit(":", 1)[1])
    return endpoint(f"http://127.0.0.1:{port}")


def probe(cfg, wait=False):
    # Wait for HTTP readiness separately. A broken upstream should not cause a series of slow searches.
    if wait:
        with requests.Session() as client:
            client.trust_env = False
            for _ in range(25):
                try:
                    res = client.get(cfg["endpoint"] + "/healthz", timeout=1, allow_redirects=False)
                    if res.status_code < 500:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.5)
    count = len(search(cfg["endpoint"], "招聘 careers"))
    return f"JSON 测试成功，取得 {count} 个结果链接"


def operate(store, action, data):
    if not LOCK.acquire(False):
        raise ValueError("插件正在执行操作，请稍后再试")
    try:
        cfg = {**DEFAULT, **store.get(KEY, {})}
        if action == "install":
            mode = data.get("mode")
            if cfg["mode"]:
                raise ValueError("已配置 SearXNG，请使用启动或测试按钮")
            if mode not in ("managed", "project", "url"):
                raise ValueError("请选择安装方式")
            override = endpoint(data["endpoint"]) if data.get("endpoint") else ""
            if mode == "url" and not override:
                raise ValueError("请填写正在运行的 SearXNG 地址")
            project = (
                prepare_managed(store, data.get("proxy", "").strip())
                if mode == "managed"
                else str(compose_file(data.get("project"))) if mode == "project" else ""
            )
            cfg.update(mode=mode, project=project, endpoint=override, override=override)
        elif not cfg["mode"]:
            raise ValueError("请先安装或接入 SearXNG")
        if action not in ("install", "start", "test", "enable", "disable", "stop", "remove"):
            raise ValueError("未知插件操作")
        cfg["note"] = "正在处理插件操作"
        store.put(KEY, cfg)
        try:
            if action in ("install", "start"):
                if cfg["mode"] != "url":
                    compose(store, cfg, "up", "-d", "--no-recreate", "searxng")
                    cfg["endpoint"] = cfg.get("override") or mapped_endpoint(store, cfg)
                cfg["note"] = probe(cfg, wait=True)
                cfg["enabled"] = True
            elif action in ("test", "enable"):
                cfg["note"] = probe(cfg)
                if action == "enable":
                    cfg["enabled"] = True
            elif action == "disable":
                cfg.update(enabled=False, note="已停用巡查搜索")
            elif action == "stop":
                if cfg["mode"] == "url":
                    raise ValueError("此实例由外部程序管理，可以停用巡查搜索")
                compose(store, cfg, "stop", "searxng")
                cfg.update(enabled=False, note="SearXNG 服务已停止")
            elif action == "remove":
                if cfg["mode"] == "managed":
                    compose(store, cfg, "rm", "--stop", "--force", "searxng")
                cfg = dict(DEFAULT)
        except Exception as exc:
            cfg["note"] = str(exc) if type(exc) is ValueError else f"操作失败（{type(exc).__name__}）"
            raise ValueError(cfg["note"]) from exc
        finally:
            cfg["checked"] = int(time.time())
            store.put(KEY, cfg)
        return cfg
    finally:
        LOCK.release()
