"""The Needs attention card's API (docs/plan/frontend.md, Home)."""

import pytest
from conftest import ADMIN_URL, _api, _sql

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


def _videos(settings, *states):
    """Channel UC1 with videos v1, v2, ... in the given states."""
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        *(
            "INSERT INTO video (id, channel_id, type, title, state) VALUES"
            f" ('v{n}', 'UC1', 'videos', 't{n}', '{state}')"
            for n, state in enumerate(states, 1)
        ),
    )


def _job(settings, job_id, kind, state, video, finished, **extra):
    cols = ", ".join(["id", "kind", "state", "video_id", "attempts", "finished_at", *extra])
    vals = ", ".join(
        [str(job_id), f"'{kind}'", f"'{state}'", f"'{video}'", "1", f"'{finished}'"]
        + [f"'{v}'" for v in extra.values()]
    )
    _sql(settings, f"INSERT INTO job ({cols}) VALUES ({vals})")


def _listed(api):
    return [(i["kind"], i["key"]) for i in api.get("/api/attention").json()]


def test_an_unresolved_archive_failure_is_listed_and_a_fixed_one_is_not(settings):
    _videos(settings, "failed", "complete")
    _job(settings, 1, "archive", "failed", "v1", "2026-11-01T00:00:00Z")
    _job(settings, 2, "archive", "failed", "v2", "2026-11-01T00:00:00Z")
    _job(settings, 3, "archive", "succeeded", "v2", "2026-11-02T00:00:00Z")

    with _api(settings) as api:
        assert _listed(api) == [("archive_failed", "1")]


def test_a_missed_recording_is_listed_until_its_vod_is_stored(settings):
    _videos(settings, "pending", "complete")
    _job(settings, 1, "record", "missed", "v1", "2026-11-01T00:00:00Z", miss_reason="dvr_off")
    _job(settings, 2, "record", "failed", "v2", "2026-11-01T00:00:00Z")

    with _api(settings) as api:
        body = api.get("/api/attention").json()

    assert [(i["kind"], i["key"], i["detail"]) for i in body] == [("record_failed", "1", "dvr off")]


def test_only_a_video_whose_latest_replace_failed_is_listed(settings):
    _videos(settings, "complete", "complete")
    _job(settings, 1, "replace", "failed", "v1", "2026-11-01T00:00:00Z")
    _job(settings, 2, "replace", "failed", "v2", "2026-11-01T00:00:00Z")
    _job(settings, 3, "replace", "succeeded", "v2", "2026-11-02T00:00:00Z")

    with _api(settings) as api:
        assert _listed(api) == [("replace_failed", "1")]


def test_only_a_video_whose_latest_encode_failed_is_listed(settings):
    _videos(settings, "complete", "complete")
    _sql(
        settings,
        "INSERT INTO encode_job (id, video_id, state, profile, source_path, source_size,"
        " reason, finished_at) VALUES"
        " (1, 'v1', 'failed', 'p', 'a.mkv', 1, 'ffmpeg exited 1', '2026-11-01T00:00:00Z'),"
        " (2, 'v2', 'failed', 'p', 'b.mkv', 1, 'x', '2026-11-01T00:00:00Z'),"
        " (3, 'v2', 'succeeded', 'p', 'b.mkv', 1, NULL, '2026-11-02T00:00:00Z')",
    )

    with _api(settings) as api:
        body = api.get("/api/attention").json()

    assert [(i["kind"], i["key"], i["detail"]) for i in body] == [
        ("encode_failed", "1", "ffmpeg exited 1")
    ]


def test_a_failing_source_with_a_channel_is_listed(settings):
    _videos(settings)
    _sql(
        settings,
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " last_error, last_polled_at) VALUES"
        " (1, 'https://a/1', 'tab', 'UC1', true, 60, 'HTTPError: 500', now()),"
        " (2, 'https://a/2', 'tab', 'UC1', false, 60, 'HTTPError: 500', now()),"
        " (3, 'https://a/3', 'tab', NULL, true, 60, 'HTTPError: 500', now()),"
        " (4, 'https://a/4', 'tab', 'UC1', true, 60, NULL, now())",
    )

    with _api(settings) as api:
        body = api.get("/api/attention").json()

    assert [(i["kind"], i["key"], i["channel_id"], i["dismissable"]) for i in body] == [
        ("source_failing", "1", "UC1", False)
    ]


def test_a_cookie_profile_whose_account_read_failed_is_listed(settings):
    _sql(
        settings,
        "INSERT INTO cookie_profile (name, seen_at, account_digest, account_checked_at) VALUES"
        " ('out', now(), NULL, now()), ('ok', now(), 'abc', now()), ('new', now(), NULL, NULL)",
    )

    with _api(settings) as api:
        assert _listed(api) == [("cookie_signed_out", "out")]


