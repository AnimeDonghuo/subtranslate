import asyncio
import json
import sys
from urllib.parse import urlsplit

import pytest

from subtranslate.proxies import parse_proxy_pool
from subtranslate.vless import ProxyBridgeError, VlessProxy, vless_bridge

UUID = "12345678-1234-4234-8234-123456789abc"


def test_vless_reality_and_tls_websocket_parsing():
    reality, websocket = parse_proxy_pool(
        f"vless://{UUID}@reality.example:443?type=tcp&security=reality&sni=target.example"
        "&pbk=publickey&sid=abcd&fp=chrome&flow=xtls-rprx-vision#private-name,"
        f"vless://{UUID}@ws.example:443?type=ws&security=tls&sni=ws.example&path=%2Fproxy&host=ws.example"
    )
    assert isinstance(reality, VlessProxy)
    assert reality.outbound["streamSettings"]["realitySettings"]["publicKey"] == "publickey"
    assert reality.outbound["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"
    assert websocket.outbound["streamSettings"]["wsSettings"]["path"] == "/proxy"
    assert websocket.outbound["streamSettings"]["tlsSettings"]["allowInsecure"] is False
    assert UUID not in repr(reality) and "publickey" not in repr(reality)


@pytest.mark.parametrize("query", [
    "type=quic&security=tls", "type=tcp&security=reality&sni=a", "type=ws&security=tls&allowInsecure=1",
    "type=grpc&security=tls&mode=invalid", "type=tcp&security=tls&encryption=other", "type=tcp&type=ws",
])
def test_rejects_unsupported_vless_options_without_exposing_link(query):
    value = f"vless://{UUID}@private.example:443?{query}"
    with pytest.raises(ValueError) as error:
        parse_proxy_pool(value)
    assert UUID not in str(error.value) and "private.example" not in str(error.value)


def test_grpc_vless_link():
    proxy, = parse_proxy_pool(f"vless://{UUID}@grpc.example:443?type=grpc&security=tls&serviceName=svc&mode=gun")
    assert proxy.outbound["streamSettings"]["grpcSettings"]["serviceName"] == "svc"
    proxy, = parse_proxy_pool(f"vless://{UUID}@grpc.example:443?type=grpc&security=tls&mode=multi")
    assert proxy.outbound["streamSettings"]["grpcSettings"]["multiMode"] is True


async def test_vless_bridge_starts_and_stops_with_private_config(tmp_path, monkeypatch):
    executable = tmp_path / "fake-xray"
    marker = tmp_path / "marker"
    executable.write_text(f'''#!{sys.executable}
import json, os, socket, sys
path = sys.argv[-1]
with open(path) as config_file: config = json.load(config_file)
with open({str(marker)!r}, "w") as output:
    json.dump({{"path": path, "mode": os.stat(path).st_mode & 0o777, "config": config}}, output)
s = socket.socket()
inbound = config["inbounds"][0]
s.bind((inbound["listen"], inbound["port"]))
s.listen()
while True:
    connection, _ = s.accept()
    connection.close()
''')
    executable.chmod(0o700)
    monkeypatch.setenv("XRAY_PATH", str(executable))
    proxy, = parse_proxy_pool(f"vless://{UUID}@proxy.example:443?type=tcp&security=tls")
    async with vless_bridge(proxy) as endpoint:
        parsed = urlsplit(endpoint)
        assert parsed.hostname == "127.0.0.1"
        data = json.loads(marker.read_text())
        assert data["mode"] == 0o600
        assert data["config"]["inbounds"][0]["listen"] == "127.0.0.1"
        config_path = data["path"]
        assert data["config"]["outbounds"][0] == proxy.outbound
        _reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        writer.close()
        await writer.wait_closed()
    assert not __import__("pathlib").Path(config_path).exists()
    with pytest.raises(OSError):
        await asyncio.open_connection(parsed.hostname, parsed.port)


async def test_missing_xray_fails_without_leaking_details(monkeypatch):
    monkeypatch.setenv("XRAY_PATH", "/no/such/secret-xray")
    proxy, = parse_proxy_pool(f"vless://{UUID}@private.example:443?security=tls")
    with pytest.raises(ProxyBridgeError) as error:
        async with vless_bridge(proxy):
            pytest.fail("should not start")
    assert UUID not in str(error.value) and "private.example" not in str(error.value)


def test_xhttp_reality_link():
    proxy, = parse_proxy_pool(
        f"vless://{UUID}@server.example:443?type=xhttp&security=reality&sni=example.com"
        "&pbk=key&path=%2Fx&mode=stream-up"
    )
    stream = proxy.outbound["streamSettings"]
    assert stream["network"] == "xhttp"
    assert stream["xhttpSettings"]["mode"] == "stream-up"
    assert stream["realitySettings"]["serverName"] == "example.com"
