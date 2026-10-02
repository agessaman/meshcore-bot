"""Shared ``requests`` session with retry/backoff for the weather and forecast APIs."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_retry_session() -> requests.Session:
    """A session that retries GETs on connection errors and 5xx responses.

    Two retries (three attempts) with 0.3 s / 0.6 s backoff. Status codes are
    not raised; callers inspect the response. Connections are pooled
    (10 pools, up to 20 connections each).
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
