"""Parse explicitly configured browser proxies without exposing credentials."""

from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from .vless import VlessProxy, parse_vless


@dataclass(frozen=True)
class BrowserProxy:
    server: str = field(repr=False)
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    def playwright_options(self) -> dict[str, str]:
        options = {"server": self.server}
        if self.username is not None:
            options["username"] = self.username
        if self.password is not None:
            options["password"] = self.password
        return options


def parse_proxy_pool(value: str) -> tuple[BrowserProxy | VlessProxy, ...]:
    if not value.strip():
        return ()
    entries = value.split(",")
    if len(entries) > 10:
        raise ValueError("WEBSITE_PROXIES supports at most 10 proxies")
    result = []
    for entry in entries:
        entry = entry.strip()
        try:
            if entry.startswith("vless://"):
                result.append(parse_vless(entry))
                continue
            # A bare host:port is conventionally an HTTP proxy.
            url = urlsplit(entry if "://" in entry else "http://" + entry)
            if (url.scheme not in {"http", "https", "socks4", "socks5"}
                    or not url.hostname or url.port is None or not 1 <= url.port <= 65535
                    or url.path not in {"", "/"} or url.query or url.fragment
                    or any(char.isspace() for char in entry)
                    or (url.scheme.startswith("socks") and (url.username is not None or url.password is not None))
                    or (url.password is not None and url.username is None)):
                raise ValueError
            host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
            result.append(BrowserProxy(
                f"{url.scheme}://{host}:{url.port}",
                unquote(url.username) if url.username is not None else None,
                unquote(url.password) if url.password is not None else None,
            ))
        except ValueError:
            raise ValueError(
                "Invalid WEBSITE_PROXIES entry; use http(s)://[user:password@]host:port "
                "unauthenticated socks4/5://host:port, or a supported vless:// link"
            ) from None
    return tuple(result)
