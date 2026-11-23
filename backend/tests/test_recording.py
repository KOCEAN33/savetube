import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import _FakeTools, _settings, _Steps

from savetube.jobs import DownloadFailed
from savetube.jobs.recording import outcome_path, record
from savetube.recorder_args import Recording
from savetube.ytdl import WAIT_UNTIL, NotStarted

# Nothing listens here: the recorder must never need it.
NO_DATABASE = "postgresql://savetube@127.0.0.1:1/savetube"
MEMBERS_ONLY = (
    "ERROR: [youtube] v1: Join this channel to get access to members-only content"
    " like this video, and other exclusive perks."
)


@pytest.fixture
def settings(tmp_path):
    return _settings(tmp_path, NO_DATABASE)


async def _unread(message):
    raise AssertionError(f"reported {message}")


async def _record(settings, runner, **fields):
    rec = Recording(job_id=1, video_id="v1", format="bv*+ba/b", **fields)
    await record(rec, settings, runner=runner, report=_unread, tools=_FakeTools())


def _outcome(settings):
    return json.loads(outcome_path(settings.incoming_root, 1).read_text())


def _writes_v1(params, report):
    media = Path(params["paths"]["home"]) / "v1.mkv"
    media.write_bytes(b"recording")
    return {
        "id": "v1",
        "title": "t",
        "ext": "mkv",
        "requested_downloads": [{"filepath": str(media)}],
    }


async def _recording(params, report):
    return _writes_v1(params, report)


@pytest.mark.anyio
async def test_a_finished_recording_leaves_its_files_and_a_recorded_outcome(settings):
    await _record(settings, _Steps(_recording))
    outcome = _outcome(settings)
    assert outcome["outcome"] == "recorded" and outcome["info"]["id"] == "v1"
    assert (settings.incoming_root / "1" / "v1.mkv").read_bytes() == b"recording"


@pytest.mark.anyio
async def test_a_retry_starts_from_an_empty_directory_with_no_outcome(settings):
    work = settings.incoming_root / "1"
    work.mkdir(parents=True)
    (work / "v1.f299.mp4.part").write_bytes(b"dead attempt")
    outcome_path(settings.incoming_root, 1).write_text('{"outcome": "failed", "error": "x"}')
    seen = []

    async def recording(params, report):
        seen.append((sorted(work.iterdir()), outcome_path(settings.incoming_root, 1).exists()))
        return _writes_v1(params, report)

    await _record(settings, _Steps(recording))
    assert seen == [([], False)]


@pytest.mark.anyio
async def test_a_stream_still_upcoming_a_day_past_its_start_writes_not_started(settings):
    async def never_starts(params, report):
        raise NotStarted("ERROR: [youtube] v1: This live event will begin in a few moments.")

    runner = _Steps(never_starts)
    until = datetime(2026, 10, 24, 16, tzinfo=UTC)
    await _record(settings, runner, wait_until=until)
    assert runner.calls[0][WAIT_UNTIL] == until.timestamp()
    assert _outcome(settings) == {"outcome": "not_started"}
    assert not (settings.incoming_root / "1").exists()


@pytest.mark.anyio
async def test_a_failed_recording_writes_its_error_deletes_its_parts_and_raises(settings):
    async def stopped(params, report):
        (Path(params["paths"]["home"]) / "v1.f299.mp4.part").write_bytes(b"part")
        raise DownloadFailed("the live ended badly")

    runner = _Steps(stopped)
    with pytest.raises(DownloadFailed):
        await _record(settings, runner)
    assert WAIT_UNTIL not in runner.calls[0]
    assert _outcome(settings) == {"outcome": "failed", "error": "the live ended badly"}
    assert not (settings.incoming_root / "1").exists()


@pytest.mark.anyio
async def test_a_members_only_refusal_retries_with_the_member_profile(tmp_path):
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    (cookies / "general.txt").write_text("# Netscape HTTP Cookie File\n# general\n")
    (cookies / "member.txt").write_text("# Netscape HTTP Cookie File\n# member\n")
    settings = _settings(tmp_path, NO_DATABASE).model_copy(update={"cookies_dir": cookies})
    jars = []

    def jar(params):
        jars.append(Path(params["cookiefile"]).read_text())

    async def refused(params, report):
        jar(params)
        raise DownloadFailed(MEMBERS_ONLY)

    async def served(params, report):
        jar(params)
        return _writes_v1(params, report)

    await _record(settings, _Steps(refused, served), profile="general", member_profile="member")
    assert ["# general" in j for j in jars] == [True, False]
    assert "# member" in jars[1]
    assert _outcome(settings)["info"]["availability"] == "subscriber_only"


def test_a_recording_reads_back_from_its_job_arguments():
    rec = Recording(
        job_id=7,
        video_id="-x_Y",
        format="bv*+ba/b",
        profile="general",
        member_profile="member",
        members=True,
        wait_until=datetime(2026, 10, 24, 16, tzinfo=UTC),
    )
    parser = argparse.ArgumentParser()
    Recording.add_arguments(parser)
    assert Recording.from_args(parser.parse_args(rec.args())) == rec
    found_live = Recording(job_id=8, video_id="v8", format="b")
    assert Recording.from_args(parser.parse_args(found_live.args())) == found_live
