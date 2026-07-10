class SiteConfigCacheMiddleware:
    """Opens a request-scoped memo for ``SiteConfig.get()`` (recommendation #2,
    Option A): the first read in a request hits the database, every subsequent
    read reuses that object, and the memo is unconditionally dropped when the
    request ends — so no request can ever see another request's copy and
    nothing is cached across requests (see SiteConfig.get for why cross-request
    caching was deliberately rejected)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from core.models import _siteconfig_local
        _siteconfig_local.scope_open = True
        _siteconfig_local.obj = None
        try:
            return self.get_response(request)
        finally:
            _siteconfig_local.scope_open = False
            _siteconfig_local.obj = None
