"""DirectorWebsocket against a fake Director: pyControl4 #62 and reconnect after a drop.

The fake speaks what pyControl4 expects: TLS with a self-signed certificate, Engine.IO v3 /
Socket.IO v2 on /socket.io/, subscription via GET /api/v1/items/datatoui. HA's blocking-call
detector is emulated by wrapping the two SSLContext calls it reports in #62.
"""

import asyncio
from collections.abc import AsyncGenerator, Generator
import datetime
import ssl
from typing import Any

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pyControl4.websocket import C4Websocket
import pytest

from custom_components.control4.director_websocket import DirectorWebsocket

SUBSCRIPTION_PATH = "/api/v1/items/datatoui"


def _self_signed(tmp_path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "director")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


class FakeDirector:
    """One push event per connection, numbered by connection."""

    def __init__(self) -> None:
        self.connections: list[web.WebSocketResponse] = []
        self.port = 0
        self.available = True
        self.refused = 0
        self.handshake_tokens: list[str | None] = []
        self.subscription_tokens: list[str | None] = []

    async def socketio(self, request: web.Request) -> web.StreamResponse:
        if not self.available:
            self.refused += 1
            return web.Response(status=503)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections.append(ws)
        self.handshake_tokens.append(request.headers.get("JWT"))
        number = len(self.connections)
        await ws.send_str(
            '0{"sid":"s%d","upgrades":[],"pingInterval":25000,"pingTimeout":60000}'
            % number
        )
        await ws.send_str("40")
        await ws.send_str('42["clientId","client%d"]' % number)
        async for msg in ws:
            if msg.data == "2":
                await ws.send_str("3")
            elif msg.data.startswith('42["startSubscription"'):
                await ws.send_str(
                    '42["sub1",{"iddevice":5,"evtName":"OnDataToUI","data":{"level":%d}}]'
                    % number
                )
        return ws

    async def subscription(self, request: web.Request) -> web.Response:
        self.subscription_tokens.append(request.query.get("JWT"))
        return web.json_response({"subscriptionId": "sub1"})


@pytest.fixture
async def director(socket_enabled, tmp_path) -> AsyncGenerator[FakeDirector]:
    fake = FakeDirector()
    app = web.Application()
    app.router.add_get("/socket.io/", fake.socketio)
    app.router.add_get(SUBSCRIPTION_PATH, fake.subscription)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=_self_signed(tmp_path))
    await site.start()
    fake.port = site._server.sockets[0].getsockname()[1]
    yield fake
    for ws in fake.connections:
        await ws.close()
    await runner.cleanup()


