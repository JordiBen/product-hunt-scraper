import asyncio
import datetime as dt
import gzip
import json
import pathlib

import jsonschema
import pytest

from src import main as m
from src import ph

FX = pathlib.Path(__file__).parent / 'fixtures'
ROOT = pathlib.Path(__file__).resolve().parents[1]


def fx(name: str) -> str:
    f = FX / f'{name}.html.gz'
    if not f.exists():  # page snapshots are not redistributed in the public repo; save your own with tests/save_fixtures.py
        pytest.skip(f'fixture {f.name} missing')
    return gzip.open(f, 'rt', encoding='utf-8').read()


NOW = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.timezone.utc)  # 2026-10-17 02:00 PT


# ---------- parsing ----------

def test_daily_leaderboard_known_day():  # T2
    rows, info = ph.leaderboard_posts(fx('leaderboard_daily_2026_10_16'))
    assert len(rows) >= 15
    top = rows[0]
    assert top['postId'] == '1274680' and top['name'] == 'Jockey by TwelveLabs' and top['dailyRank'] == 1
    assert top['featuredAt'] == '2026-10-16T00:01:00-07:00' and top['launchDate'] == '2026-10-16'
    assert top['votes'] >= 404 and top['productSlug'] == 'twelvelabs'
    assert top['launchUrl'] == 'https://www.producthunt.com/posts/jockey-by-twelvelabs-2'
    assert 'Artificial Intelligence' in top['topics']
    assert top['thumbnailUrl'].startswith('https://ph-files.imgix.net/')
    ranks = [r['dailyRank'] for r in rows]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)  # 1..N unique
    assert info.get('hasNextPage') is True


def test_weekly_and_monthly():  # T3 / T4 parsing part
    w, _ = ph.leaderboard_posts(fx('leaderboard_weekly_2026_41'))
    assert len(w) >= 15 and all(r['weeklyRank'] for r in w)
    assert len({r['postId'] for r in w}) == len(w)
    assert all('2026-10-05' <= r['launchDate'] <= '2026-10-11' for r in w)
    mo, _ = ph.leaderboard_posts(fx('leaderboard_monthly_2026_9'))
    assert len(mo) >= 15 and all(r['launchDate'].startswith('2026-09') for r in mo)
    assert [r['monthlyRank'] for r in mo] == sorted(r['monthlyRank'] for r in mo)


def test_topic_page():
    topic, rows, info = ph.topic_products(fx('topics_artificial-intelligence'))
    assert topic['slug'] == 'artificial-intelligence' and len(rows) == 15
    assert info['hasNextPage'] and info['totalCount'] > 1000
    assert all(r['productUrl'].startswith('https://www.producthunt.com/products/') for r in rows)
    assert all(r['postId'] for r in rows)


def test_launch_details_redirected_page():  # T6
    d = ph.launch_details(fx('posts_jockey-by-twelvelabs-2'), 'https://www.producthunt.com/products/twelvelabs?launch=jockey-by-twelvelabs-2')
    det, row = d['details'], d['row']
    assert row['postId'] == '1274680' and row['votes'] >= 404
    assert det['websiteUrl'] == 'https://www.twelvelabs.io' and det['websiteDomain'] == 'twelvelabs.io'
    assert any(mk['username'] == 'jaelee_' for mk in det['makers'])
    assert all(set(mk) == {'name', 'username', 'headline', 'profileUrl'} for mk in det['makers'])  # no emails
    assert det['hunter'] == {'name': 'fmerian', 'username': 'fmerian'}
    assert det['categories'] and det['reviewsRating'] == 5 and det['isNoLongerOnline'] is False
    assert det['description']
    d2 = ph.launch_details(fx('posts_jockey-by-twelvelabs-2'), 'https://www.producthunt.com/products/twelvelabs', include_makers=False)
    assert d2['details']['makers'] is None and d2['details']['hunter'] is None


def test_challenge_and_drift():  # T13 / T14 parsing part
    challenge = '<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script></body></html>'
    assert ph.is_challenge(challenge)
    with pytest.raises(ph.SoftBlock):
        ph.leaderboard_posts(challenge)
    with pytest.raises(ph.SoftBlock):  # page format changed: no Apollo state at all
        ph.leaderboard_posts('<html><body><script>self.__next_f.push([1,"x"])</script></body></html>')
    with pytest.raises(ph.SoftBlock):
        ph.launch_details('<html></html>', 'https://www.producthunt.com/products/x')


