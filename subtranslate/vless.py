"""VLESS link parsing and a short-lived, loopback-only Xray bridge."""

import asyncio
import json
import os
import socket
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


class ProxyBridgeError(Exception):
    """The local proxy bridge could not start; details may contain secrets."""


@dataclass(frozen=True)
class VlessProxy:
    # The entire configuration may contain credentials; never expose it in repr.
    outbound: dict = field(repr=False)


def parse_vless(value: str) -> VlessProxy:
    """Parse a deliberately limited subset of common VLESS share links."""
    try:
        url = urlsplit(value)
        if (url.scheme != "vless" or not url.hostname or not url.username or url.password is not None
                or url.port is None or not 1 <= url.port <= 65535 or url.path not in ("", "/")
                or any(c.isspace() for c in value)):
            raise ValueError
        identity = str(uuid.UUID(url.username))
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        supported = {"type", "security", "encryption", "flow", "sni", "fp", "pbk", "sid", "spx",
                     "host", "path", "serviceName", "authority", "alpn", "headerType", "mode", "allowInsecure"}
        if len(params) != len(pairs) or set(params) - supported:
            raise ValueError
        network = params.get("type", "tcp")
        security = params.get("security", "none")
        flow = params.get("flow", "")
        if (network not in {"tcp", "raw", "ws", "grpc", "xhttp", "splithttp"} or security not in {"none", "tls", "reality"}
                or params.get("encryption", "none") != "none" or flow not in {"", "xtls-rprx-vision"}):
            raise ValueError
        if security == "reality" and (not params.get("pbk") or not params.get("sni")
                                      or network not in {"tcp", "raw", "grpc", "xhttp", "splithttp"}):
            raise ValueError
        if flow and (security not in {"tls", "reality"} or network not in {"tcp", "raw"}):
            raise ValueError
        if security != "reality" and any(key in params for key in ("pbk", "sid", "spx")):
            raise ValueError
        if security == "none" and any(key in params for key in ("sni", "fp", "alpn")):
            raise ValueError
        if network not in {"ws", "xhttp", "splithttp"} and any(key in params for key in ("host", "path")):
            raise ValueError
        if network != "grpc" and any(key in params for key in ("serviceName", "authority")):
            raise ValueError
        if ("mode" in params and ((network == "grpc" and params["mode"] not in {"gun", "multi"})
                                  or (network in {"xhttp", "splithttp"} and params["mode"] not in
                                      {"auto", "packet-up", "stream-up", "stream-one"})
                                  or network not in {"grpc", "xhttp", "splithttp"})):
            raise ValueError
        if (params.get("headerType", "none") != "none"
                or params.get("allowInsecure", "0").lower() not in {"0", "false"}):
            raise ValueError
        if "headerType" in params and network not in {"tcp", "raw"}:
            raise ValueError
        stream: dict = {"network": "raw" if network in {"tcp", "raw"} else network, "security": security}
        if network == "ws":
            stream["wsSettings"] = {"path": params.get("path", "/"), "host": params.get("host", "")}
        elif network == "grpc":
            stream["grpcSettings"] = {"serviceName": params.get("serviceName", ""),
                                      "authority": params.get("authority", ""),
                                      "multiMode": params.get("mode") == "multi"}
        elif network in {"xhttp", "splithttp"}:
            stream["xhttpSettings"] = {"path": params.get("path", "/"), "host": params.get("host", ""),
                                       "mode": params.get("mode", "auto")}
        if security == "tls":
            settings = {"serverName": params.get("sni", url.hostname), "allowInsecure": False}
            if params.get("fp"):
                settings["fingerprint"] = params["fp"]
            if params.get("alpn"):
                settings["alpn"] = params["alpn"].split(",")
            stream["tlsSettings"] = settings
        elif security == "reality":
            if params.get("alpn"):
                raise ValueError
            stream["realitySettings"] = {"serverName": params["sni"], "publicKey": params["pbk"],
                                         "shortId": params.get("sid", ""),
                                         "fingerprint": params.get("fp", "chrome"),
                                         "spiderX": params.get("spx", "/")}
        return VlessProxy({
            "protocol": "vless",
            "settings": {"vnext": [{"address": url.hostname, "port": url.port,
                                    "users": [{"id": identity, "encryption": "none", "flow": flow}]}]},
            "streamSettings": stream,
        })
    except (ValueError, UnicodeError):
        # Never return the link, UUID or other parameters in an error.
        raise ValueError("Unsupported or invalid VLESS link") from None


@asynccontextmanager
async def vless_bridge(proxy: VlessProxy):
    """Start Xray bound to localhost; destroy its private config and process."""
    process = None
    with tempfile.TemporaryDirectory(prefix="subtranslate-vless-") as directory:
        # Reserve a random local port briefly. Xray must bind it after release.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        config = {
            "log": {"loglevel": "none"},
            "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks",
                          "settings": {"auth": "noauth", "udp": False}}],
            "outbounds": [proxy.outbound],
        }
        config_path = Path(directory) / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(config_path, 0o600)
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    os.environ.get("XRAY_PATH", "xray"), "run", "-config", str(config_path),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                async with asyncio.timeout(5):
                    while True:
                        if process.returncode is not None:
                            raise ProxyBridgeError("VLESS bridge did not start")
                        try:
                            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        except OSError:
                            await asyncio.sleep(0.05)
                            continue
                        writer.close()
                        await writer.wait_closed()
                        # Allow a failed bind to be reported before accepting the port.
                        await asyncio.sleep(0.1)
                        if process.returncode is not None:
                            raise ProxyBridgeError("VLESS bridge did not start")
                        break
            except (OSError, TimeoutError):
                raise ProxyBridgeError("VLESS bridge could not start") from None
            yield f"socks5://127.0.0.1:{port}"
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    async with asyncio.timeout(3):
                        await process.wait()
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
