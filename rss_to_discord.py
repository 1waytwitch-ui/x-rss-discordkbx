#!/usr/bin/env python3
"""Relais RSS -> webhook Discord."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

CONFIG_PATH = Path(__file__).with_name("config.json")
SEEN_PATH = Path(__file__).with_name("seen.json")
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "48"))

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def fetch(url: str, timeout: int = 30) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": "rss-to-discord/1.0 (+local relay)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def strip_html(html: str) -> str:
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", html).strip()


def parse_feed(raw: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(raw)
    items: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = text(item.find("title"))
        link = text(item.find("link"))
        guid = text(item.find("guid")) or link or title
        desc = text(item.find("description")) or text(item.find("content:encoded", NS))
        pub = text(item.find("pubDate")) or text(item.find("dc:date", NS))
        items.append({"id": guid, "title": title, "link": link, "description": strip_html(desc), "published": pub})
    return items


def item_key(item: dict[str, str]) -> str:
    raw = item.get("id") or item.get("link") or item.get("title") or ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_pinned(item: dict[str, str]) -> bool:
    title = (item.get("title") or "").lstrip()
    return title.lower().startswith("pinned:") or title.lower().startswith("épingl")


def is_too_old(item: dict[str, str], hours: int = MAX_AGE_HOURS) -> bool:
    raw = item.get("published") or ""
    if not raw:
        return False
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt > timedelta(hours=hours)
    except Exception:
        return False


def rewrite_x_link(url: str) -> str:
    if not url:
        return url
    url = re.sub(r"https?://(www\.)?(twitter|x)\.com/", "https://fxtwitter.com/", url)
    url = re.sub(r"https?://(www\.)?nitter\.[^/]+/", "https://fxtwitter.com/", url)
    return url


def post_discord(webhook: str, item: dict[str, str], username: str, avatar_url: str = "") -> None:
    link = rewrite_x_link(item["link"])
    content_parts = []
    if link:
        content_parts.append(link)
    elif item.get("title"):
        content_parts.append(item["title"])
    payload = {"username": username[:80] or "RSS", "content": "\n".join(content_parts)[:2000]}
    if avatar_url:
        payload["avatar_url"] = avatar_url
    data = json.dumps(payload).encode("utf-8")
    req = Request(webhook, data=data, headers={"Content-Type": "application/json", "User-Agent": "rss-to-discord/1.0"}, method="POST")
    with urlopen(req, timeout=20) as resp:
        resp.read()


def run_once(cfg: dict[str, Any]) -> int:
    rss_url = cfg["rss_url"].strip()
    webhook = cfg["webhook_url"].strip()
    username = cfg.get("discord_username") or "X RSS"
    avatar_url = (cfg.get("avatar_url") or "").strip()
    max_per_run = int(cfg.get("max_per_run") or 5)
    send_on_first_run = bool(cfg.get("send_on_first_run", False))
    force_test = os.environ.get("FORCE_TEST", "").lower() in {"1", "true", "yes"}

    seen: dict[str, Any] = load_json(SEEN_PATH, {"keys": []})
    known = set(seen.get("keys") or [])
    first_run = len(known) == 0

    raw = fetch(rss_url)
    items = parse_feed(raw)
    if not items:
        print("Aucun item dans le flux.")
        return 0

    # Toujours mémoriser pinned / vieux pour ne pas les renvoyer plus tard
    for item in items:
        if is_pinned(item) or is_too_old(item):
            known.add(item_key(item))

    if force_test:
        latest = next((it for it in items if not is_pinned(it) and not is_too_old(it)), None)
        if latest is None:
            print("TEST: rien de récent à envoyer (pinned/vieux ignorés).")
            save_json(SEEN_PATH, {"keys": list(known)[-2000:], "last_run": datetime.now(timezone.utc).isoformat()})
            return 0
        post_discord(webhook, latest, username, avatar_url)
        print(f"TEST OK  {latest.get('link') or latest.get('title')}")
        known.add(item_key(latest))
        save_json(SEEN_PATH, {"keys": list(known)[-2000:], "last_run": datetime.now(timezone.utc).isoformat()})
        return 1

    items_chrono = list(reversed(items))
    new_items = []
    for item in items_chrono:
        if is_pinned(item) or is_too_old(item):
            print(f"SKIP {'pinned' if is_pinned(item) else 'old'}  {item.get('link')}")
            continue
        k = item_key(item)
        if k not in known:
            new_items.append((k, item))

    if first_run and not send_on_first_run:
        for k, _ in new_items:
            known.add(k)
        seen_out = {"keys": list(known)[-2000:], "initialized_at": datetime.now(timezone.utc).isoformat()}
        save_json(SEEN_PATH, seen_out)
        print(f"Premier lancement : {len(new_items)} items mémorisés, rien envoyé.")
        return 0

    sent = 0
    for k, item in new_items[-max_per_run:]:
        try:
            post_discord(webhook, item, username, avatar_url)
            known.add(k)
            sent += 1
            print(f"OK  {item.get('link') or item.get('title')}")
            time.sleep(1.2)
        except Exception as e:
            print(f"ERR {item.get('link')}: {e}", file=sys.stderr)
            break

    save_json(SEEN_PATH, {"keys": list(known)[-2000:], "last_run": datetime.now(timezone.utc).isoformat()})
    print(f"Envoyé : {sent} / nouveaux : {len(new_items)}")
    return sent


def loop(cfg: dict[str, Any]) -> None:
    interval = int(cfg.get("interval_seconds") or 180)
    print(f"Boucle toutes les {interval}s — Ctrl+C pour arrêter")
    while True:
        try:
            run_once(cfg)
        except (HTTPError, URLError, ET.ParseError) as e:
            print(f"Erreur fetch/parse : {e}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nStop.")
            return
        time.sleep(interval)


def main() -> None:
    if not CONFIG_PATH.exists():
        print("config.json manquant.", file=sys.stderr)
        sys.exit(1)
    cfg = load_json(CONFIG_PATH, {})
    if not cfg.get("rss_url") or not cfg.get("webhook_url"):
        print("config.json : rss_url et webhook_url sont obligatoires.", file=sys.stderr)
        sys.exit(1)
    if "--once" in sys.argv:
        run_once(cfg)
    else:
        loop(cfg)


if __name__ == "__main__":
    main()
