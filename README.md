# PS5 Pro stock watcher (Canada / GTA)

Checks Canadian retailers every 10 minutes on GitHub Actions and emails you when a
PS5 Pro can actually be ordered at a normal price (at or below `max_price_cad` in
`config.json`), from the retailer itself (third-party resellers are ignored).

| Store | Method | From GitHub's servers |
|---|---|---|
| Best Buy (online + 12 GTA stores pickup) | JSON API | works |
| Walmart (sold by Walmart only) | page data, browser fallback | works |
| Amazon.ca (2 listings) | browser | usually works (occasional captcha) |
| Visions, Canada Computers, London Drugs, Memory Express, Costco, Staples | browser | works |
| EB Games | browser | usually blocked by Cloudflare (works from a home PC) |
| PlayStation Direct | none | blocks all automated access |

## Setup
Repo secrets (Settings > Secrets and variables > Actions):
`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (16-letter Google App Password), `EMAIL_TO`.

Manual run: Actions tab > "PS5 Pro stock watch" > Run workflow
(tick `test_email` to send a test email, or `dry_run` to check without emailing).

## Behaviour
- Emails once when a listing becomes orderable, then again every 3 hours while it stays orderable.
- Emails a warning if a store's check fails ~36 runs (about 6 h) in a row (not for EB Games).
- A monthly empty "keep-alive" commit stops GitHub from pausing the schedule.

## Run locally (Windows or Mac)
```
pip install -r requirements.txt
python watcher.py --dry-run      # check only
python watcher.py --test-email   # needs a .env with GMAIL_ADDRESS / GMAIL_APP_PASSWORD
```
