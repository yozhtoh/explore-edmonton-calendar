# Explore Edmonton auto-updating calendar subscription

This project converts the public [Explore Edmonton event calendar](https://exploreedmonton.com/event-calendar) into an iCalendar (`.ics`) subscription feed. GitHub Actions checks for changes every six hours and GitHub Pages hosts a stable subscription URL.

## Set up in about five minutes

1. Create a new **public** GitHub repository, such as `explore-edmonton-calendar`.
2. Upload all files and folders from this package to the repository's `main` branch.
3. In **Settings → Pages**, choose **Deploy from a branch**, branch `main`, folder `/docs`, then save.
4. Open **Actions**, select **Update Explore Edmonton calendar**, and choose **Run workflow** once.
5. Your subscription address will be:

   `https://YOUR-GITHUB-USERNAME.github.io/explore-edmonton-calendar/explore-edmonton-events.ics`

   For Apple Calendar, you may replace `https://` with `webcal://`.

## Subscribe in common calendars

### Google Calendar

On desktop: **Other calendars → + → From URL**, paste the `https://...ics` address, then choose **Add calendar**.

### Outlook on the web / Microsoft 365

Go to **Calendar → Add calendar → Subscribe from web**, paste the `https://...ics` address, name it, and import it.

### Apple Calendar

Choose **File → New Calendar Subscription**, paste the `webcal://...ics` address, and set auto-refresh to your preferred interval.

## How updates work

- GitHub Actions runs every six hours.
- The script discovers Explore Edmonton event pages and reads their structured `Event` data.
- It writes `docs/explore-edmonton-events.ics` and commits only when the feed changes.
- Calendar apps independently decide how often to refresh subscribed feeds. Google Calendar can take several hours to reflect a feed update; this timing is controlled by Google, not this project.

## Important notes

This is an unofficial personal-use feed. It depends on the public website's structure and may need maintenance if Explore Edmonton redesigns its pages. Review the site's terms and robots policy before high-frequency or commercial use. The workflow is deliberately limited to four checks per day and pauses between requests.

## Rich event details included in version 2

The generated feed now attempts to include:

- Event title and start/end dates or times
- Venue name and full published address in the standard `LOCATION` field
- Published latitude/longitude in the standard `GEO` field, when the event page exposes coordinates
- Main event description plus useful page headings/subheadings in both plain text and HTML (`X-ALT-DESC`)
- Organizer/performer, category, price, ticket link, event image, and source-page URL when published

### Calendar-app limitations

An iCalendar event is not a copy of a web page. Apple Calendar and Outlook generally display rich HTML descriptions better than Google Calendar. Some apps ignore images, custom fields, HTML headings, or `GEO`, but all apps should retain the standard title, dates, location, plain description, and source URL. A location/address normally becomes a clickable map link automatically in the calendar app. Coordinates are included as an additional mapping aid when Explore Edmonton publishes them.

## 403 / anti-bot handling

Version 3 uses two fetch methods automatically:

1. `curl_cffi` impersonates a normal Chrome network stack for fast requests.
2. If Explore Edmonton returns 403/Access Denied, Playwright launches headless Chromium and renders the page like a browser.

The GitHub Actions workflow installs Chromium automatically. No API key or secret is required.

If a run fails, open Actions > Update Explore Edmonton calendar > update > Build calendar and inspect the final error. The existing `.ics` file is intentionally left unchanged when no events can be parsed.
