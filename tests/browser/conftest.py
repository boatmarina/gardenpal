"""
Browser tests: the real app served on a local port, driven by Playwright in an
iPhone-sized viewport. Each test runs in Chromium and WebKit (Safari's engine);
a browser that isn't installed is skipped.

Any uncaught JS error or unexpected alert() fails the test. Screenshots of
failures are written to test-results/.
"""

import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest
from werkzeug.serving import make_server

from tests.conftest import signup

playwright_api = pytest.importorskip("playwright.sync_api")

RESULTS_DIR = Path(__file__).resolve().parents[2] / "test-results"
IPHONE = {
    "viewport": {"width": 390, "height": 664},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
}


@pytest.fixture(scope="session")
def live_server(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="session")
def _playwright():
    with playwright_api.sync_playwright() as p:
        yield p


_browsers = {}


@pytest.fixture(scope="session", params=["chromium", "webkit"])
def browser(request, _playwright):
    name = request.param
    if name not in _browsers:
        try:
            _browsers[name] = getattr(_playwright, name).launch()
        except Exception as exc:
            _browsers[name] = exc
    b = _browsers[name]
    if isinstance(b, Exception):
        pytest.skip(f"{name} not installed here (runs in CI): {str(b).splitlines()[0]}")
    yield b


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def page(request, browser, live_server):
    # Service workers bypass request routing, and WebKit hung on navigations with one active.
    context = browser.new_context(**IPHONE, base_url=live_server, service_workers="block")
    context.set_default_timeout(15000)

    def _local_only(route):
        host = urlparse(route.request.url).hostname
        if host in ("127.0.0.1", "localhost"):
            route.continue_()
        else:
            # Answer external requests (fonts, iNaturalist, CDNs) with an empty, CORS-enabled
            # 200 instead of a network error: WebKit stalls page loads on failed stylesheets
            # and reports blocked cross-origin fetches as page errors even when JS catches them.
            kind = route.request.resource_type
            body, ctype = {"stylesheet": ("", "text/css"), "fetch": ("{}", "application/json"),
                           "xhr": ("{}", "application/json")}.get(kind, ("", "text/plain"))
            route.fulfill(status=200, body=body, content_type=ctype,
                          headers={"Access-Control-Allow-Origin": "*"})

    context.route("**/*", _local_only)
    pg = context.new_page()
    pg.js_errors = []
    pg.dialogs = []
    def _on_pageerror(e):
        msg = str(e)
        # WebKit reports any failed or cancelled cross-origin request (e.g. an iNaturalist
        # search cut off by navigating away) as a page error, even when JS catches it.
        if "due to access control checks" in msg and "127.0.0.1" not in msg:
            return
        pg.js_errors.append(msg)

    pg.on("pageerror", _on_pageerror)

    def _on_dialog(d):
        pg.dialogs.append(d.message)
        d.dismiss()

    pg.on("dialog", _on_dialog)
    yield pg

    rep_call = getattr(request.node, "rep_call", None)
    failed = rep_call is None or rep_call.failed
    if failed or pg.js_errors:
        RESULTS_DIR.mkdir(exist_ok=True)
        try:
            pg.screenshot(path=str(RESULTS_DIR / f"{request.node.name}.png"), full_page=False)
        except Exception:
            pass
    context.close()
    if not failed:
        assert pg.js_errors == [], f"JavaScript errors in {browser.browser_type.name}: {pg.js_errors}"


@pytest.fixture
def logged_in(page, app):
    """Sign up 'tester' through the API, then log in through the real form.
    page.api is a test client logged in as the same user, for seeding data."""
    api = app.test_client()
    signup(api, "tester")
    page.api = api
    page.goto("/auth/login")
    page.fill("input[name=username]", "tester")
    page.fill("input[name=password]", "password123")
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: urlparse(url).path == "/", wait_until="domcontentloaded")
    return page
