"""HttpDesktopDriver + desktop factory-selection tests.

Verifies the agentd HTTP contract: action names, key translation, /type
clear_existing, /shell out/err, bearer auth, and honest failures.
"""

import httpx
import pytest

from artemis.drivers.base import KeyCode, SwipeDirection
from artemis.drivers.desktop.http_driver import HttpDesktopDriver


class _StubTransport(httpx.AsyncBaseTransport):
    """Captures requests and answers with canned agentd responses."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            import json

            body = json.loads(request.content)
        self.calls.append((request.method, request.url.path, body, dict(request.headers)))
        path = request.url.path
        if path == "/health":
            return httpx.Response(
                200, json={"ok": True, "screen": {"w": 1920, "h": 1080}}
            )
        if path == "/screenshot":
            return httpx.Response(200, content=b"\x89PNG-stub")
        if path == "/hierarchy":
            return httpx.Response(200, json={"elements": [{"role": "AXButton"}]})
        if path in ("/input", "/type", "/key", "/app"):
            return httpx.Response(200, json={"ok": True})
        if path == "/shell":
            return httpx.Response(200, json={"ok": True, "rc": 0, "out": "hi", "err": ""})
        return httpx.Response(404, json={"error": "nope"})


def _driver(token: str | None = "tok-1") -> tuple[HttpDesktopDriver, _StubTransport]:
    stub = _StubTransport()
    d = HttpDesktopDriver(
        device_id="mac-1", base_url="http://agentd.test", token=token,
        width=1440, height=900,
    )
    d._client = httpx.AsyncClient(
        transport=stub, base_url="http://agentd.test",
        headers=d._headers(), timeout=5.0,
    )
    return d, stub


@pytest.mark.asyncio
async def test_connect_pulls_screen_size():
    d, stub = _driver()
    await d.connect()
    assert d.screen_size == (1920, 1080)
    assert stub.calls[0][1] == "/health"


@pytest.mark.asyncio
async def test_bearer_token_sent():
    d, stub = _driver(token="secret-1")
    await d.connect()
    assert stub.calls[0][3]["authorization"] == "Bearer secret-1"


@pytest.mark.asyncio
async def test_no_token_no_auth_header():
    d, stub = _driver(token=None)
    await d.connect()
    assert "authorization" not in stub.calls[0][3]


@pytest.mark.asyncio
async def test_screen_data_combines_screenshot_and_ax():
    d, _ = _driver()
    data = await d.get_screen_data()
    assert data.screenshot_bytes == b"\x89PNG-stub"
    assert data.ui_elements == [{"role": "AXButton"}]
    assert data.ui_hierarchy_xml is None  # desktops have no DOM
    assert data.platform == "mac"


@pytest.mark.asyncio
async def test_tap_posts_click_action():
    d, stub = _driver()
    await d.tap(120, 340)
    method, path, body, _ = stub.calls[-1]
    assert path == "/input"
    assert body == {"action": "tap", "x": 120, "y": 340}


@pytest.mark.asyncio
async def test_swipe_direction_maps_cardinal():
    d, stub = _driver()
    await d.swipe_direction(SwipeDirection.UP)
    _, path, body, _ = stub.calls[-1]
    assert path == "/input" and body["action"] == "swipe"
    # up = start below center, end above center
    assert body["y"] > body["y2"] and body["x"] == body["x2"]


@pytest.mark.asyncio
async def test_input_text_uses_native_clear():
    d, stub = _driver()
    await d.input_text("hello", clear_existing=True)
    paths = [c[1] for c in stub.calls]
    assert paths == ["/type"]  # no /key hacks — agentd clears natively
    assert stub.calls[0][2] == {"text": "hello", "clear_existing": True}


@pytest.mark.asyncio
async def test_press_key_translates_android_to_mac():
    d, stub = _driver()
    await d.press_key(KeyCode.BACK)
    assert stub.calls[-1][2]["key"] == "ESC"
    await d.press_key(KeyCode.APP_SWITCH)
    assert stub.calls[-1][2]["key"] == "APP_SWITCH"
    await d.press_key("page_down")
    assert stub.calls[-1][2]["key"] == "PAGE_DOWN"


@pytest.mark.asyncio
async def test_shell_returns_out():
    d, _ = _driver()
    out = await d.execute_shell("echo hi")
    assert out == "hi"


@pytest.mark.asyncio
async def test_agentd_error_is_truthful():
    d, stub = _driver()
    stub.calls.clear()

    class _Fail(_StubTransport):
        async def handle_async_request(self, request):
            return httpx.Response(403, json={"ok": False, "error": "ax not granted"})

    d._client = httpx.AsyncClient(
        transport=_Fail(), base_url="http://agentd.test", timeout=5.0
    )
    with pytest.raises(RuntimeError, match="ax not granted"):
        await d.tap(1, 1)


def test_factory_selects_desktop_driver():
    from artemis.drivers.factory import create_driver
    from third_party.mobile_use.context import DeviceContext, DevicePlatform

    ctx_device = DeviceContext(
        mobile_platform=DevicePlatform.DESKTOP,
        device_id="mac-1",
        agentd_base="http://agentd.test",
        agentd_token="t",
    )
    ctx = type("C", (), {"device": ctx_device, "session_id": "s"})()
    drv = create_driver(ctx)
    assert isinstance(drv, HttpDesktopDriver)
    assert drv.device_id == "mac-1"
