"""Thin Fortnox API v3 client.

Not an Odoo model: a plain Python class configured from company settings and
the company credential row. The transport is isolated in _request_raw so the
test suite can mock it without any network access.

Security policy for logging: request path, HTTP status and the Fortnox error
code only. Never payloads, response bodies, customer data or tokens.
"""

import logging
import threading
import time
from collections import deque

import requests

from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.fortnox.se/3/"
OAUTH_AUTHORIZE_URL = "https://apps.fortnox.se/oauth-v1/auth"
OAUTH_TOKEN_URL = "https://apps.fortnox.se/oauth-v1/token"

_throttle_lock = threading.Lock()
_throttle_windows = {}


class FortnoxError(Exception):
    """A Fortnox request failed.

    Carries the Fortnox error code, a human readable text and the HTTP
    status, so the queue layer can store them on the queue entry.
    """

    def __init__(self, message, code="", http_status=None):
        super().__init__(message)
        self.error_code = code
        self.error_text = message
        self.http_status = http_status


class FortnoxAuthError(FortnoxError):
    """The connection is not authorized or Fortnox rejected the token."""


class SlidingWindowThrottle:
    """In memory sliding window limiter.

    Windows are shared per database and company through _throttle_windows so
    successive client instances do not reset the limit. Each worker process
    has its own window; the sandbox acceptance run confirms whether that is
    good enough against the real limit.
    """

    def __init__(self, max_calls, period, key, clock=time.monotonic, sleep=time.sleep):
        self.max_calls = max_calls
        self.period = period
        self.key = key
        self._clock = clock
        self._sleep = sleep

    def wait(self):
        while True:
            with _throttle_lock:
                now = self._clock()
                window = _throttle_windows.setdefault(self.key, deque())
                while window and now - window[0] >= self.period:
                    window.popleft()
                if len(window) < self.max_calls:
                    window.append(now)
                    return
                wait_time = max(self.period - (now - window[0]), 0.01)
            self._sleep(wait_time)


def token_request(company, data):
    """Exchange a code or refresh a token at the Fortnox OAuth endpoint.

    Returns the parsed JSON body. Failures raise FortnoxAuthError; the
    description from Fortnox is passed through but nothing else is logged
    or stored: no codes, no tokens.
    """
    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=(
                company.arc_fortnox_timeout_connect,
                company.arc_fortnox_timeout_read,
            ),
        )
    except requests.exceptions.Timeout:
        raise FortnoxAuthError(
            _("The Fortnox authorization service did not answer in time."),
            code="token_timeout",
        )
    except requests.exceptions.ConnectionError:
        raise FortnoxAuthError(
            _("The Fortnox authorization service is unreachable."),
            code="token_connection_error",
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        description = str(
            body.get("error_description") or body.get("error") or "unknown"
        )
        raise FortnoxAuthError(
            _("Fortnox authorization failed: %s", description),
            code=str(body.get("error") or "token_error"),
        )
    return body


class FortnoxClient:
    """One client instance per company and use."""

    def __init__(self, company, credential, clock=time.monotonic, sleep=time.sleep):
        self._company = company
        self._credential = credential
        self._throttle = SlidingWindowThrottle(
            max_calls=company.arc_fortnox_throttle_calls,
            period=company.arc_fortnox_throttle_seconds,
            key=(company.env.cr.dbname, "arc_fortnox", company.id),
            clock=clock,
            sleep=sleep,
        )

    @property
    def _access_token(self):
        return self._credential.sudo().access_token

    def _request_raw(self, method, url, headers, payload, params):
        return requests.request(
            method,
            url,
            headers=headers,
            json=payload,
            params=params,
            timeout=(
                self._company.arc_fortnox_timeout_connect,
                self._company.arc_fortnox_timeout_read,
            ),
        )

    def request(self, method, path, payload=None, params=None, _retried=False):
        token = self._access_token
        if not token:
            raise FortnoxAuthError(
                _("The Fortnox connection is not authorized."),
                code="not_authorized",
            )
        self._throttle.wait()
        url = f"{API_BASE_URL}{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = self._request_raw(method, url, headers, payload, params)
        except requests.exceptions.Timeout:
            raise FortnoxError(
                _("Fortnox did not answer within the configured timeout."),
                code="timeout",
            )
        except requests.exceptions.ConnectionError:
            raise FortnoxError(
                _("Fortnox is unreachable."),
                code="connection_error",
            )
        if response.status_code == 401 and not _retried:
            # Expired access token: a refresh may fix it, so retry exactly
            # once with the new token. Without a refresh token the
            # credential goes back to not authorized and the error stands.
            if self._credential.sudo()._arc_fortnox_on_unauthorized():
                return self.request(
                    method, path, payload=payload, params=params, _retried=True
                )
        return self._handle_response(response, method, path)

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, payload):
        return self.request("POST", path, payload=payload)

    def _handle_response(self, response, method, path):
        if response.status_code in (200, 201):
            return response.json() if response.content else {}
        error_code, error_text = self._parse_error_body(response)
        _logger.info(
            "Fortnox %s %s: HTTP %s, code %s",
            method,
            path,
            response.status_code,
            error_code or "none",
        )
        if response.status_code == 401:
            raise FortnoxAuthError(
                error_text or _("Fortnox rejected the access token."),
                code=error_code or "unauthorized",
                http_status=401,
            )
        raise FortnoxError(
            error_text or _("Fortnox returned an unexpected response."),
            code=error_code,
            http_status=response.status_code,
        )

    @staticmethod
    def _parse_error_body(response):
        try:
            body = response.json()
        except ValueError:
            return "", ""
        information = body.get("ErrorInformation") or {}
        code = str(information.get("error") or "")
        text = str(information.get("message") or "")
        return code, text
