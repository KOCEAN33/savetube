"""Liveness for the worker and the scheduler: `GET /healthz` answered by the
process's own asyncio loop, so a stuck loop fails the probe. The metrics port
cannot tell, because prometheus_client answers from its own thread."""

import asyncio

_READ_TIMEOUT = 5


def _response(status: str, body: bytes) -> bytes:
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        async with asyncio.timeout(_READ_TIMEOUT):
            request = (await reader.readline()).split()
            # The headers are read so the client is not reset on close.
            while (await reader.readline()).strip():
                pass
        if request[:2] == [b"GET", b"/healthz"]:
            writer.write(_response("200 OK", b"ok\n"))
        else:
            writer.write(_response("404 Not Found", b"not found\n"))
        await writer.drain()
    except TimeoutError, ConnectionError:
        pass
    finally:
        writer.close()


async def serve(port: int, host: str = "0.0.0.0") -> asyncio.Server:
    return await asyncio.start_server(_handle, host, port)
