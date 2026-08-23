"""Tests for the SSL certificate-error detection used by API retry logic.

An SSL certificate-verification failure is a deterministic configuration
error — retrying it only burns the retry budget (observed: ~40s per failed
MusicBrainz call while scans looked "stalled").  ``is_ssl_cert_error`` must
detect it even when httpx/httpcore wrap the ``ssl.SSLCertVerificationError``
inside a ``ConnectError`` chain that drops the nested exception object and
keeps only the message text.
"""

from __future__ import annotations

import ssl

import httpx

from api_clients.http_utils import is_ssl_cert_error


class TestIsSslCertError:
    def test_direct_ssl_cert_verification_error(self):
        exc = ssl.SSLCertVerificationError(
            1, "certificate verify failed: unable to get local issuer certificate"
        )
        assert is_ssl_cert_error(exc) is True

    def test_httpx_connect_error_with_ssl_message(self):
        # httpx/httpcore wrap the SSL failure in ConnectError and only the
        # message text survives ("[SSL: CERTIFICATE_VERIFY_FAILED] ...").
        exc = httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:1010)"
        )
        assert is_ssl_cert_error(exc) is True

    def test_connect_error_with_httpcore_cause(self):
        # Simulate the real chain: httpx.ConnectError -> httpcore.ConnectError
        # whose message carries the SSL marker but no __cause__.
        httpcore_err = httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate"
        )
        outer = httpx.ConnectError("connection failed") 
        try:
            raise outer from httpcore_err
        except httpx.ConnectError as exc:
            assert is_ssl_cert_error(exc) is True

    def test_plain_connect_error_not_ssl(self):
        exc = httpx.ConnectError("connection refused")
        assert is_ssl_cert_error(exc) is False

    def test_timeout_not_ssl(self):
        exc = httpx.ConnectTimeout("timed out")
        assert is_ssl_cert_error(exc) is False
