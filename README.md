# Product Hunt Scraper: Launches, Leaderboards, Makers & Websites

[![Run on Apify](https://img.shields.io/badge/Run%20on-Apify-1f6feb?logo=apify)](https://apify.com/manageable_gully/product-hunt-scraper) [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> **Run it in the cloud with no setup:** [Product Hunt Scraper: Launches, Leaderboards, Makers & Websites on the Apify Store](https://apify.com/manageable_gully/product-hunt-scraper).  
> Pay only per result, export to JSON/CSV/Excel, schedule runs and call it from the API.
<!-- Note: the Apify Store page goes live once the Actor is published. -->
<!-- AFFILIATE_LINK_PLACEHOLDER: replace the Apify links above with the affiliate link when available -->

Get **Product Hunt launches as clean JSON, CSV or Excel**: daily, weekly and monthly leaderboards, topic pages or any list of launch URLs. Each row has the product name, tagline, votes, comments, daily/weekly/monthly rank, topics, thumbnail and launch date, plus optional **details**: website URL and domain, makers and hunter (public names only), categories, rating, followers and description.

Built for **VCs and scouts** (deal flow), **founders** (competitor tracking), **newsletter writers** ("top launches of the week") and **growth teams** (lists of new SaaS websites).

## Why this scraper
- **Reliable:** plain HTTP with a real-browser TLS fingerprint, no headless browser, retries with backoff, fresh sessions on blocks and an automatic residential-proxy fallback. Built for a 98%+ run success rate.
- **Cheap:** $1.20 per 1,000 launches (listing only) or **$2.50 per 1,000 with details**, no monthly rent. 512 MB memory.
- **Only new launches:** turn on *Only new launches since last run* for daily schedules, and you pay only for launches you haven't seen.
- **Filters:** topics, keywords (name/tagline) and minimum votes. Filtered-out launches are free.
- **Polite and logged out:** never logs in, never creates accounts, follows robots.txt (no `/search`, no `/r/` short links, no user sub-pages), about 1 request per second, slowing down further if Product Hunt pushes back.
- **Privacy-minded:** no emails and no social-profile crawling. Maker data is limited to the public name, username and headline shown on the launch page, and you can switch it off.

## Modes
| Mode | What you get |
|---|---|
| `leaderboard` (default) | Top launches of every day, week or month in a date range (Pacific Time, Product Hunt's launch day). Featured ranking by default, or the *All* tab ranked by votes. Each leaderboard page gives the top ~17 launches. |
| `topic` | Products listed on topic pages, e.g. `artificial-intelligence`, `developer-tools` (highest rated first, 15 per page). |
| `urls` | Any `https://www.producthunt.com/posts/<slug>` or `/products/<slug>` URLs. |

## Input examples
Yesterday's top launches with websites and makers (the default):
```json
{ "mode": "leaderboard", "period": "daily", "startDate": "1 day", "maxItems": 100, "includeDetails": true }
```
All AI launches with 200+ votes in September 2026's monthly leaderboard:
```json
{ "mode": "leaderboard", "period": "monthly", "startDate": "2026-09-01", "topics": ["artificial-intelligence"], "minVotes": 200 }
```
Daily schedule that returns only new launches:
```json
{ "mode": "leaderboard", "startDate": "1 day", "onlyNewSinceLastRun": true, "dedupeKey": "daily-digest" }
```

## Output
| Field | Description |
|---|---|
| `postId`, `name`, `tagline`, `slug` | Launch identity |
| `launchUrl`, `productUrl`, `productSlug` | Product Hunt links |
| `launchDate` (PT), `featuredAt` | Launch day and timestamp (with its own −07:00/−08:00 offset) |
| `votes`, `launchDayVotes`, `commentsCount` | Engagement |
| `dailyRank`, `weeklyRank`, `monthlyRank` | Leaderboard positions |
| `topics`, `thumbnailUrl` | Topic names and image |
| `websiteUrl`, `websiteDomain`, `launchLink` | Details: the product's own website (direct URL, no redirect) |
| `description`, `categories`, `reviewsRating`, `reviewsCount`, `followersCount`, `productLaunchesCount`, `isNoLongerOnline` | Details: product info |
| `githubUrl`, `twitterUrl`, `linkedinUrl` | Details: company links listed on the product page |
| `makers[]` {name, username, headline, profileUrl}, `hunter` {name, username} | Details: public maker/hunter names (optional) |
| `detailsError` | Set when details could not be loaded (the row is then charged as a basic launch only) |
| `scrapedAt`, `source` | Run time (UTC) and attribution |

```json
{
  "postId": "1274680", "name": "Jockey by TwelveLabs",
  "tagline": "Your entire video library: organized, searchable, understood",
  "launchUrl": "https://www.producthunt.com/posts/jockey-by-twelvelabs-2",
  "productUrl": "https://www.producthunt.com/products/twelvelabs",
  "launchDate": "2026-10-16", "featuredAt": "2026-10-16T00:01:00-07:00",
  "votes": 405, "commentsCount": 29, "dailyRank": 1, "weeklyRank": 2,
  "topics": ["Productivity", "Artificial Intelligence", "Video"],
  "websiteUrl": "https://www.twelvelabs.io", "websiteDomain": "twelvelabs.io",
  "reviewsRating": 5, "reviewsCount": 1,
  "makers": [{ "name": "Jae Lee", "username": "jaelee_", "headline": "Co-founder & CEO", "profileUrl": "https://www.producthunt.com/@jaelee_" }],
  "hunter": { "name": "fmerian", "username": "fmerian" },
  "source": "producthunt.com public page"
}
```
The dataset has three views: **Launches**, **Makers & hunters** and **Ratings & categories**.

## Pricing (pay per event)
| Event | Price (Starter plan) |
|---|---|
| Actor start | $0.00005 |
| Launch saved | $0.0012 |
| Launch details (website, makers, rating…) | $0.0013 |

1,000 launches with details ≈ **$2.50**. Listing only ≈ **$1.20**. Turn off *Include details* to lower cost. Set a *maximum cost per run* in the run options and the Actor stops cleanly when it's reached.

## Integrations
Use it from Make, Zapier, n8n, Google Sheets, webhooks or the Apify API (JavaScript, Python, cURL). Schedule it daily and send new launches to Slack or a spreadsheet.

## FAQ
**Does it need a Product Hunt account?** No. It reads only public, logged-out pages.

**Why not the official Product Hunt API?** The API's terms do not allow commercial use without Product Hunt's permission, so this Actor does not use it.

**Is scraping Product Hunt legal?** The Actor collects publicly available, factual launch data without logging in. Note that Product Hunt's website terms prohibit crawling and scraping; you are responsible for how you use the data and for complying with the terms that apply to you. Maker names and usernames are personal data under GDPR. Make sure you have a lawful basis for processing them, or switch off *Include makers*. This Actor is not affiliated with or endorsed by Product Hunt.

**Which time zone are dates in?** Pacific Time, because Product Hunt's launch day starts at 00:01 PT.

**How many launches per leaderboard?** Each daily, weekly or monthly leaderboard page gives its top ~17 launches. Scrape a date range to collect more.

## Limits and changelog
- Leaderboards: top ~17 per day/week/month. Topic mode: up to ~1,000 products per topic. Max date range: 366 days per run.
- 1.0 (2026-10-17): first release.

*This Actor was built and is maintained with AI assistance. Report issues on the Issues tab; they are monitored daily.*

## Run from source
This repository is the full source of the [Product Hunt Scraper: Launches, Leaderboards, Makers & Websites](https://apify.com/manageable_gully/product-hunt-scraper) Actor.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest
# run locally with the Apify CLI (https://docs.apify.com/cli):
apify run --input '{...}'
```

Deploy your own copy with `apify push`. The easiest way to use it is the hosted version on the [Apify Store](https://apify.com/manageable_gully/product-hunt-scraper).

<!-- AFFILIATE_LINK_PLACEHOLDER: "New to Apify? Sign up here" link goes here -->

## License
MIT, see [LICENSE](LICENSE).
