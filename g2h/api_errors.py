"""Safe API failure categories shared by prompt generation and embeddings."""
import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Provider credentials must only reach the configured endpoint.
        return None


class TemporaryAPIError(RuntimeError):
    """Transport/rate/service failure; caller controls retry and cancellation."""


class ContentRejectedError(RuntimeError):
    """Provider rejected content; record and skip, never retry the same request."""


def http_failure(exc, service):
    # Inspect only a bounded provider code; never expose response bodies or keys.
    code = ''
    try:
        body = json.loads(exc.read(8192))
        error = body.get('error', body)
        code = str(error.get('code', '')).lower() if isinstance(error, dict) else ''
    except (ValueError, OSError, AttributeError, TypeError):
        pass
    known_codes = {'data_inspection_failed', 'datainspectionfailed', 'inappropriatecontent', 'invalidparameter', 'invalid_parameter', 'invalid_api_key', 'invalidapikey', 'arrearage', 'insufficient_quota', 'throttling', 'internalerror', 'accessdenied'}
    safe_code = code if code in known_codes else 'unavailable'
    detail = ' [provider_code=' + safe_code + ']'
    if exc.code == 400 and code in {'data_inspection_failed', 'datainspectionfailed', 'inappropriatecontent'}:
        return ContentRejectedError('%s HTTP %s' % (service, exc.code) + detail)
    fatal = any(word in code for word in
                ('quota', 'balance', 'billing', 'arrear', 'payment', 'allocation',
                 'invalid', 'accessdenied', 'forbidden', 'disabled'))
    if not fatal and (exc.code in (408, 429) or 500 <= exc.code <= 599):
        return TemporaryAPIError('%s temporary HTTP %s' % (service, exc.code) + detail)
    return RuntimeError('%s HTTP %s; check credentials, balance, access and configuration'
                        % (service, exc.code) + detail)
