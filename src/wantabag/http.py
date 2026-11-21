"""Shared requests session."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
TIMEOUT = 20


class _TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", TIMEOUT)
        return super().request(*args, **kwargs)


def make_session() -> requests.Session:
    session = _TimeoutSession()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def page_text(resp: requests.Response) -> str:
    """Decode a page as UTF-8. requests assumes ISO-8859-1 for ``text/html`` without charset."""
    return resp.content.decode("utf-8", errors="replace")
