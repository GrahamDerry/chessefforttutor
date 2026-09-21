"""Chess.com public API client (SPEC.md §4 A1).

The User-Agent header is mandatory: without it Chess.com answers 403.
"""
from __future__ import annotations

import time

import requests

from pipeline import config


class ChessComClient:
    def __init__(self, username: str | None = None, session: requests.Session | None = None):
        self.username = (username or config.username()).lower()   # API paths are lowercase
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    def _get(self, url: str) -> dict:
        last: Exception | None = None
        for attempt in range(config.HTTP_RETRIES):
            try:
                r = self.session.get(url, timeout=config.HTTP_TIMEOUT_S)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
                r.raise_for_status()
                return r.json()
            except requests.RequestException as exc:  # network blip, rate limit, 5xx
                last = exc
                time.sleep(2 ** attempt)
        assert last is not None
        raise last

    def archives(self) -> list[str]:
        """Monthly archive URLs, oldest first (as the API returns them)."""
        return list(self._get(f"{config.API_BASE}/{self.username}/games/archives")["archives"])

    def month_games(self, archive_url: str) -> list[dict]:
        return list(self._get(archive_url).get("games", []))


def year_month(archive_url: str) -> str:
    """'.../games/2026/09' -> '2026-09'."""
    parts = archive_url.rstrip("/").split("/")
    return f"{parts[-2]}-{parts[-1]}"


def select_archives(archives: list[str], *, since: str | None = None, months: int | None = None,
                    newest_first: bool = False) -> list[str]:
    """Pick the monthly archives to ingest. `since` (YYYY-MM) wins over `months`."""
    urls = sorted(archives, key=year_month)
    if since:
        urls = [u for u in urls if year_month(u) >= since]
    elif months:
        urls = urls[-months:]
    return list(reversed(urls)) if newest_first else urls
