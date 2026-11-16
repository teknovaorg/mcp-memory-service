"""Same-origin HTTPS JSON-RPC; credentials never appear in URLs or errors."""

import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class LinkError(RuntimeError):
    def __init__(self, message, data=None):
        self.data = data or {}
        super().__init__(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise LinkError(
            "Memory endpoint redirected; update the connection profile explicitly"
        )


def load_profile(path):
    profile = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return validate_profile(profile)


def validate_profile(profile):
    url = urllib.parse.urlsplit(profile["url"])
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise LinkError("Non-loopback memory connections require HTTPS")
    if url.username or url.password or url.query or url.fragment:
        raise LinkError(
            "Memory endpoint must not contain credentials, query parameters or fragments"
        )
    if not profile.get("token") or not profile.get("project"):
        raise LinkError("Profile must contain a project and credential")
    return profile


class Connection:
    def __init__(self, profile):
        self.profile = validate_profile(profile)
        self.opener = urllib.request.build_opener(NoRedirect())
        self.request_id = 0

    def rpc(self, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        if len(raw) > 800 * 1024:
            raise LinkError("Memory batch exceeds the bounded request size")
        request = urllib.request.Request(
            self.profile["url"].rstrip("/") + "/mcp",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.profile["token"],
            },
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise LinkError("Memory response exceeds the bounded size")
                return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            raise LinkError(f"Memory request rejected (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LinkError(
                "Memory service unavailable; unsent source cursors were retained"
            ) from None

    def call(self, name, args):
        self.request_id += 1
        response = self.rpc(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        if not response or "error" in response:
            raise LinkError("Memory protocol request failed")
        result = response["result"]
        text = "\n".join(
            x.get("text", "")
            for x in result.get("content", [])
            if x.get("type") == "text"
        )
        try:
            data = json.loads(text)
        except ValueError:
            raise LinkError("Memory returned an invalid structured response") from None
        if result.get("isError"):
            raise LinkError(data.get("error", "Memory operation failed"), data)
        return data
