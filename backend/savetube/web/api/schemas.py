"""Pydantic models more than one route module uses."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from savetube.ytdl import strip_noise


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SourceOut(_Out):
    id: int
    url: str
    title: str | None
    kind: str
    channel_id: str | None
    enabled: bool
    poll_interval_minutes: int
    include_words: list[str]
    exclude_words: list[str]
    other_channels: bool
    parent_id: int | None
    archiving: bool
    last_polled_at: datetime | None
    last_error: str | None

    @field_validator("last_error")
    @classmethod
    def _plain(cls, v: str | None) -> str | None:
        return strip_noise(v) if v else v


# Every match runs each word against each listed title.
WordList = Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=200)]


class WordsIn(BaseModel):
    include_words: WordList = []
    exclude_words: WordList = []


class WordCount(_Out):
    word: str
    count: int
