#!/usr/bin/env python3
"""poly-dispute-research: static reporter

Fetches Polymarket Gamma markets (closed=false), filters for UMA disputed markets with volume24hr > threshold,
keeps a local state of seen market URLs, and posts ONLY newly-seen markets to a Telegram channel.

Designed to run under systemd timer every 3 hours.

Env vars (preferred):
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required (private channel numeric id like -100...)
  LIMIT                default 500
  STATE_PATH           default ./state.json
  MAX_PER_MESSAGE      default 30

Notes:
- Writes state atomically (tmp + rename).
- If Telegram send fails, state is NOT updated.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, NamedTuple
import re


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
MARKET_BASE = "https://polymarket.com/market/"

BOOL_TRUE_VALUES = ("1", "true", "yes", "y", "on")


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


@dataclass
class Config:
    token: str
    chat_id: str
    limit: int
    state_path: str
    max_per_message: int
    debug: bool
    dry_run: bool


class DisputedMarket(NamedTuple):
    """A single disputed market: URL, display title, volumes, and tags."""

    url: str
    title: str
    vol_24h: float
    vol_1wk: float
    vol_1mo: float
    tags: List[str]


def _parse_bool_env(name: str, default: str = "0") -> bool:
    raw = (os.getenv(name) or default).strip().lower()
    return raw in BOOL_TRUE_VALUES


def getenv_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def load_config() -> Config:
    token = getenv_required("TELEGRAM_BOT_TOKEN")
    chat_id = getenv_required("TELEGRAM_CHAT_ID")

    limit = int(os.getenv("LIMIT", "500"))
    state_path = os.getenv("STATE_PATH", os.path.join(os.path.dirname(__file__), "state", "state.json"))
    max_per_message = int(os.getenv("MAX_PER_MESSAGE", "30"))
    debug = _parse_bool_env("DEBUG")
    dry_run = _parse_bool_env("DRY_RUN")

    if limit <= 0:
        raise RuntimeError("LIMIT must be > 0")
    if max_per_message <= 0:
        raise RuntimeError("MAX_PER_MESSAGE must be > 0")

    return Config(
        token=token,
        chat_id=chat_id,
        limit=limit,
        state_path=state_path,
        max_per_message=max_per_message,
        debug=debug,
        dry_run=dry_run,
    )


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------


def http_get_json(url: str, headers: Dict[str, str] | None = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers or {"accept": "application/json", "user-agent": "poly-dispute-research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


# -----------------------------------------------------------------------------
# Gamma API / market parsing
# -----------------------------------------------------------------------------


def extract_event_slug(m: Dict[str, Any]) -> str | None:
    evs = m.get("events")
    if isinstance(evs, list) and evs:
        e0 = evs[0]
        if isinstance(e0, dict):
            s = e0.get("slug")
            return s if isinstance(s, str) and s else None
    return None


def fetch_event_tags(event_slug: str) -> List[str]:
    url = f"{GAMMA_BASE}/events?{urllib.parse.urlencode({'slug': event_slug})}"
    data = http_get_json(url)
    if not (isinstance(data, list) and data):
        return []
    e = data[0]
    if not isinstance(e, dict):
        return []
    tags = e.get("tags")
    if not isinstance(tags, list):
        return []
    out: List[str] = []
    for t in tags:
        if not isinstance(t, dict):
            continue
        slug = t.get("slug")
        label = t.get("label")
        if isinstance(slug, str) and slug:
            out.append(slug)
        elif isinstance(label, str) and label:
            out.append(label)
    # dedup preserve order
    seen = set()
    dedup: List[str] = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        dedup.append(x)
    return dedup


def http_post_form(url: str, form: Dict[str, str], timeout: int = 30) -> Any:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/x-www-form-urlencoded", "user-agent": "poly-dispute-research/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason} for {url} :: {body[:500]}") from e


def is_disputed(m: Dict[str, Any]) -> bool:
    """Keep ONLY markets with exactly one 'disputed' in umaResolutionStatuses.

    Requirement: if umaResolutionStatuses contains more than 1 'disputed' token,
    we skip the market (e.g. 2nd/3rd dispute rounds are not interesting).

    Notes:
    - Gamma may return umaResolutionStatuses as a list (preferred) or other scalar.
    - We count case-insensitive occurrences of the word 'disputed'.
    """
    raw = m.get("umaResolutionStatuses")

    # Most common: list of statuses like ["Disputed", "Disputed", ...]
    if isinstance(raw, list):
        disputed_count = sum(1 for x in raw if isinstance(x, str) and x.strip().lower() == "disputed")
        return disputed_count == 1

    # Fallback: stringify and count tokens
    s = str(raw or "")
    disputed_count = len(re.findall(r"\bdisputed\b", s, flags=re.IGNORECASE))
    return disputed_count == 1


def volume_num(m: Dict[str, Any], key: str) -> float:
    try:
        return float(m.get(key) or 0)
    except Exception:
        return 0.0


def market_url(m: Dict[str, Any]) -> str | None:
    slug = m.get("slug")
    if not isinstance(slug, str) or not slug:
        return None
    return MARKET_BASE + slug


def market_title(m: Dict[str, Any]) -> str:
    q = m.get("question")
    if isinstance(q, str) and q.strip():
        return q.strip()
    slug = m.get("slug")
    if isinstance(slug, str) and slug:
        return slug.replace("-", " ").replace("_", " ").title()
    return "Market"


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fetch_all_disputed(cfg: Config) -> List[DisputedMarket]:
    out: List[DisputedMarket] = []
    offset = 0
    tags_cache: Dict[str, List[str]] = {}

    while True:
        params = {"closed": "false", "limit": str(cfg.limit), "offset": str(offset)}
        page = http_get_json(f"{GAMMA_BASE}/markets?{urllib.parse.urlencode(params)}")
        if not isinstance(page, list):
            raise RuntimeError("Gamma /markets returned non-list")
        if not page:
            break

        for m in page:
            if not isinstance(m, dict) or not is_disputed(m):
                continue
            url = market_url(m)
            if not url:
                continue
            event_slug = extract_event_slug(m)
            if event_slug and event_slug in tags_cache:
                tags = tags_cache[event_slug]
            elif event_slug:
                tags = fetch_event_tags(event_slug)
                tags_cache[event_slug] = tags
            else:
                tags = []
            out.append(
                DisputedMarket(
                    url=url,
                    title=market_title(m),
                    vol_24h=volume_num(m, "volume24hr"),
                    vol_1wk=volume_num(m, "volume1wk"),
                    vol_1mo=volume_num(m, "volume1mo"),
                    tags=tags,
                )
            )

        if len(page) < cfg.limit:
            break
        offset += cfg.limit

    seen: set[str] = set()
    dedup: List[DisputedMarket] = []
    for m in out:
        if m.url in seen:
            continue
        seen.add(m.url)
        dedup.append(m)
    return dedup


# -----------------------------------------------------------------------------
# State (persisted URLs + latest_run)
# -----------------------------------------------------------------------------


def load_state(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, str)]
        if isinstance(data, dict) and isinstance(data.get("urls"), list):
            return [x for x in data.get("urls") if isinstance(x, str)]
        return []
    except FileNotFoundError:
        return []


def atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# -----------------------------------------------------------------------------
# Formatting
# -----------------------------------------------------------------------------


def fmt_usd_k(n: float) -> str:
    absn = abs(n)
    if absn >= 1_000_000:
        return f"${n/1_000_000:.1f}m"
    if absn >= 1_000:
        return f"${n/1_000:.1f}k"
    if absn >= 1:
        return f"${n:.0f}"
    return f"${n:.2f}"


def chunked(xs: List[Any], n: int) -> Iterable[List[Any]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


# -----------------------------------------------------------------------------
# Telegram
# -----------------------------------------------------------------------------


def telegram_send_message(cfg: Config, text: str) -> None:
    if cfg.dry_run:
        # For manual runs / debugging: print would-be message and don't send anything.
        print("\n--- MESSAGE (dry-run) ---\n")
        print(text)
        print("\n--- /MESSAGE (dry-run) ---\n")
        return
    url = f"https://api.telegram.org/bot{cfg.token}/sendMessage"
    payload = {
        "chat_id": cfg.chat_id,
        "text": text,
        "disable_web_page_preview": "true",
        "parse_mode": "HTML",
    }
    r = http_post_form(url, payload)
    if not (isinstance(r, dict) and r.get("ok") is True):
        raise RuntimeError(f"Telegram send failed: {r}")


# -----------------------------------------------------------------------------
# Report flow
# -----------------------------------------------------------------------------


def _build_state_data(current: List[DisputedMarket], new_count: int) -> Dict[str, Any]:
    return {
        "urls": [m.url for m in current],
        "latest_run": {
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "new_count": new_count,
            "total_count": len(current),
        },
    }


def _write_state(cfg: Config, current: List[DisputedMarket], new_count: int) -> None:
    atomic_write_json(cfg.state_path, _build_state_data(current, new_count))


def _format_report_message(
    chunk: List[DisputedMarket],
    new_urls: set[str],
    is_continuation: bool,
) -> str:
    header = "Active Polymarket Events in Dispute (cont.)" if is_continuation else (
        "Active Polymarket Events in Dispute"
        "\n\nFilters: (UMA disputes = 1)"
    )
    lines = [header, ""]
    for m in chunk:
        prefix = "🟢 " if m.url in new_urls else ""
        link = f'<a href="{m.url}">{html_escape(m.title)}</a>'
        lines.append(f"{prefix}{link}")
        lines.append(f"vol: v24h:{fmt_usd_k(m.vol_24h)}, vW:{fmt_usd_k(m.vol_1wk)}, vM:{fmt_usd_k(m.vol_1mo)}")
        if m.tags:
            lines.append("tags: " + ", ".join(html_escape(t) for t in m.tags))
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    cfg = load_config()

    current = fetch_all_disputed(cfg)
    prev_urls = set(load_state(cfg.state_path))

    new_items = [m for m in current if m.url not in prev_urls]
    new_urls_set = {m.url for m in new_items}
    report_items = sorted(current, key=lambda m: (0 if m.url in new_urls_set else 1))

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    if cfg.debug:
        print(f"[dbg] {now} matched={len(current)} prev={len(prev_urls)} new={len(new_items)}")
        for m in report_items[:5]:
            flag = "NEW" if m.url in new_urls_set else "OLD"
            tag_s = ",".join(m.tags[:6]) + ("..." if len(m.tags) > 6 else "")
            print(f"[dbg] {flag} {m.url} v24h={fmt_usd_k(m.vol_24h)} tags=[{tag_s}]")

    if not new_items and not cfg.dry_run:
        _write_state(cfg, current, new_count=0)
        print(f"[ok] {now} no new items (current={len(current)}); state updated at {cfg.state_path}")
        return 0

    for idx, chunk in enumerate(chunked(report_items, cfg.max_per_message), start=1):
        text = _format_report_message(chunk, new_urls_set, is_continuation=(idx > 1))
        telegram_send_message(cfg, text)
        time.sleep(0.4)

    if cfg.dry_run:
        print(f"[ok] {now} dry-run: would send {len(new_items)} new items; state NOT updated")
        return 0

    _write_state(cfg, current, len(new_items))
    print(f"[ok] {now} sent {len(new_items)} new items; state updated at {cfg.state_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[fatal] {e}", file=sys.stderr)
        raise
