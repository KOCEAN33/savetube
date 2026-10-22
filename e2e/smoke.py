"""Frontend smoke checks against a running savetube with a seeded database.

Run: uvx --with playwright python -I e2e/smoke.py <base url> <manifest path>
The manifest comes from e2e/seed.py. Exits 1 when any check fails.

Every check opens its page in a fresh browser context and fails on a console
error, an uncaught page error, an /api response of 400 or more, or an /api
request that fails for another reason than an abort. A cross-origin image gets
a 1x1 PNG, so YouTube thumbnails need no network. Any other cross-origin
request is aborted and fails the check, because the pages must render offline.

The checks target roles, element types, ids, aria-labels, data-slot attributes
and URL parameters, never copy text or layout.
"""

import base64
import json
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

BASE = sys.argv[1].rstrip("/")
MANIFEST = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
TIMEOUT_MS = 15_000
expect.set_options(timeout=TIMEOUT_MS)


def path_of(url: str) -> str:
    return urlsplit(url).path


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def is_api(url: str) -> bool:
    return url.startswith(BASE + "/api/")


def settle(page: Page) -> None:
    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)


def check_home(page: Page) -> str:
    with page.expect_response(
        lambda r: path_of(r.url) == "/api/stats/summary"
    ) as summary:
        page.goto(BASE + "/")
    assert summary.value.ok, f"/api/stats/summary answered {summary.value.status}"
    expect(page.locator("main")).to_be_visible()
    cards = page.locator("main [data-slot=card]")
    # Downloads plus the statistics cards: two or fewer means the statistics did not render.
    expect(cards.nth(2)).to_be_visible()
    settle(page)
    return f"{cards.count()} cards"


def check_videos(page: Page) -> str:
    with page.expect_response(lambda r: path_of(r.url) == "/api/videos") as first:
        page.goto(BASE + "/videos")
    assert first.value.ok
    table = page.get_by_role("table")
    expect(table).to_be_visible()
    expect(table.locator("tbody a[href^='/videos/']").first).to_be_visible()
    settle(page)

    page.get_by_role("combobox", name="Channel", exact=True).click()
    options = page.get_by_role("listbox").get_by_role("option")
    expect(options.nth(1)).to_be_visible()
    # The first option is "all channels". The last is a channel name.
    with page.expect_response(
        lambda r: path_of(r.url) == "/api/videos" and "channel_name" in query_of(r.url)
    ) as filtered:
        options.last.click()
    assert filtered.value.ok, f"filtered /api/videos answered {filtered.value.status}"
    expect(page).to_have_url(re.compile(r"[?&]channel_name="))
    expect(table.locator("tbody a[href^='/videos/']").first).to_be_visible()
    settle(page)
    name = query_of(filtered.value.url)["channel_name"][0]
    return f"filter sent channel_name={name!r}"


def check_live(page: Page) -> str:
    with page.expect_response(
        lambda r: (
            path_of(r.url) == "/api/jobs" and query_of(r.url).get("kind") == ["record"]
        )
    ) as jobs:
        page.goto(BASE + "/live")
    assert jobs.value.ok
    expect(page.get_by_role("table")).to_be_visible()
    settle(page)
    return "jobs table rendered"


def check_downloads(page: Page) -> str:
    def finished(r) -> bool:
        return path_of(r.url) == "/api/jobs" and "succeeded" in query_of(r.url).get(
            "state", []
        )

    with page.expect_response(finished) as first:
        page.goto(BASE + "/downloads")
    assert first.value.ok
    assert query_of(first.value.url).get("unresolved") == ["true"], first.value.url
    expect(page.get_by_role("table").last).to_be_visible()
    settle(page)

    hide = page.get_by_role("checkbox", name="Hide resolved")
    expect(hide).to_be_checked()
    with page.expect_response(finished) as every:
        hide.click()
    assert every.value.ok and "unresolved" not in query_of(every.value.url)
    expect(page).to_have_url(re.compile(r"[?&]resolved=show"))
    settle(page)

    page.get_by_role("combobox", name="State", exact=True).click()
    with page.expect_response(
        lambda r: path_of(r.url) == "/api/jobs" and query_of(r.url).get("state") == ["failed"]
    ) as failed:
        page.get_by_role("option", name="failed", exact=True).click()
    assert failed.value.ok
    expect(page).to_have_url(re.compile(r"[?&]state=failed"))
    settle(page)
    return "hide resolved and state filter sent"


