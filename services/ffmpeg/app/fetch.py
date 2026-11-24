"""Download input files from URLs and post job callbacks, guarded against SSRF.

Only http/https. Every hop (including redirects) must resolve to public addresses unless
ALLOW_PRIVATE_URLS=true, so a request cannot reach Railway's private network, the
metadata service or localhost. Plain file downloads only: no site extraction, no
streaming-site downloaders.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "ffmpeg-api-railway/1.0"
CHUNK = 1024 * 1024


class FetchError(Exception):
    """Download refused or failed; the message is shown to the client."""


def check_url(url: str, allow_private: bool) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("url must be an http(s) URL")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError(f"cannot resolve {parts.hostname}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global or address.is_multicast:
            raise FetchError(f"{parts.hostname} resolves to a private or reserved address; "
                             "set ALLOW_PRIVATE_URLS=true to allow it")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool):
        self.allow_private = allow_private
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > 5:
            raise FetchError("too many redirects")
        check_url(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(allow_private: bool) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_CheckedRedirects(allow_private))


def download(url: str, path: str, max_bytes: int, allow_private: bool, timeout: int = 60) -> str:
    """Stream url to path; returns the file name suggested by the URL."""
    check_url(url, allow_private)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener(allow_private).open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise FetchError(f"input is larger than MAX_INPUT_MB ({max_bytes // CHUNK} MB)")
            total = 0
            with open(path, "wb") as out:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(f"input is larger than MAX_INPUT_MB ({max_bytes // CHUNK} MB)")
                    out.write(chunk)
            if total == 0:
                raise FetchError("the URL returned an empty body")
            return urllib.parse.unquote(urllib.parse.urlsplit(response.geturl()).path.rsplit("/", 1)[-1])
    except urllib.error.HTTPError as err:
        raise FetchError(f"download failed: HTTP {err.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        reason = getattr(err, "reason", err)
        raise FetchError(f"download failed: {reason}") from None


def post_json(url: str, payload: dict, allow_private: bool) -> int:
    """POST the job to a callback URL; returns the HTTP status (0 when unreachable)."""
    try:
        check_url(url, allow_private)
        body = json.dumps(payload).encode()
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        with _opener(allow_private).open(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except (FetchError, urllib.error.URLError, OSError, ValueError):
        return 0
