import hashlib
import ipaddress
import os
import re
import tempfile
from typing import Optional, Dict, Any, List
from urllib.parse import unquote, urlsplit

from xuse.utils.sanitize import mask_url_userinfo

try:
    from ..core.config_loader import ConfigLoader, PROJECT_ROOT
except ImportError:
    import sys
    from pathlib import Path
    sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
    from xuse.core.config_loader import ConfigLoader, PROJECT_ROOT


VALID_PROXY_SCHEMES = ("http", "https", "socks4", "socks5")
MAX_PROXY_URL_LENGTH = 8192
_ENVIRONMENT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def mask_proxy_url(url: Optional[str]) -> Optional[str]:
    """Mask the whole userinfo: http://user:pass@host:port -> http://***@host:port.

    Delegates to the single implementation in xuse.utils.sanitize — this used to
    be a third copy of the same regex, which is how the '/'-in-password leak
    survived a fix applied elsewhere.
    """
    return mask_url_userinfo(url)


def validate_proxy_url(url: str, *, allow_environment: bool = True) -> str:
    """Validate proxy syntax without echoing credentials or connecting anywhere.

    Configuration may contain environment placeholders. Runtime callers must
    expand them and disable placeholders before using the returned URL. Driver
    support (notably proxy authentication) is checked separately at launch.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Proxy URL must not be empty.")
    u = url.strip()
    if (len(url) > MAX_PROXY_URL_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
            or any(char.isspace() for char in u) or "\\" in u):
        raise ValueError("Proxy URL contains invalid characters or exceeds the size limit.")
    if u.split("://", 1)[0].lower() not in VALID_PROXY_SCHEMES or "://" not in u:
        raise ValueError("Unsupported proxy scheme; use http, https, socks4 or socks5.")
    probe = u
    if "${" in probe:
        if not allow_environment:
            raise ValueError("Proxy environment variables must be resolved before use.")
        # Validate a placeholder's position too: a hostname or port may be
        # supplied later, but a literal path/query or malformed URL may not.
        probe = re.sub(r"\[\$\{[A-Za-z_][A-Za-z0-9_]*\}\]", "[::1]", probe)
        probe = _ENVIRONMENT.sub("1", probe)
        if "${" in probe:
            raise ValueError("Proxy environment placeholder is invalid.")
    try:
        parsed = urlsplit(probe)
        if (not parsed.hostname or parsed.path not in ("", "/")
                or "?" in probe or "#" in probe or parsed.netloc.count("@") > 1):
            raise ValueError()
        port = parsed.port
        if parsed.netloc.rsplit("@", 1)[-1].endswith(":") or port == 0:
            raise ValueError()
        host = parsed.hostname
        if ":" in host:
            if "%" in host:
                raise ValueError()
            ipaddress.IPv6Address(host)
        else:
            host = host.encode("idna").decode("ascii").lower()
            labels = host.rstrip(".").split(".")
            if (len(host) > 254 or any(not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?", label) for label in labels)):
                raise ValueError()
        for key, value in (("username", parsed.username), ("password", parsed.password)):
            if value is None:
                continue
            if re.search(r"%(?![0-9a-fA-F]{2})", value):
                raise ValueError()
            decoded = unquote(value, errors="strict")
            if ((key == "username" and not decoded)
                    or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
                raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError("Proxy URL has an invalid host, port, credentials or unsupported URL components.") from None
    return u


def expand_proxy_environment(value: str) -> str:
    """Resolve required variables; missing values never imply direct routing."""
    if (not isinstance(value, str) or len(value) > MAX_PROXY_URL_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Proxy configuration must be a bounded string.")

    def replace(match):
        replacement = os.environ.get(match.group(1))
        if not replacement:
            raise ValueError("A required proxy environment variable is missing or empty.")
        return replacement

    result = _ENVIRONMENT.sub(replace, value)
    if ("${" in result or len(result) > MAX_PROXY_URL_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in result)):
        raise ValueError("Proxy environment expansion is invalid or exceeds the size limit.")
    return result


class ProxyManager:
    """
    Resolves per-account proxies. Supports:
    - Direct proxy URLs (e.g., http://user:pass@host:port, socks5://host:port)
    - Named pools via settings: use value "pool:<POOL_NAME>" in account proxy
    - Env interpolation: ${ENV_VAR} within proxy strings will be replaced
    """

    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        self.settings = self.config_loader.get_settings() or {}
        if not isinstance(self.settings, dict):
            raise ValueError("Proxy settings must be an object.")
        bs = self.settings.get('browser_settings', {})
        if not isinstance(bs, dict):
            raise ValueError("Browser proxy settings must be an object.")
        self.default_proxy = bs.get('proxy')
        self.proxy_pools: Dict[str, List[str]] = bs.get('proxy_pools', {})
        self.strategy = bs.get('proxy_pool_strategy', 'hash')
        self.state_file: str = bs.get('proxy_pool_state_file') or 'data/proxy_pools_state.json'

    def _interpolate_env(self, s: str) -> str:
        return expand_proxy_environment(s)

    def _load_state(self) -> Dict[str, Any]:
        from pathlib import Path
        p = PROJECT_ROOT / self.state_file
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            try:
                import json
                state = json.loads(p.read_text(encoding='utf-8'))
                return state if isinstance(state, dict) else {}
            except (ValueError, UnicodeError):
                return {}
            except OSError:
                raise ValueError("Proxy pool state could not be read.") from None
        return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        from pathlib import Path
        import json
        p = PROJECT_ROOT / self.state_file
        staged = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=p.parent, prefix='.proxy-state-', suffix='.tmp')
            staged = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, p)
        except OSError:
            raise ValueError("Proxy pool state could not be saved.") from None
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass

    def _effective_value(self, account_proxy_value):
        if account_proxy_value is None or account_proxy_value == "":
            account_proxy_value = self.default_proxy
        if account_proxy_value is None or account_proxy_value == "":
            return None
        if not isinstance(account_proxy_value, str):
            raise ValueError("Proxy configuration must be a string.")
        return self._interpolate_env(account_proxy_value).strip()

    def _pool_members(self, value):
        pool_name = value.split(':', 1)[1]
        if (not isinstance(self.proxy_pools, dict) or not pool_name
                or any(char.isspace() or ord(char) < 32 for char in pool_name)):
            raise ValueError("Proxy pool reference or configuration is invalid.")
        pool = self.proxy_pools.get(pool_name)
        if not isinstance(pool, list) or not pool or any(not isinstance(item, str) or not item for item in pool):
            raise ValueError("Proxy pool is missing, empty or malformed.")
        if not isinstance(self.strategy, str) or self.strategy.strip().lower() not in ('hash', 'round_robin'):
            raise ValueError("Unsupported proxy pool strategy; use hash or round_robin.")
        return pool_name, pool

    def preview_routes(self, account_proxy_value: Optional[str] = None) -> List[str]:
        """Validate every candidate locally, without selecting or reading a cursor.

        Returned expanded URLs are private and may contain credentials. Health
        callers must discard them after checking backend compatibility.
        """
        value = self._effective_value(account_proxy_value)
        if value is None:
            return []
        candidates = self._pool_members(value)[1] if value.startswith('pool:') else [value]
        return [validate_proxy_url(self._interpolate_env(candidate), allow_environment=False)
                for candidate in candidates]

    def resolve(self, account_proxy_value: Optional[str], account_id: Optional[str] = None) -> Optional[str]:
        proxy_str = self._effective_value(account_proxy_value)
        if proxy_str is None:
            return None
        if proxy_str.startswith('pool:'):
            pool_name, pool = self._pool_members(proxy_str)
            if account_id is not None and not isinstance(account_id, str):
                raise ValueError("Proxy account identity must be a string.")
            choice: str
            if self.strategy.strip().lower() == 'round_robin':
                state = self._load_state()
                pool_idx = state.get(pool_name, 0)
                if isinstance(pool_idx, bool) or not isinstance(pool_idx, int) or pool_idx < 0:
                    raise ValueError("Proxy pool state has an invalid index.")
                choice = pool[pool_idx % len(pool)]
                resolved = validate_proxy_url(self._interpolate_env(choice), allow_environment=False)
                state[pool_name] = (pool_idx + 1) % len(pool)
                self._save_state(state)
                return resolved
            else:
                # Deterministic pick per account for stability; fallback to first.
                # hashlib (not builtin hash) so pinning survives process restarts —
                # builtin str hashing is salted per process (PYTHONHASHSEED).
                if account_id:
                    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
                    idx = int(digest, 16) % len(pool)
                    choice = pool[idx]
                else:
                    choice = pool[0]
            return validate_proxy_url(self._interpolate_env(choice), allow_environment=False)
        # Direct URL with optional env vars
        return validate_proxy_url(proxy_str, allow_environment=False)
