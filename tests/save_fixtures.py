"""Save fresh public-page snapshots used by the unit tests (run before `pytest`): python tests/save_fixtures.py"""
import gzip
import pathlib

from curl_cffi import requests

FX = pathlib.Path(__file__).parent / 'fixtures'
PAGES = ['leaderboard/daily/2026/10/16', 'leaderboard/weekly/2026/41', 'leaderboard/monthly/2026/9',
         'topics/artificial-intelligence', 'posts/jockey-by-twelvelabs-2']

if __name__ == '__main__':
    FX.mkdir(exist_ok=True)
    s = requests.Session(impersonate='chrome')
    (FX / 'robots.txt').write_text(s.get('https://www.producthunt.com/robots.txt', timeout=30).text)
    for p in PAGES:
        r = s.get('https://www.producthunt.com/' + p, timeout=30)
        with gzip.open(FX / (p.replace('/', '_') + '.html.gz'), 'wt', encoding='utf-8') as f:
            f.write(r.text)
        print(p, r.status_code, len(r.text))
