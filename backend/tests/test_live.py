from savetube.live import parse_feed

# Trimmed from https://www.youtube.com/feeds/videos.xml?channel_id=UCPd0Z22gF43dUidPYEQO5-w
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <id>yt:channel:Pd0Z22gF43dUidPYEQO5-w</id>
 <yt:channelId>Pd0Z22gF43dUidPYEQO5-w</yt:channelId>
 <title>VESPERBELL</title>
 <entry>
  <id>yt:video:B_eFxgIWdXo</id>
  <yt:videoId>B_eFxgIWdXo</yt:videoId>
  <yt:channelId>UCPd0Z22gF43dUidPYEQO5-w</yt:channelId>
  <title>【歌枠】ちょっと疲れちゃってるんだと思うよ【VESPERBELL ヨミ】</title>
  <published>2026-10-19T15:18:56+00:00</published>
 </entry>
 <entry>
  <id>yt:video:4eaIABAiMRg</id>
  <yt:videoId>4eaIABAiMRg</yt:videoId>
  <yt:channelId>UCPd0Z22gF43dUidPYEQO5-w</yt:channelId>
  <title>狭心症  - RADWIMPS (Cover) / VESPERBELL ヨミ</title>
  <published>2026-10-15T10:00:00+00:00</published>
 </entry>
</feed>
"""  # noqa: E501


def test_parse_feed_lists_video_ids_in_feed_order():
    assert parse_feed(FEED) == ["B_eFxgIWdXo", "4eaIABAiMRg"]


def test_parse_feed_of_a_channel_without_videos_is_empty():
    empty = FEED.split("<entry>")[0] + "</feed>"
    assert parse_feed(empty) == []