def test_js_undefined_and_unicode():  # T15
    html = ('<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push({"rehydrate":{"a":{"data":undefined},'
            '"b":{"data":{"homefeedItems":{"edges":[{"node":{"__typename":"Post","id":"1","name":"Café 🚀 日本","slug":"cafe",'
            '"tagline":"say \\"undefined\\"","latestScore":"5","topics":{"edges":[]},"product":{"__typename":"Product","id":"9","slug":"cafe"},'
            '"featuredAt":"2026-01-01T00:01:00-08:00","dailyRank":"1"}}],"pageInfo":{"hasNextPage":false}}}}},"events":[]})</script>')
    rows, _ = ph.leaderboard_posts(html)
    assert rows[0]['name'] == 'Café 🚀 日本' and rows[0]['tagline'] == 'say "undefined"' and rows[0]['votes'] == 5
    json.dumps(rows, ensure_ascii=False)


# ---------- robots, urls, dates ----------

def test_robots_rules():
    r = ph.Robots((FX / 'robots.txt').read_text())
    ok = ['/leaderboard/daily/2026/10/16', '/leaderboard/weekly/2026/41/all', '/posts/x', '/products/x?launch=y', '/topics/ai?page=2', '/r/p/123']
    bad = ['/search?q=a', '/search', '/@jae/upvotes', '/r/ABC', '/posts/x?comment=1', '/products/x?review=2', '/my/settings', '/login', '/auth/x']
    assert all(r.allowed(ph.BASE + u) for u in ok)
    assert not any(r.allowed(ph.BASE + u) for u in bad)
    d = ph.Robots(ph.DEFAULT_ROBOTS)
    assert not d.allowed(ph.BASE + '/search?q=x') and d.allowed(ph.BASE + '/leaderboard/daily/2026/1/1')


def test_leaderboard_urls():
    u = ph.leaderboard_urls('daily', dt.date(2026, 10, 15), dt.date(2026, 10, 16))
    assert [x[0] for x in u] == [ph.BASE + '/leaderboard/daily/2026/10/15', ph.BASE + '/leaderboard/daily/2026/10/16']
    assert ph.leaderboard_urls('weekly', dt.date(2026, 10, 5), dt.date(2026, 10, 11)) == [(ph.BASE + '/leaderboard/weekly/2026/41', 'weekly/2026/41')]
    assert len(ph.leaderboard_urls('monthly', dt.date(2026, 9, 1), dt.date(2026, 10, 31))) == 2


def test_normalize_urls():
    assert ph.normalize_launch_url('producthunt.com/posts/abc') == ph.BASE + '/posts/abc'
    assert ph.normalize_launch_url('https://www.producthunt.com/products/x?launch=y&utm=1') == ph.BASE + '/products/x?launch=y'
    for bad in ['https://example.com/posts/a', 'https://www.producthunt.com/search?q=a', 'https://www.producthunt.com/@user']:
        with pytest.raises(ValueError):
            ph.normalize_launch_url(bad)


def test_parse_input_defaults_and_validation():  # T1 / T10 / T11 / T12
    c = m.parse_input({'mode': 'leaderboard'}, NOW)
    assert c['start'] == c['end'] == dt.date(2026, 10, 16)  # yesterday in PT
    assert m.parse_input({'startDate': '1 day'}, NOW)['start'] == dt.date(2026, 10, 16)
    assert m.parse_input({'startDate': '-7 days'}, NOW)['start'] == dt.date(2026, 10, 10)
    assert m.parse_input({'startDate': '2030-01-01'}, NOW)['start'] == dt.date(2030, 1, 1)
    with pytest.raises(ValueError, match='topic'):
        m.parse_input({'mode': 'topic', 'topics': []}, NOW)
    with pytest.raises(ValueError, match='366'):
        m.parse_input({'startDate': '2025-01-01', 'endDate': '2026-12-31'}, NOW)
    with pytest.raises(ValueError, match='before'):
        m.parse_input({'startDate': '2026-10-10', 'endDate': '2026-10-01'}, NOW)
    with pytest.raises(ValueError, match='URL mode'):
        m.parse_input({'mode': 'urls', 'startUrls': [{'url': 'https://example.com'}]}, NOW)
    with pytest.raises(ValueError):
        m.parse_input({'dedupeKey': 'bad key!'}, NOW)


