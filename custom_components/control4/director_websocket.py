"""Director WebSocket client that survives reconnects (pyControl4 #62).

pyControl4 2.0.2's ``C4Websocket.sio_connect`` has two failure modes, both reproduced
against a fake Director (self-signed TLS, Engine.IO v3 / Socket.IO v2):

- without a session it passes ``ssl_verify=False``, and engineio calls
  ``ssl.create_default_context()`` inside the event loop on every connect and every
  automatic reconnect attempt (the blocking call HA reports in #62);
- with HA's no-verify session the first connect is clean, but after any drop engineio's
  ``_reset()`` closes its session and the next attempt opens a bare
  ``aiohttp.ClientSession()``, which rejects the Director's self-signed certificate.
  Reconnects fail silently until the next ``sio_connect()`` (the daily token refresh).

The engineio client below always (re)binds its HTTP session to the caller's connector, so
the connector's pre-built no-verify SSL context is used on every attempt and nothing
builds an SSL context in the loop. Disconnecting also stops a pending reconnect loop.
"""

import asyncio
import functools
from typing import Any, override

import aiohttp
import engineio_v3
from pyControl4.websocket import C4Websocket, _C4DirectorNamespace
import socketio_v4 as socketio

# How long disconnect() lets a pending reconnect attempt finish before cancelling it.
_RECONNECT_STOP_TIMEOUT = 10


class _DirectorEngineIOClient(engineio_v3.AsyncClient):
    """Engine.IO client whose HTTP session always wraps the caller's connector."""

    def __init__(self, *args: Any, connector: aiohttp.BaseConnector, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._connector = connector

    def _ensure_http(self) -> None:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(
                connector=self._connector, connector_owner=False
            )

    @override
    def _reset(self) -> None:
        # The base closes the wrapper session; the shared connector is not ours to close.
        super()._reset()
        self.http = None

    @override
    async def _connect_websocket(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_http()
        return await super()._connect_websocket(*args, **kwargs)

    @override
    async def _send_request(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_http()
        return await super()._send_request(*args, **kwargs)


class _DirectorSocketIOClient(socketio.AsyncClient):
    """Socket.IO client wired to ``_DirectorEngineIOClient``."""

    def __init__(self, *, connector: aiohttp.BaseConnector) -> None:
        self._connector = connector
        self._closing = False
        # ssl_verify=True: engineio must not build its own context, the connector's applies.
        super().__init__(ssl_verify=True)

    @override
    def _engineio_v3_client_class(self) -> Any:
        return functools.partial(_DirectorEngineIOClient, connector=self._connector)

    @override
    async def _handle_reconnect(self) -> None:
        # The loop starts by clearing the abort event, so an abort set before the task
        # got its first turn would be lost; a closing client never starts a loop.
        if self._closing:
            return
        await super()._handle_reconnect()

    @override
    async def disconnect(self) -> None:
        # socketio_v4's disconnect() leaves a running reconnect loop alive: after an unload
        # or a token refresh during an outage it keeps retrying with the old token and,
        # once the Director is back, opens a second socket next to the new one.
        # Cancelling alone doesn't stop it - the loop swallows CancelledError while it
        # sleeps between attempts. The abort event makes it exit at that sleep; cancel
        # only if it is stuck inside a connect attempt, where cancellation propagates.
        self._closing = True
        task = self._reconnect_task
        if task is not None and not task.done():
            self._reconnect_abort.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), _RECONNECT_STOP_TIMEOUT)
            except TimeoutError:
                task.cancel()
            except Exception:  # noqa: BLE001 - the loop's own failure is irrelevant here
                pass
        self._reconnect_task = None
        # If that last attempt did connect, this disconnects it too.
        await super().disconnect()


class DirectorWebsocket(C4Websocket):
    """``C4Websocket`` that requires HA's no-verify session and reconnects through it."""

    def __init__(
        self,
        ip: str,
        session_no_verify_ssl: aiohttp.ClientSession,
        connect_callback: Any = None,
        disconnect_callback: Any = None,
    ) -> None:
        super().__init__(ip, session_no_verify_ssl, connect_callback, disconnect_callback)

    @override
    async def sio_connect(self, director_bearer_token: str) -> None:
        """Same handshake as pyControl4 2.0.2, with the reconnect-safe client."""
        await self.sio_disconnect()
        assert self.session is not None
        self._sio = _DirectorSocketIOClient(connector=self.session.connector)
        self._sio.register_namespace(
            _C4DirectorNamespace(
                token=director_bearer_token,
                url=self.base_url,
                callback=self._callback,
                session=self.session,
                connect_callback=self.connect_callback,
                disconnect_callback=self.disconnect_callback,
            )
        )
        await self._sio.connect(
            self.wss_url,
            transports=["websocket"],
            headers={"JWT": director_bearer_token},
        )
