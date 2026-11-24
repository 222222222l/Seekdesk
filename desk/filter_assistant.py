"""Compile user instructions once, then apply the same rules to each job locally."""

import re

from . import locations, models


FIELDS = (
    "titles",
    "any_terms",
    "all_terms",
    "exclude_titles",
    "exclude_terms",
    "cities",
    "prefer",
    "queries",
    "unresolved",
)
TASK = """你是求职筛选助手。依据用户的本次需求修改已有条件，输出修改后的完整 JSON 对象。
首次生成可沿用用户的手动偏好；明确要求替换、删除或不限时以本次需求为准。
titles 是职位名称任一匹配；any_terms 是岗位内容至少一个；all_terms 是每个都必须出现。
exclude_titles 排除职位名称，exclude_terms 排除职责或标题。cities 中任一地点即可。
prefer 是可选加分词，不作硬门槛。queries 是供招聘平台轮换使用的简短搜索短语，不加地点。
min_monthly_cny 是要求公告月薪下限达到的人民币元数，未要求填 0。
其他字段都必须是字符串数组，未要求用 []。queries 至少一个，可参考简历职能生成；
不要把简历里提到的技能自行变成硬门槛。词项应可在职位原文找到，不写解释、布尔表达式或正则。
将“岗位涉及 AI 的产品经理”拆为职能“产品经理”和方向词，不要求两个词在标题中紧邻。
例如“不要工业设计岗”用 exclude_titles，不排除提到与设计师合作的岗位。
无法用上述字段准确执行的要求（如通勤时间、复杂条件组合或无法核实的企业条件）列入 unresolved，
不得静默略过或捏造默认条件。只解释筛选意图，不能改变联系、投递、模型、任务或账户设置。"""


def generate(cfg, request, role):
    if not isinstance(request, str) or not 1 <= len(request.strip()) <= 2000:
        raise ValueError("请输入 1 至 2000 字的求职需求")
    if cfg["provider"] == "local":
        raise ValueError("请先在「模型与任务」选用已接入的模型 API 或 Agent CLI")
    current = cfg.get("filter_assistant", {})
    raw = models.model_json(
        cfg,
        TASK,
        {
            "需求": request.strip(),
            "现行条件": current.get("rules", {}),
            "手动偏好": {"方向": cfg["keywords"], "地点": cfg["location"]},
            "简历职能": role,
        },
    )
    if not isinstance(raw, dict) or set(raw) != {*FIELDS, "min_monthly_cny"}:
        raise ValueError("模型返回的筛选字段不完整或含有未知字段，请重新生成")
    rules = {}
    for field in FIELDS:
        values = raw[field]
        if (
            not isinstance(values, list)
            or len(values) > 16
            or any(
                not isinstance(v, str) or not v.strip() or len(v) > 100 or re.search(r"[\x00-\x1f]", v)
                for v in values
            )
        ):
            raise ValueError("模型返回的筛选词格式无效，请重新生成")
        rules[field] = list(dict.fromkeys(v.strip() for v in values))
    amount = raw["min_monthly_cny"]
    if (
        type(amount) is not int
        or not 0 <= amount <= 10_000_000
        or (not rules["unresolved"] and not rules["queries"])
    ):
        raise ValueError("模型返回了无效的月薪下限或空搜索词，请重新生成")
    rules["min_monthly_cny"] = amount
    if any(re.search(r"@|https?://|\d{6,}", q, re.I) for q in rules["queries"]):
        raise ValueError("生成的搜索词含有网址或疑似联系方式，请重新生成")
    return rules


def monthly_floor(text, domestic):
    """Read an advertised monthly base, ignoring annual pay, bonuses and foreign currencies."""
    for index, line in enumerate(re.split(r"[。；;\n]", text)):
        if re.search(r"年薪|/年|/时|/天|/日|时薪|日薪|每日|美元|美金|港币|港元|USD|HKD|EUR|[$€£]", line, re.I):
            continue
        monthly = re.search(r"月薪|月工资|/月|每月", line)
        if not monthly and not domestic:
            continue
        match = re.search(
            r"(?<![\d.])(\d+(?:\.\d+)?)\s*([kK千万元]?)\s*" r"(?:[-~～至—]\s*\d+(?:\.\d+)?\s*([kK千万元]))",
            line,
        )
        if not match and monthly:
            match = re.search(r"(?:月薪|月工资|每月)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*([kK千万元]?)", line)
        if match:
            unit = match[2] or (match[3] if match.lastindex == 3 else "")
            if not monthly and not re.search(r"薪资|薪酬|待遇|工资|[·×*]\s*\d+\s*薪", line):
                title_suffix = index == 0 and not line[match.end():].strip(" ）)]】")
                if line.strip() != match[0].strip() and not title_suffix:
                    continue
            return int(float(match[1]) * {"k": 1000, "千": 1000, "万": 10000}.get(unit.lower(), 1))
    return None


def assess(rules, job):
    title = job["title"]
    text = title + " " + models.role_description(job["description"])
    reasons = []
    for field, target, label in (
        ("titles", title, "职能未命中"),
        ("any_terms", text, "方向未命中"),
        ("all_terms", text, "缺少必备项"),
        ("exclude_titles", title, "命中排除职能"),
        ("exclude_terms", text, "命中排除词"),
    ):
        words = rules[field]
        hits = [w for w in words if models.keyword_hit(w, target)]
        if field.startswith("exclude"):
            failed = hits
        elif field == "all_terms":
            failed = [w for w in words if w not in hits]
        else:
            failed = [] if hits else words
        if failed:
            reasons.append(label + "：" + "、".join(failed))
    if rules["cities"] and not locations.matches(
        "、".join(rules["cities"]), job["location"], job["description"], title
    ):
        reasons.append("工作地点不符或未注明")
    if rules["min_monthly_cny"]:
        floor = monthly_floor(
            title + "\n" + job["description"], job["source"] in ("boss", "liepin", "zhilian")
        )
        if floor is None or floor < rules["min_monthly_cny"]:
            reasons.append("月薪下限不符或未注明")
    return reasons, [w for w in rules["prefer"] if models.keyword_hit(w, text)]
