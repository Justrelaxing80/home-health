"""Container entrypoint: http and https on the same port.

The public port (8080) is a tiny TCP front that looks at the first byte of each
connection: 0x16 starts a TLS handshake, so the connection goes to an internal
https server; anything else goes to the internal http server. Both run the same
app in this process (the scheduler and database startup run once). So
http://host:9085 keeps working and https://host:9085 works too, with no extra port.

Set HTTPS=off to serve plain http only (the old behaviour).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket

import uvicorn

from . import tls

log = logging.getLogger("home-health.serve")
IDLE_FIRST_BYTE = 30  # seconds a client may stay silent before sending anything


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # main() handles signals for both servers
        yield


def _listener(host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(512)
    s.setblocking(False)
    return s


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


def _front(http_port: int, https_port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        up_w = None
        try:
            first = await asyncio.wait_for(reader.read(1), IDLE_FIRST_BYTE)
            if not first:
                return
            port = https_port if first == b"\x16" else http_port
            up_r, up_w = await asyncio.open_connection("127.0.0.1", port)
            up_w.write(first)
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            for w in (up_w, writer):
                if w is not None:
                    w.close()
    return handle


async def main():
    port = int(os.environ.get("PORT", "8080"))
    common = {"app": "app.main:app", "proxy_headers": True, "log_level": "info"}
    ctx = None
    if os.environ.get("HTTPS", "on").strip().lower() not in ("0", "off", "false", "no"):
        try:
            ctx = tls.server_context()
        except Exception:  # noqa: BLE001
            log.exception("https setup failed; serving plain http only")
    if ctx is None:
        await _Server(uvicorn.Config(host="0.0.0.0", port=port, **common)).serve()
        return

    tls.ACTIVE = True
    http_sock, https_sock = _listener("127.0.0.1", 0), _listener("127.0.0.1", 0)
    http_srv = _Server(uvicorn.Config(**common))
    https_srv = _Server(uvicorn.Config(lifespan="off", ssl_context_factory=lambda _cfg, _default: ctx, **common))
    front = await asyncio.start_server(_front(http_sock.getsockname()[1], https_sock.getsockname()[1]),
                                       host="0.0.0.0", port=port, reuse_address=True, backlog=512)
    log.info("serving http and https on port %d", port)

    def stop(*_):
        front.close()
        http_srv.should_exit = https_srv.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop))
    await asyncio.gather(http_srv.serve(sockets=[http_sock]), https_srv.serve(sockets=[https_sock]))
    front.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
