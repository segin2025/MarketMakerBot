from typing import Dict, List, Optional
import re
import requests
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import dateparser


HIGH_IMPACT_KEYWORDS = [
    "sec", "etf", "spot etf", "approval", "reject", "listing", "delist",
    "hack", "exploit", "breach", "outage", "halt", "suspend",
    "cpi", "inflation", "fomc", "rate hike", "rate cut", "fed",
    "binance", "coinbase", "kraken", "blackrock", "cme",
]

MEDIUM_IMPACT_KEYWORDS = [
    "upgrade", "downgrade", "airdrop", "partnership", "acquisition", "merger",
    "regulation", "law", "lawsuit", "fine", "sanction",
]


def _fetch_rss(url: str, timeout: float = 5.0) -> List[Dict[str, str]]:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items = []
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            desc = (it.findtext("description") or "").strip()
            pub_raw = it.findtext("pubDate") or it.findtext("updated") or ""
            pub_dt = dateparser.parse(pub_raw) if pub_raw else None
            items.append({
                "title": title,
                "summary": desc,
                "published": pub_dt.isoformat() if pub_dt else "",
            })
        return items
    except Exception:
        return []


def _recent(items: List[Dict[str, str]], lookback_min: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    now = datetime.now(timezone.utc)
    for it in items:
        ts = it.get("published")
        dt = dateparser.parse(ts) if ts else None
        if not dt:
            # Assume recent if no timestamp; be conservative
            out.append(it)
            continue
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        age_min = (now - dt).total_seconds() / 60.0
        if age_min <= lookback_min:
            out.append(it)
    return out


def _score_impact(text: str) -> str:
    t = text.lower()
    if any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in HIGH_IMPACT_KEYWORDS):
        return "high"
    if any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in MEDIUM_IMPACT_KEYWORDS):
        return "medium"
    return "low"


def resolve_news_context(mode: str = "off", lookback_min: int = 45) -> Dict:
    """Return a context dict to guide trading when impactful news is detected.

    mode:
      - off: never enable news mode
      - auto: detect from public RSS feeds
      - force: always enable as high impact
    """
    if mode not in ("off", "auto", "force"):
        mode = "off"

    if mode == "off":
        return {"active": False}

    if mode == "force":
        return {
            "active": True,
            "impact": "high",
            "force_relaxed": True,
            "fallback_on_trend": "relaxed",
            "risk_r_per_trade": 0.0030,  # 0.30%
            "leverage_cap": 3.0,
        }

    # auto mode
    feeds = [
        # General crypto news
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        # Binance announcements RSS (if available)
        "https://www.binance.com/en/support/announcement/c-48?navId=48",
    ]
    recent: List[Dict[str, str]] = []
    for u in feeds:
        recent.extend(_recent(_fetch_rss(u), lookback_min))

    if not recent:
        return {"active": False}

    best = "low"
    for it in recent:
        txt = f"{it.get('title','')} {it.get('summary','')}"
        imp = _score_impact(txt)
        if imp == "high":
            best = "high"
            break
        if imp == "medium":
            best = "medium"

    if best == "high":
        return {
            "active": True,
            "impact": "high",
            "force_relaxed": True,
            "fallback_on_trend": "relaxed",
            "risk_r_per_trade": 0.0030,
            "leverage_cap": 3.0,
        }
    if best == "medium":
        return {
            "active": True,
            "impact": "medium",
            "force_relaxed": True,
            "fallback_on_trend": "relaxed",
            "risk_r_per_trade": 0.0025,
            "leverage_cap": 3.0,
        }
    return {"active": False}


