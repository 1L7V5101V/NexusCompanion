from feeds.base import FeedSubscription
from feeds.rss import RSSFeedSource


def _make_source(name: str = "test") -> RSSFeedSource:
    sub = FeedSubscription.new(type="rss", name=name, url="https://example.com/rss")
    return RSSFeedSource(sub)


def test_parse_rss_with_leading_whitespace_before_xml_decl():
    source = _make_source()
    xml_text = """  <?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Post A</title>
      <link>https://example.com/a</link>
      <description>Hello</description>
      <pubDate>Mon, 23 Feb 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
    items = source._parse(xml_text, limit=5)
    assert len(items) == 1
    assert items[0].title == "Post A"
    assert items[0].url == "https://example.com/a"


def test_parse_xcancel_whitelist_feed_returns_empty_items():
    source = _make_source("xcancel")
    xml_text = """  <?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>RSS reader not yet whitelisted!</title>
    <description>RSS reader not yet whitelist!</description>
    <item>
      <title>RSS reader not yet whitelisted!</title>
      <link>https://rss.xcancel.com/foo/rss</link>
      <description>placeholder</description>
    </item>
  </channel>
</rss>
"""
    items = source._parse(xml_text, limit=5)
    assert items == []
