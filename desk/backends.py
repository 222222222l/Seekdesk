"""Provider catalogue, model discovery, and model API wire formats."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import quote, urlsplit

import requests

AGENTS = {
    "codex": "Codex CLI",
    "pi": "Pi CLI",
    "claude": "Claude Code",
    "gemini_cli": "Gemini CLI",
    "opencode": "OpenCode",
}
COMMANDS = {"gemini_cli": "gemini"}
PACKAGES = {
    "pi": ["@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent"],
    "codex": ["@openai/codex"],
    "claude": ["@anthropic-ai/claude-code"],
    "gemini_cli": ["@google/gemini-cli"],
    "opencode": ["opencode-ai"],
}
# The base is also the boundary for reusing a saved credential.
APIS = {
    "api": ("自定义兼容 API", "", "openai"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com", "openai"),
    "qwen": ("通义千问 · 百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai"),
    "kimi": ("Kimi · 月之暗面", "https://api.moonshot.cn/v1", "openai"),
    "siliconflow": ("硅基流动", "https://api.siliconflow.cn/v1", "openai"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai"),
    "zhipu": ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "openai"),
    "anthropic": ("Anthropic Claude", "https://api.anthropic.com/v1", "anthropic"),
    "gemini": ("Google Gemini API", "https://generativelanguage.googleapis.com/v1beta", "gemini"),
    "ollama": ("本地 Ollama", "http://127.0.0.1:11434", "ollama"),
    "lmstudio": ("本地 LM Studio", "http://127.0.0.1:1234/v1", "openai"),
}
IDS = {"local", *AGENTS, *APIS}


def cli_executable(provider):
    name = COMMANDS.get(provider, provider)
    if provider not in AGENTS:
        raise ValueError("未知 CLI 后端")
    if os.name != "nt":
        if shutil.which(name):
            return [shutil.which(name)]
    else:
        exe = shutil.which(name + ".exe")
        if exe:
            return [exe]
        cmd = shutil.which(name + ".cmd")
        if cmd:
            for package in PACKAGES[provider]:
                root = Path(cmd).parent / "node_modules" / package
                manifest = root / "package.json"
                if manifest.is_file():
                    entry = json.loads(manifest.read_text(encoding="utf-8")).get("bin", {})
                    script = entry.get(name) if isinstance(entry, dict) else entry
                    if script and (root / script).is_file():
                        return [shutil.which("node.exe") or "node", str(root / script)]
    raise ValueError(f"未检测到 {AGENTS[provider]}，请先安装并登录")


def catalog():
    items = {"local": {"label": "本地关键词匹配", "kind": "local"}}
    for key, label in AGENTS.items():
        try:
            installed = bool(cli_executable(key))
        except (ValueError, OSError):
            installed = False
        items[key] = {"label": label, "kind": "cli", "installed": installed}
    for key, (label, url, _) in APIS.items():
        items[key] = {"label": label, "kind": "api", "base": url}
    return items


def validate_base(value):
    u = urlsplit(value)
    if (
        not u.hostname
        or u.username is not None
        or u.password is not None
        or u.query
        or u.fragment
        or (
            u.scheme != "https"
            and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))
        )
    ):
        raise ValueError("请输入 HTTPS 基址，本机服务可使用 HTTP")
    return value.rstrip("/")


def request(cfg, path, *, params=None, body=None):
    base = validate_base(cfg["api_url"])
    style = APIS[cfg["provider"]][2]
    headers = {}
    key = cfg.get("api_key", "")
    if style == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif style == "gemini":
        if key:
            headers["x-goog-api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    try:
        with requests.Session() as client:
            if urlsplit(base).hostname in ("localhost", "127.0.0.1", "::1"):
                client.trust_env = False
            response = client.request(
                "GET" if body is None else "POST",
                base + path,
                headers=headers,
                params=params,
                json=body,
                timeout=(5, 18 if body is None else 90),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError(
                    f"{APIS[cfg['provider']][0]} 返回 HTTP {response.status_code}，请检查基址、密钥和模型权限"
                )
            return response.json()
    except requests.RequestException as exc:
        raise ValueError("模型服务连接或响应异常，请核对基址与网络") from exc


def api_models(cfg):
    validate_base(cfg["api_url"])
    key = cfg["provider"]
    style = APIS[key][2]
    models = []
    params = {"sub_type": "chat"} if key == "siliconflow" else {}
    path = "/api/tags" if style == "ollama" else "/models"
    qwen_workspace = (
        key == "qwen"
        and urlsplit(cfg["api_url"]).hostname.endswith(".maas.aliyuncs.com")
        and cfg["api_url"].rstrip("/").endswith("/compatible-mode/v1")
    )
    if qwen_workspace:
        cfg = {**cfg, "api_url": cfg["api_url"].rstrip("/").removesuffix("/compatible-mode/v1") + "/api/v1"}
        params = {"capabilities": "TG", "page_no": 1, "page_size": 100}
    for _ in range(30):
        data = request(cfg, path, params=params)
        if not isinstance(data, dict):
            raise ValueError("模型目录格式不正确")
        if qwen_workspace:
            output = data.get("output", {})
            rows = output.get("models", [])
            models.extend(r["model"] for r in rows if isinstance(r, dict) and isinstance(r.get("model"), str))
            if not rows or len(models) >= int(output.get("total", len(models))):
                break
            params["page_no"] += 1
            continue
        rows = data.get("models" if style in ("ollama", "gemini") else "data")
        if not isinstance(rows, list):
            raise ValueError("接口未提供模型目录，可手动输入模型 ID")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if style == "gemini" and "generateContent" not in row.get("supportedGenerationMethods", []):
                continue
            name = row.get("name" if style in ("ollama", "gemini") else "id")
            if isinstance(name, str) and name:
                models.append(name.removeprefix("models/") if style == "gemini" else name)
        cursor = None
        if style == "gemini" and data.get("nextPageToken"):
            cursor = {"pageToken": data["nextPageToken"]}
        if style == "anthropic" and data.get("has_more") and data.get("last_id"):
            cursor = {"after_id": data["last_id"]}
        if not cursor or cursor == params:
            break
        params = cursor
    return sorted(set(models))


def complete(cfg, prompt):
    style = APIS[cfg["provider"]][2]
    model = cfg["model"]
    msg = [{"role": "user", "content": prompt}]
    if style == "ollama":
        return request(cfg, "/api/chat", body={"model": model, "messages": msg, "stream": False})["message"][
            "content"
        ]
    if style == "anthropic":
        blocks = request(cfg, "/messages", body={"model": model, "messages": msg, "max_tokens": 4096})[
            "content"
        ]
        return "".join(b["text"] for b in blocks if b.get("type") == "text")
    if style == "gemini":
        name = quote(model.removeprefix("models/"), safe="")
        result = request(
            cfg, "/models/" + name + ":generateContent", body={"contents": [{"parts": [{"text": prompt}]}]}
        )
        return "".join(
            p.get("text", "") for p in result["candidates"][0]["content"]["parts"] if not p.get("thought")
        )
    return request(cfg, "/chat/completions", body={"model": model, "messages": msg})["choices"][0]["message"][
        "content"
    ]


def cli_models(key):
    exe = cli_executable(key)
    commands = {
        "codex": ["debug", "models", "--bundled"],
        "pi": [
            "--offline",
            "--no-extensions",
            "--no-mcp",
            "--no-skills",
            "--no-context-files",
            "--list-models",
        ],
        "opencode": ["models"],
        "gemini_cli": ["models", "list", "--output-format", "json"],
    }
    if key == "claude":
        return ["sonnet", "opus", "haiku"], "Claude 官方别名，可手填完整 ID；账号权限需测试"
    with tempfile.TemporaryDirectory() as folder:
        if key == "codex":
            try:
                live = subprocess.run(
                    exe + ["debug", "models"],
                    cwd=folder,
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                    errors="replace",
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if live.returncode == 0:
                    ids = [
                        r["slug"]
                        for r in json.loads(live.stdout)["models"]
                        if r.get("visibility", "list") == "list"
                    ]
                    if ids:
                        return ids, "当前 Codex 配置返回的模型目录"
            except (subprocess.TimeoutExpired, ValueError, KeyError):
                pass
        p = subprocess.run(
            exe + commands[key],
            cwd=folder,
            capture_output=True,
            text=True,
            encoding="utf8",
            errors="replace",
            timeout=35,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if p.returncode:
        raise ValueError("该 CLI 的目录命令不可用，请检查版本或手填模型 ID")
    if key == "codex":
        rows = json.loads(p.stdout)["models"]
        ids = [r["slug"] for r in rows if r.get("visibility", "list") == "list"]
        return ids, "本机 Codex 随附目录，账号权限需测试"
    if key == "gemini_cli":
        rows = json.loads(p.stdout)
        if isinstance(rows, dict):
            rows = rows.get("models", [])
        return [r.get("modelId") or r.get("id") for r in rows if isinstance(r, dict)], "Gemini CLI 本机目录"
    if key == "opencode":
        return [
            s.strip() for s in p.stdout.splitlines() if "/" in s and len(s.split()) == 1
        ], "OpenCode 提供商目录"
    ids = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if (
            len(parts) >= 6
            and parts[0].lower() != "provider"
            and parts[-1].lower() in ("yes", "no", "true", "false")
        ):
            ids.append(parts[0] + "/" + parts[1])
    return ids, "Pi 本机目录，模型标识包含提供商"