@pytest.fixture
async def ha_session() -> AsyncGenerator[aiohttp.ClientSession]:
    """Like async_get_clientsession(hass, verify_ssl=False): context built up front."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context))
    yield session
    await session.close()


@pytest.fixture
def blocking_ssl_calls(monkeypatch) -> Generator[list[str]]:
    calls: list[str] = []
    for name in ("load_default_certs", "set_default_verify_paths"):
        original = getattr(ssl.SSLContext, name)

        def wrapper(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(ssl.SSLContext, name, wrapper)
    return calls


async def _wait_for(condition, timeout: float = 5) -> bool:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)
    return True


async def _connect(websocket_class, director: FakeDirector, session) -> tuple[Any, list]:
    events: list[int] = []

    async def on_event(device_id: int, message: dict) -> None:
        events.append(message["data"]["level"])

    websocket = websocket_class(f"127.0.0.1:{director.port}", session)
    websocket.add_item_callback(5, on_event)
    await websocket.sio_connect("token")
    return websocket, events


async def test_reconnects_after_director_drop_without_blocking_ssl(
    director: FakeDirector, ha_session, blocking_ssl_calls
) -> None:
    """После обрыва сокет переподключается сам, события идут, SSL-контекст в цикле не строится."""
    websocket, events = await _connect(DirectorWebsocket, director, ha_session)
    try:
        await _wait_for(lambda: events == [1])

        await director.connections[0].close()
        await _wait_for(lambda: events == [1, 2], timeout=10)

        assert len(director.connections) == 2
        assert blocking_ssl_calls == []
    finally:
        await websocket.sio_disconnect()


async def test_disconnect_during_outage_stops_reconnect_loop(
    director: FakeDirector, ha_session
) -> None:
    """Выгрузка или смена токена, пока директор лежит: старый цикл переподключения не выживает."""
    websocket, events = await _connect(DirectorWebsocket, director, ha_session)
    await _wait_for(lambda: events == [1])

    director.available = False
    await director.connections[0].close()
    await _wait_for(lambda: director.refused >= 1, timeout=10)

    await websocket.sio_disconnect()
    refused = director.refused
    director.available = True
    await asyncio.sleep(4)  # больше двух задержек переподключения socketio

    assert director.refused == refused
    assert len(director.connections) == 1
    assert events == [1]


async def test_rotate_token_opens_new_socket_before_closing_old(
    director: FakeDirector, ha_session
) -> None:
    """Смена токена: новый сокет подписан раньше, чем закрыт старый; сущности не гаснут."""
    drops: list[bool] = []

    async def on_disconnect() -> None:
        drops.append(True)

    events: list[int] = []

    async def on_event(device_id: int, message: dict) -> None:
        events.append(message["data"]["level"])

    websocket = DirectorWebsocket(
        f"127.0.0.1:{director.port}", ha_session, disconnect_callback=on_disconnect
    )
    websocket.add_item_callback(5, on_event)
    await websocket.sio_connect("old")
    try:
        await _wait_for(lambda: events == [1])

        await websocket.rotate_token("new")

        assert director.handshake_tokens == ["old", "new"]
        assert director.subscription_tokens == ["old", "new"]
        await _wait_for(lambda: events == [1, 2])
        await _wait_for(lambda: director.connections[0].closed)
        assert not director.connections[1].closed
        assert drops == []
    finally:
        await websocket.sio_disconnect()


async def test_failed_rotation_keeps_old_socket_and_its_reconnects_use_new_token(
    director: FakeDirector, ha_session
) -> None:
    """Новый сокет не открылся: старый живёт, а его переподключение идёт уже с новым токеном."""
    websocket, events = await _connect(DirectorWebsocket, director, ha_session)
    try:
        await _wait_for(lambda: events == [1])

        director.available = False
        with pytest.raises(Exception):
            await websocket.rotate_token("new")
        assert not director.connections[0].closed

        director.available = True
        await director.connections[0].close()
        await _wait_for(lambda: events == [1, 2], timeout=10)

        assert director.handshake_tokens == ["token", "new"]
        assert director.subscription_tokens == ["token", "new"]
    finally:
        await websocket.sio_disconnect()


async def test_stock_c4websocket_never_reconnects(
    director: FakeDirector, ha_session
) -> None:
    """Контроль стенда: pyControl4 2.0.2 с сессией HA после обрыва до директора не доходит."""
    # Отвергнутые клиентом TLS-рукопожатия фейк пишет в обработчик исключений цикла —
    # это и есть демонстрируемый дефект, а не сбой теста.
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: None)
    websocket, events = await _connect(C4Websocket, director, ha_session)
    try:
        await _wait_for(lambda: events == [1])

        await director.connections[0].close()
        await asyncio.sleep(4)  # задержки переподключения socketio: 0.5 с, 1 с, 2 с

        assert len(director.connections) == 1
        assert events == [1]
    finally:
        # Штатный disconnect() цикл переподключения не останавливает — гасим сами.
        websocket._sio._reconnect_abort.set()
        if websocket._sio._reconnect_task is not None:
            await asyncio.wait_for(websocket._sio._reconnect_task, 10)
        await websocket.sio_disconnect()
        await asyncio.sleep(0.1)
        loop.set_exception_handler(handler)
