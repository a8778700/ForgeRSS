#!/usr/bin/env python3
# Copyright (C) 2026 ForgeRSS Contributors
# Licensed under AGPL-3.0

"""
Xiaoyuzhou (小宇宙) Podcast Feed Generator.

No login required. Uses curl_cffi to fetch the public podcast page directly.

Configuration:
1. XIAOYUZHOU_PODCAST_ID env var (comma-separated for multiple podcasts).
    Accepts:
      - pure podcast id  "5e7e9c70..."
      - full URL         "https://www.xiaoyuzhoufm.com/podcast/<id>"

2. Optional audio download (default off): XIAOYUZHOU_DOWNLOAD_AUDIO=true
   Single mp3 file per episode → downloads/xiaoyuzhou/<podcast_id>/<ep_id>.mp3

Example:
    XIAOYUZHOU_PODCAST_ID="<podcast_id>" python scripts/run_single.py xiaoyuzhou
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

from generators.base import Article, BaseFeedGenerator

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DOWNLOAD_DIR = PROJECT_ROOT / "downloads" / "xiaoyuzhou"


def _parse_podcast_input(raw: str) -> tuple[str, str]:
    """Resolve input into (podcast_id, podcast_url)."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    if raw.startswith(("http://", "https://")):
        m = re.search(r"/podcast/([a-zA-Z0-9]+)", raw)
        if m:
            pid = m.group(1)
            return pid, f"https://www.xiaoyuzhoufm.com/podcast/{pid}"
        return raw, raw
    return raw, f"https://www.xiaoyuzhoufm.com/podcast/{raw}"


