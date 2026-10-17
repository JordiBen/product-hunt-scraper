"""Apify entrypoint: Product Hunt launches from public, logged-out pages (leaderboards, topics, launch URLs).
Plain HTTP with a browser TLS fingerprint (no headless browser), robots.txt respected, no logins, no emails."""
from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
import time

from apify import Actor
from curl_cffi.requests import AsyncSession

from . import ph

LAUNCH_EVENT = 'launch'
DETAILS_EVENT = 'launch-details'
DEDUPE_STORE = 'product-hunt-scraper-dedupe'
MAX_SEEN = 50_000
MAX_RETRIES = 4
FALLBACK_AFTER_BLOCKS = 6        # consecutive blocks (each with a fresh session + backoff) before residential
RESIDENTIAL_RETRY_DIRECT_AFTER = 25  # successful residential requests before trying direct again
RESIDENTIAL_USD_PER_GB = 8.0
WIRE_CORRECTION = 1.5            # our wire-size estimate vs. billed proxy bytes (measured 2026-10-17)
EST_REVENUE = {'launch': 0.0012, 'launch-details': 0.0013}  # BRONZE prices, for the cost guard only
LIST_RETRIES = 6  # leaderboard/topic pages matter most: retry longer (~1 min)
TOPIC_PAGE_SIZE = 15
MAX_TOPIC_PAGES = 67  # ~1,000 products per topic
DETAIL_FIELDS = ('websiteUrl', 'websiteDomain', 'launchLink', 'description', 'categories', 'reviewsRating', 'reviewsCount',
                 'followersCount', 'productLaunchesCount', 'isNoLongerOnline', 'githubUrl', 'twitterUrl', 'linkedinUrl',
                 'makers', 'hunter')


def today_pt(now: dt.datetime | None = None) -> dt.date:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(ph.PT).date()


def parse_input(inp: dict, now: dt.datetime | None = None) -> dict:
    mode = inp.get('mode') or 'leaderboard'
    if mode not in ('leaderboard', 'topic', 'urls'):
        raise ValueError('mode must be "leaderboard", "topic" or "urls".')
    period = inp.get('period') or 'daily'
    if period not in ('daily', 'weekly', 'monthly'):
        raise ValueError('period must be daily, weekly or monthly.')
    today = today_pt(now)
    clean = lambda xs: [str(x).strip() for x in (xs or []) if str(x).strip()]  # noqa: E731
    topics = [t.lower() for t in clean(inp.get('topics'))]
    cfg = {
        'mode': mode, 'period': period, 'today': today, 'topics': topics,
        'keywords': [k.lower() for k in clean(inp.get('keywords'))],
        'minVotes': int(inp.get('minVotes') or 0),
        'featuredOnly': bool(inp.get('featuredOnly', True)),
        'includeDetails': bool(inp.get('includeDetails', True)),
        'includeMakers': bool(inp.get('includeMakers', True)),
        'onlyNew': bool(inp.get('onlyNewSinceLastRun', False)),
        'dedupeKey': str(inp.get('dedupeKey') or 'default'),
        'maxItems': int(inp.get('maxItems', 100) or 0),
        'concurrency': int(min(max(int(inp.get('maxConcurrency') or 5), 1), 20)),
        'proxy': inp.get('proxyConfiguration') or {'useApifyProxy': False},
        'allowResidential': bool(inp.get('allowResidentialFallback', True)),
        'urls': [], 'start': None, 'end': None,
    }
    if not re.match(r'^[a-zA-Z0-9-]{1,40}$', cfg['dedupeKey']):
        raise ValueError('dedupeKey may contain only letters, digits and dashes (max 40).')
    if cfg['maxItems'] < 0:
        raise ValueError('maxItems must be >= 0.')
    if mode == 'leaderboard':
        start = ph.parse_date(inp.get('startDate'), today) or (today - dt.timedelta(days=1))
        end = ph.parse_date(inp.get('endDate'), today) or start
        if end < start:
            raise ValueError(f'endDate ({end}) is before startDate ({start}).')
        if (end - start).days > 366:
            raise ValueError(f'Date range {start}..{end} is longer than 366 days. Split it into several runs.')
        cfg['start'], cfg['end'] = start, end
    elif mode == 'topic':
        if not topics:
            raise ValueError('Topic mode needs at least one topic slug in "topics" (e.g. artificial-intelligence).')
        bad = [t for t in topics if not re.match(r'^[a-z0-9-]{1,80}$', t)]
        if bad:
            raise ValueError(f'Invalid topic slug(s): {bad}. Use the slug from the topic URL, e.g. developer-tools.')
    else:
        raw = [s.get('url') if isinstance(s, dict) else s for s in (inp.get('startUrls') or [])]
        urls, errs = [], []
        for u in raw:
            if not u or not str(u).strip():
                continue
            try:
                n = ph.normalize_launch_url(u)
                if n not in urls:
                    urls.append(n)
            except ValueError as e:
                errs.append(str(e))
        if not urls:
            raise ValueError('URL mode needs at least one https://www.producthunt.com/posts/<slug> or /products/<slug> URL.'
                             + (f' Rejected: {errs[:3]}' if errs else ''))
        cfg['urls'], cfg['urlErrors'] = urls, errs
    return cfg


