"""Product Hunt public-page parsing: extract the server-rendered Apollo state and normalise launches.
Pure functions (no network) so they can be unit-tested against stored HTML fixtures."""
from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

BASE = 'https://www.producthunt.com'
PT = ZoneInfo('America/Los_Angeles')
SOURCE = 'producthunt.com public page'
IMG = 'https://ph-files.imgix.net/'
_APOLLO = re.compile(r'\(window\[Symbol\.for\("ApolloSSRDataTransport"\)\]\s*\?\?=\s*\[\]\)\.push\(')
_SCRIPT = re.compile(r'<script[^>]*>(.*?)</script>', re.S)
CHALLENGE_MARKERS = ('Just a moment...', '_cf_chl_opt', 'cf-chl-', 'challenge-platform')


class SoftBlock(Exception):
    """Challenge page, empty Apollo state or rate-limit: retry with a new session."""


def _js_to_json(s: str) -> str:
    """Replace bare `undefined` (outside strings) with null so the payload parses as JSON."""
    out, i, n, ins = [], 0, len(s), False
    while i < n:
        c = s[i]
        if ins:
            if c == '\\':
                out.append(s[i:i + 2]); i += 2; continue
            if c == '"':
                ins = False
            out.append(c); i += 1; continue
        if c == '"':
            ins = True
        elif c == 'u' and s.startswith('undefined', i):
            out.append('null'); i += 9; continue
        out.append(c); i += 1
    return ''.join(out)


def is_challenge(html: str) -> bool:
    head = html[:20000]
    return any(m in head for m in CHALLENGE_MARKERS) and 'ApolloSSRDataTransport' not in html


def payloads(html: str) -> list:
    out = []
    for s in _SCRIPT.findall(html):
        m = _APOLLO.search(s[:200])
        if not m:
            continue
        body = s[m.end():].rstrip().rstrip(';').rstrip()
        if body.endswith(')'):
            body = body[:-1]
        try:
            out.append(json.loads(body))
        except json.JSONDecodeError:
            try:
                out.append(json.loads(_js_to_json(body)))
            except json.JSONDecodeError:
                continue
    return out


def walk(o, fn):
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            fn(x)
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def index(ps: list) -> dict:
    """Merge every normalised object by (__typename, id). Later non-null values win; nulls never overwrite."""
    objs: dict = {}

    def f(d):
        t, i = d.get('__typename'), d.get('id')
        if t and i is not None:
            cur = objs.setdefault((t, str(i)), {})
            for k, v in d.items():
                if v is not None or k not in cur:
                    cur[k] = v
    walk(ps, f)
    return objs


def query_results(ps: list, key: str) -> list:
    """All `data[key]` values from rehydrated Apollo queries (e.g. 'homefeedItems', 'topic', 'post', 'product')."""
    out = []
    for p in ps:
        for v in (p.get('rehydrate') or {}).values():
            d = (v or {}).get('data') if isinstance(v, dict) else None
            if isinstance(d, dict) and d.get(key):
                out.append(d[key])
        for e in p.get('events') or []:
            d = ((e.get('value') or {}).get('data')) if isinstance(e.get('value'), dict) else None
            if isinstance(d, dict) and d.get(key):
                out.append(d[key])
    return out


def _int(v):
    try:
        return int(v) if v is not None and str(v).strip() != '' else None
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _topics(node) -> list[dict]:
    t = (node or {}).get('topics') or {}
    edges = t.get('edges') if isinstance(t, dict) else None
    out = []
    for e in edges or []:
        n = (e or {}).get('node') or {}
        if n.get('name'):
            out.append({'name': n['name'], 'slug': n.get('slug')})
    return out


def launch_date(featured_at: str | None) -> str | None:
    if not featured_at:
        return None
    try:
        return dt.datetime.fromisoformat(featured_at).astimezone(PT).date().isoformat()
    except ValueError:
        return featured_at[:10]


