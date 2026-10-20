import asyncio
import threading
import time
import urllib.error
import urllib.request

import pytest

from savetube.health import serve


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _get(url: str, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.mark.anyio
async def test_healthz_answers_while_the_loop_runs():
    async with await serve(0, host="127.0.0.1") as server:
        port = server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        assert await asyncio.to_thread(_get, f"{url}/healthz", 5) == (200, b"ok\n")
        status, _ = await asyncio.to_thread(_get, f"{url}/other", 5)
        assert status == 404


@pytest.mark.anyio
async def test_healthz_fails_while_the_loop_is_blocked():
    async with await serve(0, host="127.0.0.1") as server:
        port = server.sockets[0].getsockname()[1]
        result = {}

        def probe():
            try:
                result["answer"] = _get(f"http://127.0.0.1:{port}/healthz", 0.5)
            except OSError as e:
                result["error"] = e

        t = threading.Thread(target=probe)
        t.start()
        time.sleep(1.5)  # noqa: ASYNC251 - blocks the loop on purpose, as a stuck hook would
        await asyncio.to_thread(t.join)
        assert "error" in result, result