class XiaoyuzhouPodcastGenerator(BaseFeedGenerator):
    """RSS generator for Xiaoyuzhou podcast episodes."""

    FEED_NAME = "xiaoyuzhou"
    FEED_TITLE = "Xiaoyuzhou Podcasts"
    FEED_URL = "https://www.xiaoyuzhoufm.com/"
    FEED_DESCRIPTION = "Latest podcast episodes from Xiaoyuzhou"
    FEED_LANGUAGE = "zh-CN"
    FEED_LOGO = "https://www.xiaoyuzhoufm.com/favicon.ico"

    PODCAST_INPUTS = [
        u.strip()
        for u in os.environ.get("XIAOYUZHOU_PODCAST_ID", "").split(",")
        if u.strip()
    ]

    MAX_EPISODES = int(os.environ.get("XIAOYUZHOU_MAX_EPISODES", "20"))
    DOWNLOAD_AUDIO = os.environ.get("XIAOYUZHOU_DOWNLOAD_AUDIO", "false").lower() == "true"

    def __init__(self):
        super().__init__()
        if not self.PODCAST_INPUTS:
            self.logger.warning("No podcasts configured. Set XIAOYUZHOU_PODCAST_ID.")

    def fetch_articles(self) -> list[Article]:
        articles = []
        self._podcast_metas: list[dict] = []

        per_pod_cap = self.MAX_EPISODES
        run_cap = getattr(self, "_run_max_articles", None)
        if run_cap is not None and run_cap < per_pod_cap:
            per_pod_cap = run_cap

        for raw in self.PODCAST_INPUTS:
            pid, purl = _parse_podcast_input(raw)
            self.logger.info(f"Fetching podcast {pid} (URL: {purl})")
            eps = self._fetch_podcast_episodes(pid, purl, max_episodes=per_pod_cap)
            articles.extend(eps)
            self.logger.info(f"Found {len(eps)} episodes from {pid}")

        if len(self._podcast_metas) == 1:
            meta = self._podcast_metas[0]
            podcast_title = meta.get("title") or "Xiaoyuzhou"
            self.FEED_TITLE = f"{podcast_title} (小宇宙播客)"
            brief = (meta.get("brief") or "").strip()
            desc = (meta.get("description") or "").strip()
            self.FEED_DESCRIPTION = (brief or desc[:200] or self.FEED_DESCRIPTION)
            img = (meta.get("image") or {})
            cover = img.get("largePicUrl") or img.get("picUrl")
            if cover:
                self.FEED_LOGO = cover

        return articles

    def _fetch_podcast_episodes(
        self,
        pid: str,
        purl: str,
        max_episodes: int = 20,
    ) -> list[Article]:
        try:
            from curl_cffi import requests as _creq
        except ImportError:
            return []

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            r = _creq.get(purl, headers=headers, impersonate="chrome", timeout=30)
        except Exception as e:
            return []
        if r.status_code != 200:
            return []

        html = r.text
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except Exception:
            return []

        try:
            podcast_meta = data["props"]["pageProps"]["podcast"]
        except (KeyError, TypeError):
            return []

        episodes_data = podcast_meta.get("episodes") or []
        if not episodes_data:
            return []

        podcast_title = podcast_meta.get("title") or f"Podcast{pid}"
        podcast_author = (podcast_meta.get("author") or "").strip() or podcast_title
        
        if hasattr(self, "_podcast_metas"):
            self._podcast_metas.append(podcast_meta)

        articles = []
        for ep in episodes_data[:max_episodes]:
            try:
                a = self._parse_episode(ep, pid, podcast_title, podcast_author, podcast_meta)
                if a:
                    articles.append(a)
            except Exception:
                continue

        if self.DOWNLOAD_AUDIO and articles:
            for art in articles:
                self._download_audio(art, pid)

        return articles

    def _parse_episode(
        self,
        ep: dict,
        pid: str,
        podcast_title: str,
        podcast_author: str = "",
        podcast_meta: Optional[dict] = None,
    ) -> Optional[Article]:
        ep_id = ep.get("eid") or ep.get("id")
        if not ep_id:
            return None

        title = (ep.get("title") or "").strip() or f"episode_{ep_id}"
        shownotes = (ep.get("shownotes") or "").strip()
        description = (ep.get("description") or "").strip()

        duration = ep.get("duration") or 0
        duration_str = ""
        if duration:
            h, rem = divmod(int(duration), 3600)
            m, s = divmod(rem, 60)
            duration_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

        pub_str = ep.get("pubDate") or ep.get("publishedAt") or ""
        pub_date = datetime.now(pytz.UTC)
        if pub_str:
            try:
                pub_date = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except Exception:
                pass

        audio_url = ""
        enclosure = ep.get("enclosure") or {}
        if isinstance(enclosure, dict):
            audio_url = enclosure.get("url") or ""
        if not audio_url:
            media = ep.get("media") or {}
            if isinstance(media, dict):
                src = media.get("source") or {}
                audio_url = src.get("url", "") if isinstance(src, dict) else ""

        cover_url = ""
        img = ep.get("image") or {}
        if isinstance(img, dict):
            cover_url = img.get("largePicUrl") or img.get("picUrl") or img.get("smallPicUrl") or ""

        permalink = f"https://www.xiaoyuzhoufm.com/episode/{ep_id}"

        html_parts = ['<div style="font-size:16px;line-height:1.8;color:#333">']

        if podcast_meta:
            pod_cover = (podcast_meta.get("image") or {}).get("smallPicUrl") or \
                        (podcast_meta.get("image") or {}).get("picUrl") or ""
            podcasters = podcast_meta.get("podcasters") or []
            podcaster_names = ", ".join(
                p.get("nickname", "") for p in podcasters if p.get("nickname")
            ) or podcast_author
            color = (podcast_meta.get("color") or {}).get("original") or "#2a9eed"
            html_parts.append(
                f'<div style="display:flex;align-items:center;gap:12px;'
                f'padding:12px;border-left:4px solid {color};background:#fafafa;'
                f'margin-bottom:16px">'
            )
            if pod_cover:
                html_parts.append(
                    f'<img src="{pod_cover}" alt="{podcast_title}" '
                    f'style="width:56px;height:56px;border-radius:6px;flex-shrink:0" />'
                )
            html_parts.append(
                f'<div><div style="font-weight:600">{podcast_title}</div>'
                f'<div style="color:#666;font-size:13px">主播：{podcaster_names}</div></div>'
            )
            html_parts.append('</div>')

        if cover_url:
            html_parts.append(
                f'<p><img src="{cover_url}" alt="{title}" '
                f'style="max-width:100%;height:auto;border-radius:8px" /></p>'
            )

        if duration_str:
            html_parts.append(f'<p><strong>⏱ 时长：</strong>{duration_str}</p>')

        if shownotes:
            html_parts.append(f'<div>{shownotes}</div>')
        elif description:
            from html import escape as _esc
            html_parts.append(f'<p>{_esc(description).replace(chr(10), "<br>")}</p>')

        if audio_url:
            html_parts.append(
                f'<p style="margin-top:12px"><audio controls src="{audio_url}" '
                f'style="width:100%"></audio></p>'
            )

        html_parts.append(
            f'<p style="margin-top:12px"><a href="{permalink}" '
            f'style="color:#2a9eed">在小宇宙打开 &rarr;</a></p>'
        )

        if podcast_meta and (podcast_meta.get("brief") or podcast_meta.get("description")):
            from html import escape as _esc2
            brief = (podcast_meta.get("brief") or "").strip()
            full_desc = (podcast_meta.get("description") or "").strip()
            html_parts.append(
                '<hr style="margin:20px 0;border:none;border-top:1px solid #eee" />'
                '<div style="color:#888;font-size:13px">'
            )
            if brief:
                html_parts.append(f'<p><strong>关于节目：</strong>{_esc2(brief)}</p>')
            if full_desc and full_desc != brief:
                html_parts.append(
                    f'<p>{_esc2(full_desc).replace(chr(10), "<br>")}</p>'
                )
            sub_count = podcast_meta.get("subscriptionCount")
            ep_count = podcast_meta.get("episodeCount")
            if sub_count or ep_count:
                bits = []
                if sub_count: bits.append(f"👥 {sub_count:,} 订阅")
                if ep_count: bits.append(f"🎧 共 {ep_count} 集")
                html_parts.append(f'<p>{" · ".join(bits)}</p>')
            html_parts.append('</div>')

        html_parts.append('</div>')

        # 核心修复：直接将 audio_url 赋值给 media 键或 enclosure 兼容字段
        media_list = []
        if audio_url:
            media_list.append({"url": audio_url, "type": "audio/mp4"})

        return Article(
            url=permalink,
            title=f"[{podcast_title}] {title}",
            published_at=pub_date,
            content='\n'.join(html_parts),
            summary=(description or shownotes)[:300],
            author=podcast_author or podcast_title,
            images=[cover_url] if cover_url else [],
            media=media_list,
            category="播客",
        )

    def _download_audio(self, article: Article, pid: str) -> None:
        pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate Xiaoyuzhou Podcast RSS")
    parser.add_argument("--max", type=int, default=20)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    gen = XiaoyuzhouPodcastGenerator()
    gen.run(full_refresh=args.full, max_articles=args.max)