def post_row(p: dict, objs: dict | None = None) -> dict:
    """Basic launch row from a Post node (leaderboard or launch page)."""
    objs = objs or {}
    full = {**objs.get(('Post', str(p.get('id'))), {}), **{k: v for k, v in p.items() if v is not None}}
    prod = full.get('product') or {}
    if prod.get('id'):
        prod = {**objs.get(('Product', str(prod['id'])), {}), **{k: v for k, v in prod.items() if v is not None}}
    slug = full.get('slug')
    pslug = prod.get('slug')
    topics = _topics(full)
    thumb = full.get('thumbnailImageUuid')
    return {
        'postId': str(full.get('id')), 'name': full.get('name'), 'tagline': full.get('tagline'), 'slug': slug,
        'launchUrl': f'{BASE}/posts/{slug}' if slug else None,
        'productSlug': pslug, 'productUrl': f'{BASE}/products/{pslug}' if pslug else None,
        'launchDate': launch_date(full.get('featuredAt') or full.get('scheduledAt') or full.get('createdAt')),
        'featuredAt': full.get('featuredAt'),
        'votes': None if full.get('hideVotesCount') else _int(full.get('latestScore')),
        'launchDayVotes': None if full.get('hideVotesCount') else _int(full.get('launchDayScore')),
        'commentsCount': _int(full.get('commentsCount')),
        'dailyRank': _int(full.get('dailyRank')), 'weeklyRank': _int(full.get('weeklyRank')), 'monthlyRank': _int(full.get('monthlyRank')),
        'topics': [t['name'] for t in topics], 'topicSlugs': [t['slug'] for t in topics if t.get('slug')],
        'thumbnailUrl': IMG + thumb if thumb else None,
    }


def leaderboard_posts(html: str) -> tuple[list[dict], dict]:
    """Rows from a /leaderboard/... page (server-rendered first page, ~17-20 launches), plus pageInfo."""
    if is_challenge(html):
        raise SoftBlock('challenge page')
    ps = payloads(html)
    if not ps:
        raise SoftBlock('no Apollo state in page')
    objs = index(ps)
    rows, seen, info = [], set(), {}
    for feed in query_results(ps, 'homefeedItems'):
        info = feed.get('pageInfo') or info
        for e in feed.get('edges') or []:
            n = (e or {}).get('node') or {}
            if n.get('__typename') != 'Post' or not n.get('id') or str(n['id']) in seen:
                continue
            seen.add(str(n['id']))
            rows.append(post_row(n, objs))
    return rows, info


def topic_products(html: str) -> tuple[dict, list[dict], dict]:
    """(topic, product rows, pageInfo) from a /topics/<slug>?page=N page (15 products per page, highest rated first)."""
    if is_challenge(html):
        raise SoftBlock('challenge page')
    ps = payloads(html)
    if not ps:
        raise SoftBlock('no Apollo state in page')
    objs = index(ps)
    topic, rows, info, seen = {}, [], {}, set()
    for t in query_results(ps, 'topic'):
        if not isinstance(t, dict) or 'products' not in t:
            continue
        topic = {'name': t.get('name'), 'slug': t.get('slug')}
        prods = t.get('products') or {}
        info = prods.get('pageInfo') or info
        info = {**info, 'totalCount': prods.get('totalCount')}
        for e in prods.get('edges') or []:
            n = (e or {}).get('node') or {}
            if n.get('__typename') != 'Product' or not n.get('slug') or n['slug'] in seen:
                continue
            seen.add(n['slug'])
            ll = n.get('latestLaunch') or {}
            llf = objs.get(('Post', str(ll.get('id'))), {}) if ll.get('id') else {}
            logo = n.get('logoUuid')
            rows.append({
                'postId': str(ll['id']) if ll.get('id') else None, 'name': n.get('name'), 'tagline': n.get('tagline'),
                'slug': llf.get('slug'), 'launchUrl': f"{BASE}/posts/{llf['slug']}" if llf.get('slug') else None,
                'productSlug': n['slug'], 'productUrl': f"{BASE}/products/{n['slug']}",
                'launchDate': launch_date(ll.get('scheduledAt') or llf.get('featuredAt')),
                'featuredAt': llf.get('featuredAt'),
                'votes': None, 'launchDayVotes': None, 'commentsCount': None,
                'dailyRank': None, 'weeklyRank': None, 'monthlyRank': None,
                'topics': [topic['name']] if topic.get('name') else [], 'topicSlugs': [topic['slug']] if topic.get('slug') else [],
                'thumbnailUrl': IMG + logo if logo else None,
                'categories': [c.get('name') for c in n.get('categories') or [] if c.get('name')],
                'reviewsRating': _num(n.get('reviewsRating')), 'reviewsCount': _int(n.get('reviewsCount')),
                'isNoLongerOnline': n.get('isNoLongerOnline'),
                'productPostsCount': _int(n.get('postsCount')),
            })
    return topic, rows, info


