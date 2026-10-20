"""Title words: suggestions extracted from a channel's titles, and the include and
exclude filter a source applies to them (docs/plan.md, Title words)."""

import re
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncSession

from savetube.db.models import Channel, GlobalWords

_CLOSERS = {"【": "】", "[": "]", "(": ")", "（": "）", "「": "」"}
_HASHTAG = re.compile(r"#(\w+)")
TOP = 30


def _closing(title: str, start: int) -> int | None:
    """Index of the bracket that closes the one at `start`, or None if unclosed."""
    stack = [_CLOSERS[title[start]]]
    for i in range(start + 1, len(title)):
        c = title[i]
        if c in _CLOSERS:
            stack.append(_CLOSERS[c])
        elif c in stack:
            # An inner bracket left open closes with the outer one.
            while stack.pop() != c:
                pass
            if not stack:
                return i
    return None


def _words(title: str) -> list[str]:
    """Words in order of appearance, original case, leading `#` stripped."""
    found = []
    i = 0
    while i < len(title):
        end = _closing(title, i) if title[i] in _CLOSERS else None
        if end is None:
            i += 1
            continue
        found.append(title[i + 1 : end].strip().lstrip("#").strip())
        i = end + 1
    found += _HASHTAG.findall(title)
    return [w for w in found if w]


def extract(title: str) -> set[str]:
    return set(_words(title))


def rank(titles: list[str]) -> list[tuple[str, int]]:
    """Words carried by more than one title, most titles first, top 30. Each word
    is shown in its most frequent original form."""
    videos: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}
    for title in titles:
        words = _words(title)
        videos.update({w.casefold() for w in words})
        for w in words:
            forms.setdefault(w.casefold(), Counter())[w] += 1
    return [(forms[key].most_common(1)[0][0], n) for key, n in videos.most_common() if n > 1][:TOP]


def matches(title: str, include: list[str], exclude: list[str]) -> bool:
    t = title.casefold()
    if include and not any(w.casefold() in t for w in include):
        return False
    return not any(w.casefold() in t for w in exclude)


Lists = tuple[list[str], list[str]]


async def effective(session: AsyncSession, channel_id: str | None, *own: Lists) -> Lists:
    """The global lists, the channel's when there is one, and `own` (a parent's
    and a source's), merged."""
    stored = [await session.get(GlobalWords, 1)]
    if channel_id is not None:
        stored.append(await session.get(Channel, channel_id))
    pairs = [(w.include_words, w.exclude_words) for w in stored if w is not None] + list(own)
    return (
        normalize_words([x for include, _ in pairs for x in include]),
        normalize_words([x for _, exclude in pairs for x in exclude]),
    )


def normalize_words(words: list[str]) -> list[str]:
    """Trimmed, empty strings dropped, the first form of each casefolded word kept."""
    kept: dict[str, str] = {}
    for w in words:
        w = w.strip()
        if w:
            kept.setdefault(w.casefold(), w)
    return list(kept.values())
