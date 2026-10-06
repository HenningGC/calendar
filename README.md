# calendar

One public calendar view merged from several Microsoft 365 calendars, published with
GitHub Pages and refreshed every hour by GitHub Actions.

**Live:** https://henninggc.github.io/calendar/
**Subscribe:** `webcal://henninggc.github.io/calendar/calendar.ics`

## How it works

1. `scripts/build.py` downloads every ICS feed listed in the `ICS_URLS` repository
   secret, expands recurring events, drops cancelled ones, merges duplicates that
   appear in more than one calendar and writes:
   - `site/events.json` – occurrences for the web view (UTC timestamps)
   - `site/calendar.ics` – a merged feed other calendar apps can subscribe to
   - `site/meta.json` – build time, sources and counts
2. `web/index.html` (copied into `site/`) renders the events with
   [FullCalendar](https://fullcalendar.io/) – month, week and agenda views, a legend to
   toggle each source calendar and a time-zone switcher.
3. `.github/workflows/build.yml` runs the script hourly (and on every push or manual
   trigger) and deploys `site/` to GitHub Pages. If a feed cannot be fetched the build
   fails and the previously published site stays online.

The feed URLs never enter the repository: they only live in the `ICS_URLS` secret.

## Configuration

| Where | Name | Purpose |
|---|---|---|
| Secret | `ICS_URLS` | One feed per line (or comma separated). Optional label: `work=https://…/calendar.ics`. Without a label the Outlook tenant domain is used (`epam`, `apollogic`, …). |
| Variable | `PRIVACY` | `busy` → only Busy / Tentative / Out of office blocks · `titles` → event titles only (default) · `full` → titles, locations and descriptions |
| Variable | `PAST_DAYS` | Days of history to include (default 90) |
| Variable | `FUTURE_DAYS` | Days ahead to include (default 365) |

Change a secret or variable, then re-run the workflow (Actions → *Build and publish
calendar* → *Run workflow*) or wait for the next hourly run.

```bash
gh secret set ICS_URLS --repo HenningGC/calendar < feeds.txt
gh variable set PRIVACY --repo HenningGC/calendar --body busy
gh workflow run build.yml --repo HenningGC/calendar
```

## Run locally

```bash
pip install -r requirements.txt
ICS_URLS="https://…/calendar.ics" python scripts/build.py
python -m http.server --directory site 8000   # then open http://localhost:8000
```

## Notes

- GitHub disables the hourly schedule on public repositories after 60 days without any
  commit. Push a small change (or trigger the workflow manually) to re-enable it.
- Outlook's published ICS feeds usually cover a limited window around today; events
  outside that window are not available to merge.