def _maker(u: dict) -> dict:
    un = u.get('username')
    return {'name': u.get('name'), 'username': un, 'headline': u.get('headline'),
            'profileUrl': f'{BASE}/@{un}' if un else None}


def _meta(html: str, name: str) -> str | None:
    m = re.search(r'<meta[^>]+(?:name|property)="' + re.escape(name) + r'"[^>]+content="([^"]*)"', html[:200000])
    if not m:
        return None
    import html as _h
    return _h.unescape(m.group(1))


def launch_details(html: str, final_url: str, launch_slug: str | None = None, include_makers: bool = True) -> dict:
    """Details from a /products/<slug>?launch=<slug> (or /posts/<slug> after redirect) page.
    Returns {'row': basic post row or None, 'details': {...}}."""
    if is_challenge(html):
        raise SoftBlock('challenge page')
    ps = payloads(html)
    if not ps:
        raise SoftBlock('no Apollo state in page')
    objs = index(ps)
    q = parse_qs(urlsplit(final_url).query)
    launch_slug = launch_slug or (q.get('launch') or [None])[0]
    path = urlsplit(final_url).path.rstrip('/').split('/')
    prod_slug = path[2] if len(path) > 2 and path[1] == 'products' else None
    product = next((o for (t, _), o in objs.items() if t == 'Product' and prod_slug and o.get('slug') == prod_slug and 'websiteUrl' in o), None)
    if product is None:
        product = next((o for (t, _), o in objs.items() if t == 'Product' and 'websiteUrl' in o), None)
    post = None
    if launch_slug:
        post = next((o for (t, _), o in objs.items() if t == 'Post' and o.get('slug') == launch_slug), None)
    if post is None and product:
        pv = (product.get('focusedPageVariant') or product.get('pageVariant') or {}).get('launch') or product.get('latestLaunch') or {}
        if pv.get('id'):
            post = objs.get(('Post', str(pv['id'])))
    if product is None and post is None:
        raise SoftBlock('no Product/Post in page')
    product = product or {}
    if post and post.get('product', {}).get('id') and not product:
        product = objs.get(('Product', str(post['product']['id'])), {})
    row = post_row(post, objs) if post and post.get('name') else None
    makers = []
    if include_makers and post:
        for u in post.get('makers') or []:
            if isinstance(u, dict):
                u = {**objs.get(('User', str(u.get('id'))), {}), **{k: v for k, v in u.items() if v is not None}}
                makers.append(_maker(u))
    hunter = None
    if include_makers and post and isinstance(post.get('user'), dict):
        u = post['user']
        hunter = {'name': u.get('name'), 'username': u.get('username')}
    website = product.get('websiteUrl') or ((post or {}).get('primaryLink') or {}).get('url')
    desc = (post or {}).get('description') or product.get('description') or _meta(html, 'description')
    details = {
        'productName': product.get('name'), 'productTagline': product.get('tagline'),
        'websiteUrl': website,
        'websiteDomain': product.get('websiteDomain') or product.get('cleanUrl') or (urlsplit(website).hostname if website else None),
        'launchLink': ((post or {}).get('primaryLink') or {}).get('url'),
        'description': desc,
        'categories': [c.get('name') for c in product.get('categories') or [] if isinstance(c, dict) and c.get('name')],
        'reviewsRating': _num(product.get('reviewsRating')), 'reviewsCount': _int(product.get('reviewsCount')),
        'followersCount': _int(product.get('followersCount')),
        'productLaunchesCount': _int(product.get('postsCount')),
        'isNoLongerOnline': product.get('isNoLongerOnline'),
        'githubUrl': product.get('githubUrl'), 'twitterUrl': product.get('twitterUrl'), 'linkedinUrl': product.get('linkedinUrl'),
        'makers': makers if include_makers else None, 'hunter': hunter,
    }
    if row and not row.get('productSlug'):
        row['productSlug'] = product.get('slug')
        row['productUrl'] = f"{BASE}/products/{product['slug']}" if product.get('slug') else None
    return {'row': row, 'details': details}


# ---------- URLs, dates, robots ----------

