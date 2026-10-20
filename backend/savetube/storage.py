import os
import shutil
from pathlib import Path


def dir_size(path: Path) -> int:
    """Every file in the video's directory, so subtitles and thumbnails count."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def finalize(incoming: Path, final: Path) -> None:
    """Move a finished download into the tree.

    `incoming` sits on the same dataset as the tree, so this is one rename and
    a half-written file never appears there.
    """
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        raise FileExistsError(final)
    shutil.rmtree(incoming / ".tmp", ignore_errors=True)
    os.rename(incoming, final)


def disk_usage(path: Path) -> tuple[int, int]:
    """(total, free) bytes of the filesystem holding `path`."""
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
