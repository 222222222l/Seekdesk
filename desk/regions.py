"""Classify the publishing source independently of a vacancy's work location."""

from urllib.parse import urlsplit

from .platforms import INDEX_SITES
from .sources import EMPLOYER_INDEXES


REGIONS = ("domestic", "overseas")


def source_region(cfg, source, url=""):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if source == "博世中国岗位" or source in INDEX_SITES or source in EMPLOYER_INDEXES or any(host == site[0] for site in EMPLOYER_INDEXES.values()):
        return "domestic"
    if source.startswith(("greenhouse:", "lever:")):
        return "overseas"
    if any(host == p["domain"] or host.endswith("." + p["domain"]) for p in INDEX_SITES.values()):
        return "domestic"
    if any(host == d or host.endswith("." + d) for d in ("greenhouse.io", "lever.co")):
        return "overseas"
    for region in REGIONS:
        for entry in cfg.get(region + "_sites", "").split():
            if host == urlsplit(entry).hostname:
                return region
    if host.endswith(".cn"):
        return "domestic"
    return ""


def includes(cfg, source, url=""):
    selected = cfg.get("source_regions", REGIONS)
    return len(selected) == 2 or source_region(cfg, source, url) in selected