def leaderboard_urls(period: str, start: dt.date, end: dt.date) -> list[tuple[str, str]]:
    """[(url, label)] for every daily/weekly/monthly leaderboard covering start..end (inclusive)."""
    out, seen = [], set()
    d = start
    while d <= end:
        if period == 'daily':
            key = f'daily/{d.year}/{d.month}/{d.day}'
        elif period == 'weekly':
            y, w, _ = d.isocalendar()
            key = f'weekly/{y}/{w}'
        else:
            key = f'monthly/{d.year}/{d.month}'
        if key not in seen:
            seen.add(key)
            out.append((f'{BASE}/leaderboard/{key}', key))
        d += dt.timedelta(days=1)
    return out


_REL = re.compile(r'^\s*[-+]?\s*(\d+)\s*(day|week|month|year)s?(\s+ago)?\s*$', re.I)


def parse_date(v, today: dt.date) -> dt.date | None:
    """YYYY-MM-DD or relative ('1 day', '2 weeks', 'today', 'yesterday') -> date in PT."""
    if v in (None, ''):
        return None
    s = str(v).strip()
    if s.lower() == 'today':
        return today
    if s.lower() == 'yesterday':
        return today - dt.timedelta(days=1)
    m = _REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {'day': 1, 'week': 7, 'month': 30, 'year': 365}[unit] * n
        return today - dt.timedelta(days=days)
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError(f'Invalid date "{s}". Use YYYY-MM-DD or a relative value like "7 days".') from None


def normalize_launch_url(u: str) -> str:
    """Accept /posts/<slug>, /products/<slug>[?launch=], producthunt.com without www. Raise on anything else."""
    s = str(u).strip()
    if not s:
        raise ValueError('empty URL')
    if not re.match(r'^https?://', s):
        s = 'https://' + s.lstrip('/')
    p = urlsplit(s)
    host = (p.hostname or '').lower()
    if host not in ('producthunt.com', 'www.producthunt.com'):
        raise ValueError(f'not a Product Hunt URL: {u}')
    parts = [x for x in p.path.split('/') if x]
    if len(parts) >= 2 and parts[0] in ('posts', 'products'):
        url = f'{BASE}/{parts[0]}/{parts[1]}'
        launch = (parse_qs(p.query).get('launch') or [None])[0]
        if parts[0] == 'products' and launch and re.match(r'^[\w-]+$', launch):
            url += f'?launch={launch}'
        return url
    raise ValueError(f'unsupported Product Hunt URL (use /posts/<slug> or /products/<slug>): {u}')


class Robots:
    """Minimal robots.txt matcher for User-agent: * (supports * and $; longest match wins, Allow wins ties)."""

    def __init__(self, text: str):
        self.rules: list[tuple[bool, re.Pattern, int]] = []
        groups, cur, star = [], None, False
        for raw in text.splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or ':' not in line:
                continue
            k, v = (x.strip() for x in line.split(':', 1))
            k = k.lower()
            if k == 'user-agent':
                if cur is None or cur['rules']:
                    cur = {'agents': [], 'rules': []}
                    groups.append(cur)
                cur['agents'].append(v.lower())
            elif k in ('allow', 'disallow') and cur is not None and v:
                cur['rules'].append((k == 'allow', v))
        for g in groups:
            if '*' in g['agents']:
                star = True
                for allow, pat in g['rules']:
                    rx = '^' + re.escape(pat).replace(r'\*', '.*').replace(r'\$', '$')
                    self.rules.append((allow, re.compile(rx), len(pat)))
        self.loaded = star or not groups

    def allowed(self, url: str) -> bool:
        p = urlsplit(url)
        path = (p.path or '/') + (('?' + p.query) if p.query else '')
        best = None
        for allow, rx, ln in self.rules:
            if rx.match(path) and (best is None or ln > best[1] or (ln == best[1] and allow)):
                best = (allow, ln)
        return True if best is None else best[0]


DEFAULT_ROBOTS = """User-agent: *
Disallow: /auth/*
Disallow: /search*
Disallow: /*?*comment=*
Disallow: /*?*review=*
Disallow: /my/*
Disallow: /yours*
Disallow: /notifications
Disallow: /@*/*
Disallow: /login$
Disallow: /r/*
Allow: /r/p/*
"""
