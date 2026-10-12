"""Strict cookie import for Playwright. No credentials are persisted or logged."""
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from xuse.core.config_loader import PROJECT_ROOT

from .errors import SessionError

MAX_COOKIE_FILE_BYTES = 1_048_576
MAX_COOKIES = 256
_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _cookie_expiration(raw: Dict[str, Any]) -> Optional[float]:
    """Accept exporter aliases only when they describe the same expiration."""
    values = [raw[key] for key in ("expires", "expirationDate", "expiry") if key in raw]
    for value in values:
        if value is None:
            continue
        try:
            finite = math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            finite = False
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
            raise SessionError("invalid_cookies")
    if values and any(value != values[0] for value in values[1:]):
        raise SessionError("invalid_cookies")
    return values[0] if values else None


def normalize_cookies(payload: Any, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Accept X exports only; malformed entries invalidate the entire import.

    Expired non-authentication cookies can be dropped. Authentication cookies
    must exist, be nonempty, be secure, and not be expired. Domains are checked
    exactly, never with a substring match or a twitter.com-to-x.com rewrite.
    Cookie arrays and cookies/storage-state envelopes are accepted. The latter
    import cookies only: local storage and IndexedDB are never restored.
    """
    now = time.time() if now is None else now
    if isinstance(payload, dict):
        if (set(payload) - {"cookies", "origins"}
                or ("origins" in payload and not isinstance(payload["origins"], list))):
            raise SessionError("invalid_cookies")
        payload = payload.get("cookies")
    if not isinstance(payload, list) or not payload or len(payload) > MAX_COOKIES:
        raise SessionError("invalid_cookies")
    result = []
    seen = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise SessionError("invalid_cookies")
        name, value = raw.get("name"), raw.get("value")
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(value, str) or len(value) > 16384
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise SessionError("invalid_cookies")
        domain = raw.get("domain", ".x.com")
        if domain not in ("x.com", ".x.com"):
            raise SessionError("invalid_cookies")
        for flag in ("secure", "httpOnly", "session", "hostOnly"):
            if flag in raw and not isinstance(raw[flag], bool):
                raise SessionError("invalid_cookies")
        # A leading dot tells Chromium to include subdomains. Exporters can
        # spell a host-only domain with a dot, so explicit metadata takes
        # precedence. Without metadata preserve the browser's exported scope.
        if "hostOnly" in raw:
            domain = "x.com" if raw["hostOnly"] else ".x.com"
        # This importer has no partition-aware account model. Silently
        # discarding a partition key would turn a scoped cookie into a broader
        # unpartitioned cookie. Explicit unpartitioned metadata is harmless.
        if (raw.get("partitionKey") is not None
                or raw.get("firstPartyDomain") not in (None, "")
                or ("partitioned" in raw and raw["partitioned"] is not False)):
            raise SessionError("invalid_cookies")
        path = raw.get("path", "/")
        if (not isinstance(path, str) or not path.startswith("/")
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise SessionError("invalid_cookies")
        # Host-only and domain variants can collide in the browser cookie
        # store. Refuse last-write-wins precedence for optional cookies too.
        key = (name, domain.lstrip("."), path)
        if key in seen:
            raise SessionError("invalid_cookies")
        seen.add(key)
        cookie = {"name": name, "value": value, "domain": domain, "path": path}
        cookie["secure"] = raw.get("secure", False)
        cookie["httpOnly"] = raw.get("httpOnly", False)
        same_site = raw.get("sameSite")
        if same_site is not None:
            if not isinstance(same_site, str):
                raise SessionError("invalid_cookies")
            mapped = {"strict": "Strict", "lax": "Lax", "none": "None",
                      "no_restriction": "None", "no-restriction": "None"}.get(same_site.lower())
            if ((not mapped and same_site.lower() != "unspecified")
                    or (mapped == "None" and not cookie["secure"])):
                raise SessionError("invalid_cookies")
            if mapped:
                cookie["sameSite"] = mapped
        expiration = _cookie_expiration(raw)
        if expiration is not None:
            if expiration != -1:
                if expiration <= now:
                    if name in ("auth_token", "ct0"):
                        raise SessionError("expired_credentials")
                    continue
                if not raw.get("session", False):
                    cookie["expires"] = float(expiration)
        if name in ("auth_token", "ct0") and (not value or not cookie["secure"] or path != "/"):
            raise SessionError("invalid_cookies")
        result.append(cookie)
    credentials = {c["name"] for c in result}
    if not {"auth_token", "ct0"}.issubset(credentials):
        raise SessionError("missing_cookies")
    # Two different cookies for a credential name have ambiguous precedence.
    if any(sum(c["name"] == name for c in result) != 1 for name in ("auth_token", "ct0")):
        raise SessionError("invalid_cookies")
    return result


def _unique_json_object(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def parse_cookie_export(data: bytes, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Parse bounded JSON bytes into cookies only, without exposing raw input.

    Duplicate fields are rejected before JSON can silently choose a domain,
    value, or expiration. This parser is shared by file loading and explicit
    account import so both boundaries make the same validation decision.
    """
    if not isinstance(data, bytes) or len(data) > MAX_COOKIE_FILE_BYTES:
        raise SessionError("invalid_cookies")
    try:
        payload = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError, RecursionError):
        raise SessionError("invalid_cookies") from None
    return normalize_cookies(payload, now=now)


def load_account_cookies(account: Dict[str, Any], config_loader: Any) -> List[Dict[str, Any]]:
    """Load inline cookies or the canonical AccountConfig cookie_file_path."""
    inline = account.get("cookies")
    if inline is not None:
        return normalize_cookies(inline)
    candidate = account.get("cookie_file_path")
    if not isinstance(candidate, str) or not candidate:
        raise SessionError("missing_cookies")
    config_dir = Path(getattr(config_loader, "accounts_file", PROJECT_ROOT / "config/accounts.json")).parent
    requested = Path(candidate)
    candidates = [requested] if requested.is_absolute() else [config_dir / requested, PROJECT_ROOT / requested]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                data = stream.read(MAX_COOKIE_FILE_BYTES + 1)
            return parse_cookie_export(data)
        except SessionError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise SessionError("invalid_cookies") from None
    raise SessionError("missing_cookies")
