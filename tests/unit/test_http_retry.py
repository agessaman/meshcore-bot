"""make_retry_session keeps the retry policy the weather paths were built with."""

from modules.http_retry import make_retry_session


def test_retry_policy_and_pooling():
    session = make_retry_session()
    for prefix in ("https://", "http://"):
        adapter = session.get_adapter(prefix + "example.invalid")
        retry = adapter.max_retries
        assert retry.total == 2
        assert retry.backoff_factor == 0.3
        assert set(retry.status_forcelist) == {500, 502, 503, 504}
        assert set(retry.allowed_methods) == {"GET"}
        assert retry.raise_on_status is False
        assert adapter._pool_connections == 10
        assert adapter._pool_maxsize == 20


def test_each_call_returns_a_new_session():
    assert make_retry_session() is not make_retry_session()
