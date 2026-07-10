"""A small, safe caching layer for the heavy reporting aggregates that the
dashboard, executive and controls pages share (department_summary, trust_summary).

Design goals:
  * **Correctness first.** Any write to a financial model bumps a global
    "data version"; every cache key includes it, so a cached figure is dropped
    the instant the underlying data changes. A short TTL is only a backstop.
  * **Off by default.** Caching is active only when settings.DASHBOARD_CACHE_TTL
    is greater than 0 (set DJANGO_DASH_CACHE_TTL in production). With it at 0 —
    the default in dev and tests — these helpers compute directly, so test
    determinism and local development are never affected.
"""
import contextlib
import contextvars

from django.conf import settings
from django.core.cache import cache

_VERSION_KEY = "treasury:data_version"

# Request-scoped memo: a per-request dict held in a context variable, so it is
# automatically isolated between requests (and threads / async tasks) and never
# leaks. Enabled by RequestScopeMiddleware; usable directly via request_scope().
_REQUEST_MEMO: "contextvars.ContextVar" = contextvars.ContextVar(
    "treasury_request_memo", default=None)


def data_version():
    v = cache.get(_VERSION_KEY)
    if v is None:
        cache.set(_VERSION_KEY, 1, None)
        return 1
    return v


def bump_data_version():
    """Invalidate every cached aggregate (called on any financial write). Also
    clears the request-scoped memo, so a write followed by a read within the
    same request recomputes rather than serving a pre-write figure."""
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        cache.set(_VERSION_KEY, data_version() + 1, None)
    memo = _REQUEST_MEMO.get()
    if memo is not None:
        memo.clear()


def cache_ttl():
    return int(getattr(settings, "DASHBOARD_CACHE_TTL", 0) or 0)


def cached(key, compute):
    """Return compute() — deduplicated for the current request (always), and
    additionally cached across requests under `key` + the data version when the
    optional TTL cache is enabled.

    Two layers, different lifetimes:

    * **Request-scoped memo (always on).** Within one request/render, an
      identical `key` computes once. This is what makes a report that reads
      e.g. ``department_summary(s, e)`` from several sections pay for it only
      once (recommendation #1), with no configuration and no cross-request
      state. It is opened/closed by ``RequestScopeMiddleware`` (or the
      ``request_scope()`` context manager in tests); outside a scope this layer
      is simply inactive and compute() runs normally.
    * **Cross-request TTL cache (opt-in).** Unchanged: active only when
      DASHBOARD_CACHE_TTL > 0, invalidated by data_version bumps.
    """
    memo = _request_memo()
    if memo is not None and key in memo:
        return memo[key]

    ttl = cache_ttl()
    if ttl <= 0:
        val = compute()
    else:
        full = f"treasury:agg:{data_version()}:{key}"
        hit = cache.get(full)
        if hit is not None:
            val = hit
        else:
            val = compute()
            cache.set(full, val, ttl)

    if memo is not None:
        memo[key] = val
    return val


# ---------------------------------------------------------------------------
# Request-scoped memo helpers (the ContextVar itself is defined at the top).
# ---------------------------------------------------------------------------

def _request_memo():
    return _REQUEST_MEMO.get()


@contextlib.contextmanager
def request_scope():
    """Open a request-scoped memo for the duration of the block. Nested scopes
    reuse the outermost memo (so one render shares one memo throughout)."""
    existing = _REQUEST_MEMO.get()
    if existing is not None:
        yield existing
        return
    token = _REQUEST_MEMO.set({})
    try:
        yield _REQUEST_MEMO.get()
    finally:
        _REQUEST_MEMO.reset(token)


class RequestScopeMiddleware:
    """Wrap each request in a request_scope() so the reporting aggregates that
    go through cached() are computed at most once per request. Safe and cheap:
    the memo is a plain dict discarded at the end of the request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with request_scope():
            return self.get_response(request)