def check_channel(page: Page) -> str:
    channel_id = MANIFEST["channel_id"]
    page.goto(f"{BASE}/channels/{channel_id}")
    # A single-choice Radix ToggleGroup: a radiogroup of radios.
    group = page.get_by_role("radiogroup", name="Channel section")
    expect(group).to_be_visible()
    items = group.get_by_role("radio")
    expect(items.nth(1)).to_be_visible()
    settle(page)
    visited = []
    for i in range(items.count()):
        item = items.nth(i)
        if item.get_attribute("aria-checked") == "true":
            visited.append(query_of(page.url).get("tab", ["(default)"])[0])
            continue
        before = page.url
        item.click()
        expect(item).to_have_attribute("aria-checked", "true")
        expect(page).not_to_have_url(before)
        settle(page)
        visited.append(query_of(page.url).get("tab", ["(default)"])[0])
    assert len(set(visited)) == items.count(), (
        f"tabs did not each change the URL: {visited}"
    )
    return "tabs " + ", ".join(visited)


def check_settings(page: Page) -> str:
    page.goto(BASE + "/settings")
    for control in (
        "#dl-concurrent",
        "#dl-rate",
        "#dl-daily",
        "#dl-window-enabled",
        "#dl-cookie-profile",
        "#subtitle-lang",
    ):
        expect(page.locator(control)).to_be_visible()
    settle(page)
    concurrent = page.locator("#dl-concurrent")
    new = 4 if concurrent.input_value() == "3" else 3
    concurrent.fill(str(new))
    form = page.locator("form").filter(has=concurrent)
    with page.expect_response(
        lambda r: (
            path_of(r.url) == "/api/download-settings" and r.request.method == "PUT"
        )
    ) as put:
        form.locator("button[type=submit]").click()
    assert 200 <= put.value.status < 300, f"PUT answered {put.value.status}"
    saved = put.value.json()["max_concurrent"]
    assert saved == new, f"PUT saved max_concurrent={saved}, sent {new}"
    settle(page)
    return f"PUT {put.value.status}, max_concurrent={saved}"


def check_video(page: Page) -> str:
    video_id = MANIFEST["video_id"]
    if not MANIFEST["media"]:
        page.goto(f"{BASE}/videos/{video_id}")
        expect(page.locator("main")).to_be_visible()
        settle(page)
        return "SKIPPED media assertion: no ffmpeg made the media"
    with page.expect_response(
        lambda r: path_of(r.url) == f"/api/videos/{video_id}/media"
    ) as media:
        page.goto(f"{BASE}/videos/{video_id}")
    status = media.value.status
    assert status in (200, 206), f"media answered {status}"
    expect(page.locator("main video")).to_be_visible()
    settle(page)
    return f"media {status}"


CHECKS = [
    ("Home", check_home),
    ("Videos", check_videos),
    ("Live", check_live),
    ("Downloads", check_downloads),
    ("Channel", check_channel),
    ("Settings", check_settings),
    ("Video", check_video),
]


def run(browser, name, check) -> bool:
    problems: list[str] = []
    context = browser.new_context(base_url=BASE)

    def outside(route):
        req = route.request
        if req.resource_type == "image":
            route.fulfill(status=200, content_type="image/png", body=PIXEL)
        else:
            problems.append(f"external request: {req.method} {req.url}")
            route.abort()

    context.route(lambda url: not url.startswith(BASE + "/"), outside)
    page = context.new_page()
    page.on(
        "console",
        lambda m: (
            m.type == "error"
            and problems.append(f"console error: {m.text} at {m.location}")
        ),
    )
    page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
    page.on(
        "response",
        lambda r: (
            is_api(r.url)
            and r.status >= 400
            and problems.append(f"api {r.request.method} {r.url} -> {r.status}")
        ),
    )
    page.on(
        "requestfailed",
        lambda r: (
            is_api(r.url)
            and "ERR_ABORTED" not in (r.failure or "")
            and problems.append(f"api {r.method} {r.url} failed: {r.failure}")
        ),
    )
    detail = ""
    try:
        detail = check(page)
    # Any exception fails this check only, so the other checks still run.
    except Exception as e:  # noqa: BLE001
        problems.append(f"assertion: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
    finally:
        context.close()
    status = "FAIL" if problems else "PASS"
    print(f"{status} {name}: {detail}", flush=True)
    for p in problems:
        print(f"    {p}", flush=True)
    return not problems


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        results = [run(browser, name, check) for name, check in CHECKS]
        browser.close()
    print(f"{sum(results)}/{len(results)} checks passed", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
