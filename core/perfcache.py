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
from django.conf import settings
from django.core.cache import cache

_VERSION_KEY = "treasury:data_version"


def data_version():
    v = cache.get(_VERSION_KEY)
    if v is None:
        cache.set(_VERSION_KEY, 1, None)
        return 1
    return v


def bump_data_version():
    """Invalidate every cached aggregate (called on any financial write)."""
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        cache.set(_VERSION_KEY, data_version() + 1, None)


def cache_ttl():
    return int(getattr(settings, "DASHBOARD_CACHE_TTL", 0) or 0)


def cached(key, compute):
    """Return compute() — cached under `key` + the current data version when
    caching is enabled, otherwise computed fresh."""
    ttl = cache_ttl()
    if ttl <= 0:
        return compute()
    full = f"treasury:agg:{data_version()}:{key}"
    hit = cache.get(full)
    if hit is not None:
        return hit
    val = compute()
    cache.set(full, val, ttl)
    return val