def passes_filters(row: dict, cfg: dict, use_topic_filter: bool) -> bool:
    if cfg['minVotes'] and (row.get('votes') is None or row['votes'] < cfg['minVotes']):
        return False
    if use_topic_filter and cfg['topics']:
        have = {s.lower() for s in row.get('topicSlugs') or []} | {n.lower() for n in row.get('topics') or []}
        if not any(t in have or t.replace('-', ' ') in have for t in cfg['topics']):
            return False
    if cfg['keywords']:
        text = f"{row.get('name') or ''} {row.get('tagline') or ''}".lower()
        if not any(k in text for k in cfg['keywords']):
            return False
    return True


def finalize_row(row: dict, details: dict | None, cfg: dict, err: str | None = None) -> dict:
    out = dict(row)
    for k in DETAIL_FIELDS:
        out.setdefault(k, None)
    if details:
        for k in DETAIL_FIELDS:
            v = details.get(k)
            if v not in (None, [], '') or out.get(k) in (None, []):
                out[k] = v
    if not cfg['includeMakers']:
        out['makers'], out['hunter'] = None, None
    if err:
        out['detailsError'] = err
    out.pop('topicSlugs', None)
    out['scrapedAt'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    out['source'] = ph.SOURCE
    return out


class Fetcher:
    """Polite HTTP fetcher: browser TLS fingerprint, session pool, retries with backoff, Retry-After,
    global request pacing, optional proxy with automatic residential fallback after repeated blocks."""

    def __init__(self, proxy_cfg, residential_cfg_factory, concurrency: int, rpm: int = 60):
        self.proxy_cfg = proxy_cfg
        self.base_proxy_cfg = proxy_cfg
        self.res_successes = 0
        self.base_interval = 60.0 / rpm
        self.residential_factory = residential_cfg_factory
        self.on_residential = False
        self.sem = asyncio.Semaphore(concurrency)
        self.interval = 60.0 / rpm
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.sessions: list[AsyncSession] = []
        self.pool = max(2, min(concurrency, 20))
        self.consecutive_blocks = 0
        self.stats = {'requests': 0, 'retries': 0, 'blocks': 0, 'sessionsRetired': 0, 'bytes': 0, 'residentialBytes': 0,
                      'residentialFallback': False, 'status': {}}
        self.robots = ph.Robots(ph.DEFAULT_ROBOTS)
        self.disallowed: list[str] = []

    async def _session(self, i: int) -> AsyncSession:
        while len(self.sessions) <= i:
            self.sessions.append(None)
        if self.sessions[i] is None:
            proxy = None
            if self.proxy_cfg is not None:
                proxy = await self.proxy_cfg.new_url(session_id=f's{i}_{random.randint(0, 10**8)}')
            self.sessions[i] = AsyncSession(impersonate='chrome', timeout=45, proxy=proxy,
                                            headers={'Accept-Language': 'en-US,en;q=0.9'})
        return self.sessions[i]

    async def _retire(self, i: int, count: bool = True) -> None:
        s = self.sessions[i] if i < len(self.sessions) else None
        if s is not None:
            self.sessions[i] = None
            self.stats['sessionsRetired'] += int(count)
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass

    async def _pace(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def _maybe_fallback(self) -> None:
        # Slow down first; residential proxy (paid per GB) only after repeated blocks with fresh sessions.
        self.interval = min(self.interval * 1.5, 5.0)
        if self.consecutive_blocks >= FALLBACK_AFTER_BLOCKS and not self.on_residential and self.residential_factory is not None:
            cfg = await self.residential_factory()
            if cfg is not None:
                Actor.log.warning('Repeated blocks: switching to the RESIDENTIAL proxy group for the rest of the run.')
                self.proxy_cfg, self.on_residential = cfg, True
                self.res_successes = 0
                self.stats['residentialFallback'] = True
                self.stats['residentialSwitches'] = self.stats.get('residentialSwitches', 0) + 1
                for i in range(len(self.sessions)):
                    await self._retire(i)
                self.consecutive_blocks = 0

    async def _back_to_direct(self) -> None:
        """Blocks are usually temporary rate limits: go back to the cheap direct connection after a while."""
        self.proxy_cfg, self.on_residential, self.res_successes = self.base_proxy_cfg, False, 0
        for i in range(len(self.sessions)):
            await self._retire(i, count=False)

    def residential_cost_usd(self) -> float:
        return self.stats['residentialBytes'] * WIRE_CORRECTION / 1e9 * RESIDENTIAL_USD_PER_GB

    async def load_robots(self) -> None:
        try:
            s = await self._session(0)
            r = await s.get(ph.BASE + '/robots.txt')
            self.stats['requests'] += 1
            if r.status_code == 200 and 'user-agent' in r.text.lower() and not ph.is_challenge(r.text):
                self.robots = ph.Robots(r.text)
        except Exception as e:  # noqa: BLE001
            Actor.log.info(f'robots.txt not loaded ({e}); using the built-in copy of its rules.')

    async def get(self, url: str, parse, retries: int = MAX_RETRIES):
        """GET url and return parse(html, final_url). Retries on network errors, 403/429/5xx and SoftBlock."""
        if not self.robots.allowed(url):
            self.disallowed.append(url)
            raise PermissionError(f'disallowed by robots.txt: {url}')
        last = None
        async with self.sem:
            for attempt in range(retries + 1):
                i = random.randrange(self.pool)
                try:
                    await self._pace()
                    s = await self._session(i)
                    r = await s.get(url, allow_redirects=True, max_redirects=5)
                    self.stats['requests'] += 1
                    st = self.stats['status']
                    st[str(r.status_code)] = st.get(str(r.status_code), 0) + 1
                    size = int(r.headers.get('content-length') or 0) or len(r.content) // 8  # wire size estimate
                    self.stats['bytes'] += size
                    if self.on_residential:
                        self.stats['residentialBytes'] += size
                    final = str(r.url)
                    if not self.robots.allowed(final):
                        raise PermissionError(f'redirected to a robots-disallowed URL: {final}')
                    if r.status_code == 404:
                        raise FileNotFoundError(f'HTTP 404: {url}')
                    if r.status_code in (403, 429, 503) or ph.is_challenge(r.text):
                        ra = r.headers.get('retry-after')
                        raise ph.SoftBlock(f'HTTP {r.status_code}' + (f' retry-after {ra}' if ra else ''))
                    if r.status_code >= 400:
                        raise ConnectionError(f'HTTP {r.status_code}')
                    result = parse(r.text, final)
                    self.consecutive_blocks = 0
                    self.interval = max(self.base_interval, self.interval * 0.95)
                    if self.on_residential:
                        self.res_successes += 1
                        if self.res_successes >= RESIDENTIAL_RETRY_DIRECT_AFTER:
                            await self._back_to_direct()
                    return result
                except (PermissionError, FileNotFoundError):
                    raise
                except ph.SoftBlock as e:
                    last = e
                    self.stats['blocks'] += 1
                    self.consecutive_blocks += 1
                    await self._retire(i)
                    await self._maybe_fallback()
                    delay = min(2 ** attempt, 20) + random.random()
                    ra = str(e).split('retry-after ')[-1] if 'retry-after' in str(e) else None
                    if ra and ra.isdigit():
                        delay = max(delay, min(int(ra), 60))
                except Exception as e:  # noqa: BLE001
                    last = e
                    await self._retire(i)
                    delay = 2 ** attempt + random.random()
                if attempt < retries:
                    self.stats['retries'] += 1
                    await asyncio.sleep(delay)
        raise RuntimeError(f'failed after {retries + 1} attempts: {type(last).__name__}: {last}')

    async def close(self) -> None:
        for i in range(len(self.sessions)):
            await self._retire(i, count=False)


async def make_proxy(cfg: dict):
    p = cfg['proxy'] or {}
    if not p.get('useApifyProxy') and not p.get('proxyUrls'):
        return None
    return await Actor.create_proxy_configuration(actor_proxy_input=p)


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        try:
            cfg = parse_input(inp)
        except ValueError as e:
            await Actor.fail(status_message=f'Invalid input: {e}')
            return

        async def residential():
            if not cfg['allowResidential'] or not Actor.is_at_home():
                return None
            try:
                return await Actor.create_proxy_configuration(groups=['RESIDENTIAL'])
            except Exception as e:  # noqa: BLE001
                Actor.log.warning(f'Residential proxy unavailable: {e}')
                return None

        fetcher = Fetcher(await make_proxy(cfg), residential, cfg['concurrency'])
        await fetcher.load_robots()

        seen: set[str] = set()
        store = None
        if cfg['onlyNew']:
            store = await Actor.open_key_value_store(name=DEDUPE_STORE)
            seen = set(await store.get_value(f"seen-{cfg['dedupeKey']}") or [])

        stats = {'pages': 0, 'pagesFailed': 0, 'pagesEmpty': 0, 'candidates': 0, 'skippedSeen': 0, 'pushed': 0,
                 'details': 0, 'detailsFailed': 0}
        stop = asyncio.Event()
        stop_reason = ''
        cap = cfg['maxItems'] or 10**9
        delivered_ids: list[str] = []
        candidates: list[tuple[dict, str | None]] = []  # (row, details url)
        cand_ids: set[str] = set()

        def add_candidate(row: dict, durl: str | None) -> bool:
            key = row.get('postId') or row.get('productSlug')
            if not key or key in cand_ids:
                return False
            if cfg['onlyNew'] and key in seen:
                stats['skippedSeen'] += 1
                return False
            cand_ids.add(key)
            candidates.append((row, durl))
            stats['candidates'] += 1
            return True

        status_note = ''
        # ---------- 1) discovery ----------
        if cfg['mode'] == 'leaderboard':
            end = min(cfg['end'], cfg['today'])
            pages = ph.leaderboard_urls(cfg['period'], cfg['start'], end) if cfg['start'] <= end else []
            if not cfg['featuredOnly']:
                pages = [(u + '/all', k) for u, k in pages]
            if not pages:
                status_note = f"No leaderboard yet for {cfg['start']} (Pacific Time, today is {cfg['today']})."
            batch = max(cfg['concurrency'], 1)
            for b in range(0, len(pages), batch):
                if len(candidates) >= cap:
                    break
                chunk = pages[b:b + batch]
                res = await asyncio.gather(*(fetcher.get(u, lambda h, f: ph.leaderboard_posts(h), LIST_RETRIES) for u, _ in chunk),
                                           return_exceptions=True)
                for (u, label), r in zip(chunk, res):
                    stats['pages'] += 1
                    if isinstance(r, Exception):
                        stats['pagesFailed'] += 1
                        Actor.log.warning(f'Leaderboard {label} failed: {r}')
                        continue
                    rows, _ = r
                    if not rows:
                        stats['pagesEmpty'] += 1
                    for row in rows:
                        if passes_filters(row, cfg, use_topic_filter=True) and len(candidates) < cap:
                            add_candidate(row, row['launchUrl'] or row['productUrl'])
                await Actor.set_status_message(f"Leaderboards {stats['pages']}/{len(pages)}, launches found {len(candidates)}")
        elif cfg['mode'] == 'topic':
            per_topic_pages = min(MAX_TOPIC_PAGES, max(1, -(-cap // TOPIC_PAGE_SIZE) if cfg['maxItems'] else MAX_TOPIC_PAGES))
            for t in cfg['topics']:
                for page in range(1, per_topic_pages + 1):
                    if len(candidates) >= cap:
                        break
                    url = f'{ph.BASE}/topics/{t}' + (f'?page={page}' if page > 1 else '')
                    stats['pages'] += 1
                    try:
                        topic, rows, info = await fetcher.get(url, lambda h, f: ph.topic_products(h), LIST_RETRIES)
                    except FileNotFoundError:
                        stats['pagesFailed'] += 1
                        await Actor.push_data({'inputTopic': t, 'error': f'Topic "{t}" not found on Product Hunt'})
                        break
                    except Exception as e:  # noqa: BLE001
                        stats['pagesFailed'] += 1
                        Actor.log.warning(f'Topic {t} page {page} failed: {e}')
                        break
                    if not rows:
                        stats['pagesEmpty'] += 1
                        break
                    for row in rows:
                        if passes_filters(row, cfg, use_topic_filter=False) and len(candidates) < cap:
                            add_candidate(row, row['productUrl'])
                    if not info.get('hasNextPage'):
                        break
                    await Actor.set_status_message(f'Topic {t}: page {page}, products found {len(candidates)}')
        else:
            for u in cfg['urls'][:cap]:
                candidates.append(({}, u))
            for e in cfg.get('urlErrors') or []:
                await Actor.push_data({'error': e})

        # ---------- 2) details + push ----------
        push_lock = asyncio.Lock()
        try:
            ppe = bool(Actor.get_charging_manager().get_pricing_info().is_pay_per_event)
        except Exception:  # noqa: BLE001
            ppe = False

        def budget_left(event: str) -> int | None:
            try:
                return Actor.get_charging_manager().calculate_max_event_charge_count_within_limit(event)
            except Exception:  # noqa: BLE001
                return None

        async def emit(row: dict, details: dict | None, err: str | None) -> None:
            nonlocal stop_reason
            async with push_lock:
                if stop.is_set() or stats['pushed'] >= cap:
                    return
                out = finalize_row(row, details if cfg['includeDetails'] else None, cfg, err)
                r = await Actor.push_data(out, charged_event_name=LAUNCH_EVENT)
                if ppe and r.charged_count == 0:  # nothing pushed: the user's max charge is used up
                    stop.set(); stop_reason = 'max charge reached'
                    return
                stats['pushed'] += 1
                key = out.get('postId') or out.get('productSlug')
                if key:
                    delivered_ids.append(key)
                if details and cfg['includeDetails'] and not err:
                    c = await Actor.charge(DETAILS_EVENT)
                    stats['details'] += 1
                    if c.event_charge_limit_reached:
                        stop.set(); stop_reason = 'max charge reached'
                if getattr(r, 'event_charge_limit_reached', False):
                    stop.set(); stop_reason = 'max charge reached'
                if stats['pushed'] % 10 == 0:
                    await Actor.set_status_message(f"Launches saved {stats['pushed']}/{min(len(candidates), cap)}")

        fsem = asyncio.Semaphore(cfg['concurrency'])
        guard = {'detailsPaused': False}

        async def prepare(row: dict, durl: str | None):
            """Fetch details (if needed) and return a coroutine factory that saves the row, or None."""
            if stop.is_set():
                return None
            url_mode = cfg['mode'] == 'urls'
            need_details = (cfg['includeDetails'] or url_mode) and durl
            if need_details and not url_mode:
                left = budget_left(DETAILS_EVENT)
                if left is not None and left < 1:
                    need_details = False
                if guard['detailsPaused']:
                    need_details = False
                elif fetcher.stats['residentialBytes'] > 3e6:
                    revenue = 0.8 * (stats['pushed'] * EST_REVENUE['launch'] + stats['details'] * EST_REVENUE['launch-details'])
                    if fetcher.residential_cost_usd() > 0.6 * revenue:
                        guard['detailsPaused'] = True
                        need_details = False
                        Actor.log.warning('Residential proxy cost is too high for this run: details paused, listing data continues.')
                        await Actor.set_status_message('Product Hunt is blocking; details paused to cap proxy cost, listing continues')
            details, err = None, None
            if need_details:
                try:
                    async with fsem:
                        d = await fetcher.get(durl, lambda h, f: ph.launch_details(h, f, (row.get('slug') or None), cfg['includeMakers']))
                    details = d['details']
                    if d['row']:
                        base = d['row']
                        # leaderboard/topic rows keep their own ranks; launch page fills gaps
                        row = {**base, **{k: v for k, v in row.items() if v not in (None, []) and not k.startswith('_')}}
                except FileNotFoundError as e:
                    err = str(e)
                except Exception as e:  # noqa: BLE001
                    err = f'{type(e).__name__}: {e}'[:300]
                if err:
                    stats['detailsFailed'] += 1
            if url_mode:
                if not details:
                    msg = err or 'no launch data found'
                    return lambda: Actor.push_data({'inputUrl': durl, 'error': msg})
                if not row.get('postId'):  # product page without an identifiable launch
                    slug = durl.split('/products/')[-1].split('?')[0] if '/products/' in durl else None
                    row = {'postId': None, 'name': details.get('productName'), 'tagline': details.get('productTagline'),
                           'productSlug': slug, 'productUrl': f'{ph.BASE}/products/{slug}' if slug else None}
                row = {'inputUrl': durl, **row}
                if not passes_filters(row, cfg, use_topic_filter=False):
                    return None
            e = err if cfg['includeDetails'] else None
            return lambda: emit(row, details, e)

        # Details are fetched in parallel, rows are saved in the original (rank) order chunk by chunk.
        chunk = max(cfg['concurrency'] * 4, 10)
        todo = candidates[:cap]
        for b in range(0, len(todo), chunk):
            if stop.is_set():
                break
            prepared = await asyncio.gather(*(prepare(r, u) for r, u in todo[b:b + chunk]))
            for p in prepared:
                if stop.is_set():
                    break
                if p is not None:
                    await p()
        await fetcher.close()

        if store is not None and delivered_ids:
            keep = list(dict.fromkeys(delivered_ids + [s for s in seen if s not in set(delivered_ids)]))[:MAX_SEEN]
            await store.set_value(f"seen-{cfg['dedupeKey']}", keep)

        summary = {**stats, 'detailsPaused': guard['detailsPaused'], 'residentialCostUsdEst': round(fetcher.residential_cost_usd(), 4), 'http': {k: v for k, v in fetcher.stats.items()}, 'disallowedSkipped': len(fetcher.disallowed),
                   'input': {k: (str(v) if isinstance(v, dt.date) else v) for k, v in cfg.items() if k not in ('proxy',)}}
        await Actor.set_value('OUTPUT', summary)
        Actor.log.info(f'Summary: {summary}')

        # Schema-drift / systemic block guard: fail loudly instead of returning silent empties.
        bad = stats['pagesFailed'] + stats['pagesEmpty']
        if cfg['mode'] != 'urls' and stats['pages'] and bad / stats['pages'] >= 0.3:
            await Actor.fail(status_message=(f"{stats['pagesFailed']} of {stats['pages']} pages failed and {stats['pagesEmpty']} were empty "
                                             '(Product Hunt blocked the requests or changed its page format). '
                                             f"Saved {stats['pushed']} launches before stopping."))
            return
        if cfg['mode'] == 'urls' and stats['pushed'] == 0 and cfg['urls']:
            await Actor.fail(status_message='None of the URLs returned launch data (see dataset errors).')
            return
        msg = (f"Done: {stats['pushed']} launches" + (f", {stats['details']} with details" if cfg['includeDetails'] else '')
               + (f", {stats['detailsFailed']} detail errors" if stats['detailsFailed'] else '')
               + (f", {stats['skippedSeen']} already seen" if cfg['onlyNew'] else '')
               + (' (details paused part-way: Product Hunt was blocking)' if guard['detailsPaused'] else '')
               + (f' (stopped: {stop_reason})' if stop_reason else '') + (f'. {status_note}' if status_note else ''))
        await Actor.set_status_message(msg, is_terminal=True)
