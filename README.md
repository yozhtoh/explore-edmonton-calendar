Explore Edmonton timed-only calendar subscription

This project turns Explore Edmonton's public event calendar into a subscription feed hosted by GitHub Pages.

Strict publishing rule

Only event occurrences with an explicit clock time are included.

Date-only / all-day events are omitted.

No default times are invented.

No duration is invented when only a start time is published.

Explicit lists of dates with one shared time become separate VEVENT entries.

Date ranges are expanded only when the page explicitly says something like "daily", "every day", or "every Saturday".

Ambiguous multi-day date ranges are skipped rather than guessed.

The workflow also fails if the generated .ics contains DTSTART;VALUE=DATE or DTEND;VALUE=DATE.

GitHub setup

Create a public GitHub repository, for example explore-edmonton-calendar.

Upload all files and folders, including the hidden .github folder, to the main branch.

Go to Settings -> Pages.

Choose Deploy from a branch, branch main, folder /docs, then save.

Go to Settings -> Actions -> General -> Workflow permissions and enable Read and write permissions if your repository policy requires it.

Go to Actions -> Update Explore Edmonton timed calendar -> Run workflow for the first build.

Wait for all steps to turn green.

The workflow runs automatically every 6 hours.

Subscription URL

For a repository named explore-edmonton-calendar:

https://YOUR-USERNAME.github.io/explore-edmonton-calendar/explore-edmonton-events.ics

Apple Calendar can also use:

webcal://YOUR-USERNAME.github.io/explore-edmonton-calendar/explore-edmonton-events.ics

Use subscription by URL, not a one-time .ics import, if you want future updates.

How event times are chosen

A true date-time from Event JSON-LD is accepted.

If the site's structured dates are date-only, the rendered event page is searched for explicit clock times such as 7:30 PM or 11:00 a.m. to 6:00 p.m..

The time is associated only with explicit dates or an explicit recurrence described on the page.

If that association is ambiguous, the page is omitted.

This intentionally favors fewer, trustworthy events over filling the calendar with guessed schedules.