def test_filters():  # T4 filter part
    rows, _ = ph.leaderboard_posts(fx('leaderboard_monthly_2026_9'))
    cfg = m.parse_input({'minVotes': 500, 'topics': ['developer-tools']}, NOW)
    kept = [r for r in rows if m.passes_filters(r, cfg, True)]
    assert all(r['votes'] >= 500 and 'developer-tools' in r['topicSlugs'] for r in kept)
    cfg = m.parse_input({'keywords': ['AI']}, NOW)
    assert all('ai' in (r['name'] + r['tagline']).lower() for r in rows if m.passes_filters(r, cfg, True))


def test_output_rows_match_dataset_schema():
    schema = json.loads((ROOT / '.actor' / 'actor.json').read_text())['storages']['dataset']['fields']
    cfg = m.parse_input({}, NOW)
    rows, _ = ph.leaderboard_posts(fx('leaderboard_daily_2026_10_16'))
    d = ph.launch_details(fx('posts_jockey-by-twelvelabs-2'), 'https://www.producthunt.com/products/twelvelabs?launch=jockey-by-twelvelabs-2')
    out = [m.finalize_row(r, d['details'] if r['postId'] == '1274680' else None, cfg) for r in rows]
    for o in out:
        jsonschema.validate(o, schema)
        assert 'topicSlugs' not in o and o['source'] == ph.SOURCE
    assert out[0]['websiteUrl'] == 'https://www.twelvelabs.io'
    _, trows, _ = ph.topic_products(fx('topics_artificial-intelligence'))
    for r in trows:
        jsonschema.validate(m.finalize_row(r, None, cfg), schema)
    nomakers = m.finalize_row(rows[0], d['details'], m.parse_input({'includeMakers': False}, NOW))
    assert nomakers['makers'] is None and nomakers['hunter'] is None


# ---------- fetcher: soft-block retry + residential fallback (T13) ----------

class FakeResp:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url = status, text, url
        self.headers = {}
        self.content = text.encode()


class FakeSession:
    def __init__(self, script):
        self.script = script

    async def get(self, url, **kw):
        status, text = self.script.pop(0)
        return FakeResp(status, text, url)

    async def close(self):
        pass


def test_fetcher_retries_soft_blocks_then_falls_back(monkeypatch):
    good = fx('leaderboard_daily_2026_10_16')
    blocks = [(403, 'Just a moment... _cf_chl_opt'), (429, ''), (200, '<html>no state</html>')] * 2
    script = blocks + [(200, good)] * 30
    switched = []

    async def residential():
        switched.append(1)
        return 'RESIDENTIAL'  # stand-in proxy config; _session is faked below

    f = m.Fetcher(None, residential, 2)
    f.interval = f.base_interval = 0

    async def fake_session(i):
        return FakeSession(script)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(f, '_session', fake_session)
    monkeypatch.setattr(m.asyncio, 'sleep', no_sleep)
    parse = lambda h, u: ph.leaderboard_posts(h)  # noqa: E731
    rows, _ = asyncio.run(f.get(ph.BASE + '/leaderboard/daily/2026/10/16', parse, retries=6))
    assert len(rows) >= 15
    assert f.stats['blocks'] == 6 and f.stats['retries'] == 6
    assert switched == [1] and f.on_residential  # residential only after 6 consecutive blocks
    for _ in range(m.RESIDENTIAL_RETRY_DIRECT_AFTER):
        asyncio.run(f.get(ph.BASE + '/leaderboard/daily/2026/10/16', parse))
    assert not f.on_residential and f.proxy_cfg is None  # back to the cheap direct connection


def test_fetcher_no_fallback_for_few_blocks(monkeypatch):
    script = [(403, 'Just a moment... _cf_chl_opt')] * 3 + [(200, fx('leaderboard_daily_2026_10_16'))]
    called = []

    async def residential():
        called.append(1)
        return 'RESIDENTIAL'
    f = m.Fetcher(None, residential, 2)
    f.interval = f.base_interval = 0

    async def fake_session(i):
        return FakeSession(script)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(f, '_session', fake_session)
    monkeypatch.setattr(m.asyncio, 'sleep', no_sleep)
    asyncio.run(f.get(ph.BASE + '/leaderboard/daily/2026/10/16', lambda h, u: ph.leaderboard_posts(h)))
    assert not called and not f.on_residential


def test_fetcher_refuses_disallowed_urls():
    f = m.Fetcher(None, None, 1)
    with pytest.raises(PermissionError):
        asyncio.run(f.get(ph.BASE + '/search?q=ai', lambda h, u: h))
    assert f.disallowed == [ph.BASE + '/search?q=ai']
