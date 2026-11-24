"""Common country/city equivalents used in domestic and international listings."""

import re

CITIES = {
    "北京": "beijing",
    "上海": "shanghai",
    "深圳": "shenzhen",
    "广州": "guangzhou",
    "杭州": "hangzhou",
    "苏州": "suzhou",
    "南京": "nanjing",
    "成都": "chengdu",
    "重庆": "chongqing",
    "武汉": "wuhan",
    "西安": "xi'an",
    "天津": "tianjin",
    "厦门": "xiamen",
    "福州": "fuzhou",
    "合肥": "hefei",
    "长沙": "changsha",
    "青岛": "qingdao",
    "宁波": "ningbo",
    "无锡": "wuxi",
    "东莞": "dongguan",
    "佛山": "foshan",
    "济南": "jinan",
    "大连": "dalian",
    "沈阳": "shenyang",
    "郑州": "zhengzhou",
    "昆明": "kunming",
    "珠海": "zhuhai",
    "南昌": "nanchang",
    "长春": "changchun",
    "石家庄": "shijiazhuang",
    "南宁": "nanning",
    "贵阳": "guiyang",
    "哈尔滨": "harbin",
    "太原": "taiyuan",
}
PROVINCES = "河北 河南 安徽 辽宁 吉林 黑龙江 江苏 浙江 福建 江西 山东 湖北 湖南 广东 海南 四川 贵州 云南 陕西 甘肃 青海 内蒙古 广西 西藏 宁夏 新疆".split()
COUNTRY = {"中国", "china", "中国大陆", "中国内地", "全国", "国内", "mainland china"}


def choices(preference):
    result = []
    for part in re.split(r"[,，、;；/|\n]+", preference):
        words = part.split()
        if words and all(w.removesuffix("市") in CITIES or w.casefold() in CITIES.values() for w in words):
            result.extend(words)
        elif part.strip():
            result.append(part.strip())
    return result


def matches(preference, location, description, title=""):
    requested = choices(preference)
    if not requested:
        return True
    # A structured work location takes precedence over offices mentioned in the description.
    text = location.strip()
    if not text:
        heading = title.lstrip("【[（(「 ")
        text = next((cn for cn in CITIES if heading.startswith(cn)), "")
        if not text:
            field = re.search(
                r"(?:工作地点|工作地址|办公地点|办公地址|work location|location)\s*[:：]\s*([^。；;\n]{1,80})",
                description,
                re.I,
            )
            text = field[1] if field else ""
    return any(_matches_place(q.casefold(), text.casefold()) for q in requested)


def _matches_place(q, text):
    if q in COUNTRY:
        return bool(
            re.search(r"中国|全国|国内|\bchina\b|\bcn\b", text)
            or any(c in text or en in text for c, en in CITIES.items())
            or any(v in text for v in PROVINCES)
        )
    q = q.removesuffix("市")
    aliases = next(((cn, en) for cn, en in CITIES.items() if q in (cn, en)), (q,))
    return any(k in text for k in aliases)