def test_items_list_newest_first(settings):
    _videos(settings, "failed", "failed")
    _job(settings, 1, "archive", "failed", "v1", "2026-11-01T00:00:00Z")
    _job(settings, 2, "archive", "failed", "v2", "2026-11-03T00:00:00Z")

    with _api(settings) as api:
        assert _listed(api) == [("archive_failed", "2"), ("archive_failed", "1")]


def test_a_dismissed_failure_stays_hidden_and_a_new_one_shows(settings):
    _videos(settings, "failed")
    _job(settings, 1, "archive", "failed", "v1", "2026-11-01T00:00:00Z")

    with _api(settings) as api:
        r = api.post(
            "/api/attention/dismiss", json={"items": [{"kind": "archive_failed", "key": "1"}]}
        )
        hidden = _listed(api)
        _job(settings, 2, "archive", "failed", "v1", "2026-11-02T00:00:00Z")
        later = _listed(api)

    assert r.status_code == 204
    assert hidden == []
    assert later == [("archive_failed", "2")]


def test_a_source_failure_cannot_be_dismissed(settings):
    with _api(settings) as api:
        r = api.post(
            "/api/attention/dismiss", json={"items": [{"kind": "source_failing", "key": "1"}]}
        )

    assert r.status_code == 422


def _dismiss(api, kind, key):
    r = api.post("/api/attention/dismiss", json={"items": [{"kind": kind, "key": key}]})
    assert r.status_code == 204


def test_a_dismissed_archive_failure_leaves_the_unresolved_job_list(settings):
    _videos(settings, "failed", "failed")
    _job(settings, 1, "archive", "failed", "v1", "2026-11-01T00:00:00Z")
    _job(settings, 2, "archive", "failed", "v2", "2026-11-02T00:00:00Z")

    def flags(jobs):
        return {j["id"]: (j["dismissable"], j["dismissed"]) for j in jobs}

    with _api(settings) as api:
        before = flags(api.get("/api/jobs", params={"unresolved": True}).json())
        _dismiss(api, "archive_failed", "1")
        unresolved = [j["id"] for j in api.get("/api/jobs", params={"unresolved": True}).json()]
        every = flags(api.get("/api/jobs").json())

    assert before == {1: (True, False), 2: (True, False)}
    assert unresolved == [2]
    assert every == {1: (False, True), 2: (True, False)}


def test_a_failure_a_later_run_fixed_offers_no_dismiss(settings):
    _videos(settings, "complete")
    _job(settings, 1, "archive", "failed", "v1", "2026-11-01T00:00:00Z")
    _job(settings, 2, "archive", "succeeded", "v1", "2026-11-02T00:00:00Z")

    with _api(settings) as api:
        jobs = {j["id"]: j["dismissable"] for j in api.get("/api/jobs").json()}

    assert jobs == {1: False, 2: False}


def test_a_dismissed_encode_failure_moves_to_finished_and_leaves_the_failed_count(settings):
    _videos(settings, "complete", "complete")
    _sql(
        settings,
        "INSERT INTO encode_job (id, video_id, state, profile, source_path, source_size,"
        " reason, finished_at) VALUES"
        " (1, 'v1', 'failed', 'p', 'a.mkv', 1, 'larger', '2026-11-01T00:00:00Z'),"
        " (2, 'v2', 'failed', 'p', 'b.mkv', 1, 'x', '2026-11-01T00:00:00Z')",
    )

    def shape(body):
        return (
            [(j["id"], j["dismissed"]) for j in body["waiting"]["items"]],
            body["waiting"]["total"],
            [(j["id"], j["dismissed"]) for j in body["finished"]["items"]],
            body["finished"]["total"],
            body["summary"]["failed"],
        )

    with _api(settings) as api:
        before = shape(api.get("/api/encode").json())
        _dismiss(api, "encode_failed", "1")
        after = shape(api.get("/api/encode").json())

    assert before == ([(1, False), (2, False)], 2, [], 0, 2)
    assert after == ([(2, False)], 1, [(1, True)], 1, 1)


def test_the_job_list_filters_by_video(settings):
    _videos(settings, "failed", "failed")
    _job(settings, 1, "archive", "failed", "v1", "2026-11-01T00:00:00Z")
    _job(settings, 2, "archive", "failed", "v2", "2026-11-02T00:00:00Z")

    with _api(settings) as api:
        r = api.get("/api/jobs", params={"video_id": "v2"})

    assert [j["id"] for j in r.json()] == [2]
    assert r.headers["X-Total-Count"] == "1"
