"""The arguments of one recorder Job. Kept free of heavy imports: the CLI
builds every role's parser from it, and the web role imports savetube.k8s."""

import argparse
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Recording:
    """What one recorder Job records, as the scheduler read it from the rows."""

    job_id: int
    video_id: str
    format: str
    # The general profile, or the member profile for a members-only video.
    profile: str | None = None
    # The channel's member profile, which a members-only refusal of `profile`
    # retries with.
    member_profile: str | None = None
    members: bool = False
    # NOT_STARTED_LIMIT past the scheduled start, None for a stream found live.
    wait_until: datetime | None = None

    def args(self) -> list[str]:
        """The `savetube record` arguments that `from_args` reads back."""
        # One `--name=value` item each: a video id can start with `-`.
        args = [f"--job-id={self.job_id}", f"--video-id={self.video_id}", f"--format={self.format}"]
        if self.profile is not None:
            args.append(f"--cookies={self.profile}")
        if self.member_profile is not None:
            args.append(f"--member-cookies={self.member_profile}")
        if self.members:
            args.append("--members")
        if self.wait_until is not None:
            args.append(f"--wait-until={self.wait_until.isoformat()}")
        return args

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--job-id", type=int, required=True)
        parser.add_argument("--video-id", required=True)
        parser.add_argument("--format", required=True)
        parser.add_argument("--cookies")
        parser.add_argument("--member-cookies")
        parser.add_argument("--members", action="store_true")
        parser.add_argument("--wait-until", type=datetime.fromisoformat)

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> Recording:
        return cls(
            job_id=ns.job_id,
            video_id=ns.video_id,
            format=ns.format,
            profile=ns.cookies,
            member_profile=ns.member_cookies,
            members=ns.members,
            wait_until=ns.wait_until,
        )
