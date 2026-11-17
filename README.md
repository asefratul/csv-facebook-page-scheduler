# CSV Facebook Page Scheduler

Schedule Facebook Page posts from a CSV file, for free. No server, no paid service, no database: a GitHub Actions job reads your content calendar and publishes whatever is due.

Supports text, link, single-image, multi-image and Reel (video) posts, an optional first comment on each, Unicode text (emoji, Bangla, Arabic, etc.), and several posts per day.

> **Unofficial.** Not affiliated with or endorsed by Meta. You are responsible for following Meta's Platform Terms and Page policies, and for the content you post. Provided as-is under the MIT license.

A GitHub Actions job that publishes the Facebook posts from a CSV content calendar. It runs **every hour**, publishes whatever is due (several posts per day are fine), adds an optional first comment, and records what it published.

```
content.csv  ──►  publish.py  ──►  Facebook Graph API  (post + optional first comment)
 (you edit)           │
 media/*.png          └──►  posted.csv  (log of published rows, committed back by the workflow)
```

| File | Purpose |
|---|---|
| `content.csv` | Your content calendar: one row per post. You write and upload this. The job never modifies it. |
| `content.example.csv` | One example row of every post type. Copy rows from it into `content.csv` (which starts empty, so nothing is posted by accident). |
| `media/` | Images for `image` posts and videos for `reel` posts (any folder inside the repo works). |
| `posted.csv` | Created by the job. One line per published row, so nothing is posted twice. |
| `publish.py` | Finds the rows that are due, publishes them, comments, logs them. |
| `get_page_token.py` | One-off helper you run locally: turns a Graph API Explorer token into a verified non-expiring Page token. |
| `.github/workflows/schedule.yml` | Runs `publish.py` hourly (and on demand) and commits `posted.csv`. |
| `config.json`, `brand-guidelines.example.md` | Settings and a guidelines template for the optional AI draft workflow (see below). |
| `make_content.py`, `generator/`, `sync_drafts.py`, `content-plan.csv`, `ideas/` | Optional: idea agent, draft generator and PR syncing (see *AI-assisted drafts*). |
| `.github/workflows/*-ideas.yml`, `generate-content.yml`, `sync-drafts.yml` | Optional: the idea, draft and sync workflows. Delete them if you only want the plain scheduler. |

---

## Prerequisites

You need all of the following before the first run.

### 1. A Facebook Page you administer
You need full admin control of the Facebook Page. Note its numeric **Page ID** (Page → About / Page transparency).

### 2. A Meta app in **Live** mode
- Create or use an app at [developers.facebook.com](https://developers.facebook.com) → **My Apps**.
- The app must be **Live**, not in Development mode. In Development mode, posts made through the API may be visible only to people with a role on the app. Going Live needs a Privacy Policy URL, an app icon and a category (**App settings → Basic**).
- The app must offer these permissions (in the app dashboard under **Use cases**, add *Manage everything on your Page* if they are missing):
  `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `pages_manage_engagement`

### 3. A non-expiring **Page access token**
The scheduler needs a Page token, not a user token. A Page token obtained from a long-lived user token never expires. `get_page_token.py` does the exchange for you and verifies the result.

1. Open [Graph API Explorer](https://developers.facebook.com/tools/explorer), choose your app, and click **Get User Access Token**.
2. Add the four permissions above and generate the token. In the dialog, make sure your Page is ticked. This short-lived token lasts about an hour, so do the next steps right away.
3. Get your **App ID** and **App Secret** from **App settings → Basic** and run the script (needs Python 3 and `pip install requests`):
   ```bash
   export FB_APP_ID=123456789
   export FB_APP_SECRET=your_app_secret      # or skip this line: you will be prompted, with hidden input
   python get_page_token.py "PASTE_SHORT_LIVED_TOKEN" --page-id YOUR_PAGE_ID
   ```
   Leave out `--page-id` to list the Pages the token can manage, then run it again with the ID you want.
4. The script prints four checks, then the Page token on its own line:
   ```
   [PASS] Token is valid
   [PASS] Type is PAGE (got PAGE)
   [PASS] Expires: never (expires_at = 0)
   [PASS] Has pages_manage_posts and pages_manage_engagement
   ```
   Save the token as the `FB_ACCESS_TOKEN` secret (see section 5). Do not commit it or paste it into chat or issues. Putting a token on the command line leaves it in your shell history, so clear that entry if the machine is shared (the short-lived token is already useless after an hour; the App Secret is why it is read from the environment or a prompt instead of an argument).

#### How to verify the token really is long-lived
The script checks this itself, using Meta's `debug_token` endpoint, and you should confirm it independently once:

1. Open the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken), paste the **Page token**, and click **Debug**.
2. You must see **Type: Page** and **Expires: Never**, and the scopes must include `pages_manage_posts` and `pages_manage_engagement`.
3. If it says **Expires** with a date or hours, the token is a short-lived one. Typical causes: the exchange step failed, or you pasted the user token instead of the Page token. Run the script again with a fresh short-lived token.
4. Later, a failing run with *"Error validating access token"* means the token was invalidated (password change, app secret reset, removal from the Page, or permissions revoked). Generate a new one with the same steps.

The debugger may also show a separate **Data Access Expires** date. That is a different limit from the token's expiry: use the token's **Expires** line to judge whether it is long-lived, and if API calls start failing regardless, regenerate it.

### 4. A GitHub repository
- Push this repo to GitHub (private is recommended).
- Scheduled workflows only run from the repo's **default branch**, so push this code to it.

### 5. GitHub secrets
The workflow runs in the **`production`** environment, so the secrets must be stored there.

1. Repo → **Settings → Environments → New environment** → name it `production` (skip if it exists).
2. In that environment, under **Environment secrets**, add:

   | Name | Value |
   |---|---|
   | `FB_PAGE_ID` | Numeric Page ID |
   | `FB_ACCESS_TOKEN` | The Page access token from step 3 |

3. Do **not** add required reviewers to the environment. A required reviewer makes every scheduled run wait for manual approval.
4. If you would rather use repository-level secrets, add them under **Settings → Secrets and variables → Actions** and delete the `environment: production` line from the workflow.

Never commit tokens or the App Secret to the repo.

### 6. Local tools (only for running locally)
Python 3 and `pip install requests`.

---

## Writing posts: `content.csv`

One row per post, with a header row. Prepare as many days as you like in a spreadsheet, export it as CSV (UTF-8), and upload it to the repo, replacing `content.csv`.

| Column | Required | Meaning |
|---|---|---|
| `post_type` | no | `text`, `link`, `image`, `images` (several pictures) or `reel`. Blank means `text`. |
| `post_content` | yes (except `image`, `images`, `reel`) | The post text. For `image`, `images` and `reel` posts it is the caption. May span several lines. |
| `post_comment` | no | The first comment, usually a link. **Leave blank for no comment**: the post is still published. |
| `post_date` | yes | When to publish, in your configured time zone (see below): `2026-10-08 09:00` (24-hour clock). A date alone, `2026-10-08`, means the default time (19:00). |
| `media_url` | for `link`, `image`, `images`, `reel` | `link`: the full URL to attach. `image`: a file path inside this repo (`media/play.png`) or a public image URL. `images`: 2 to 10 of those separated by `|` (`media/a.jpg\|media/b.jpg`). `reel`: a `.mp4`/`.mov` path inside this repo (`media/tip1.mp4`) or a public video URL. Also accepted as `media_path`. |

Example:

```csv
post_type,post_content,post_comment,post_date,media_url
text,"Small steps lead to big milestones!",Our guide: https://example.com,2026-10-08 09:00,
image,"Play is the highest form of research!",What toy does your child love?,2026-10-08 19:00,media/play.png
link,"New article is live",,2026-10-09 09:00,https://example.com/blog/routines
images,"Our week in pictures",,2026-10-09 18:00,media/week1.jpg|media/week2.jpg|media/week3.jpg
reel,"3 calm-down tips for toddlers",Save this for later!,2026-10-10 18:00,media/tip1.mp4
```

### Time zone and default time
Times in `post_date` are read in the time zone set by the repository variable `POST_TIMEZONE` (an IANA name such as `Asia/Dhaka` or `America/New_York`). **It defaults to UTC**, so set it: **Settings → Secrets and variables → Actions → Variables → New repository variable**. You can also set `DEFAULT_POST_TIME` (`HH:MM`, default `19:00`) for rows that have a date but no time.

### Updating content (no git commands needed)
Do it on github.com, or in the GitHub mobile app. The hourly job picks up whatever is in the repo at its next run.

**Edit the file**
1. Open `content.csv` in the repo and click the pencil (**Edit**).
2. Add or change rows. Quote any cell that contains commas, quotes or line breaks.
3. Click **Commit changes** and commit directly to the `master` branch.

**Or replace it with a spreadsheet export** (easiest for many rows)
1. Keep your calendar in Google Sheets or Excel and export it as **CSV UTF-8**, named exactly `content.csv`.
2. In the repo, **Add file → Upload files**, drop the file in (it replaces the old one), and **Commit changes**.

**Adding an image:** **Add file → Upload files** into the `media/` folder, then put `media/<file name>` in the `media_url` column. Upload the image and the CSV change before the post is due.

The job only commits `posted.csv`, never `content.csv`, so your edits and the job's commits don't collide. Rows for posts that have already gone out can stay in the file; the log in `posted.csv` prevents reposts.

### Several posts on one day
Give each row its own time (`2026-10-08 09:00`, `2026-10-08 19:00`). Rows with the same date and no time all go out at the default time, one after another. A row is identified by its date and time, so changing a row's time after it has been posted makes it look like a new row.

### Images from the repo
1. Put the image in the repo, for example `media/play.png`, and commit it.
2. Put that path in `media_url`. The path is relative to the repo root, uses `/`, and cannot point outside the repo.
3. The job uploads the file to Facebook directly, so the repo can stay private. JPG and PNG work; keep files to a few MB. One image per post (no albums).

### Several images in one post
Use `post_type` `images` and list 2 to 10 images in `media_url`, separated by `|` (spaces around it are fine). Each can be a repo path or a public URL, mixed freely. The caption goes in `post_content`; the comment works as usual. Facebook shows them as one post with a photo grid, in the order listed. For a single picture use `image`.

If one upload fails, nothing is published and the whole row is retried on the next run. If the final publish call times out, the row is logged `UNCERTAIN` (check the Page, as for any post).

### Reels (video)
Set `post_type` to `reel` and `media_url` to a video in the repo (`media/tip1.mp4`) or a public video URL. `post_content` becomes the Reel's caption (it can be blank).

**Video requirements (Facebook's):** vertical 9:16, 3 to 90 seconds, at least 540x960 (1080x1920 recommended), 24 to 60 fps, MP4 with H.264 video and AAC audio. Facebook may reject anything else.

**Getting the file into the repo:** the GitHub web upload accepts files up to **25 MB**; a git push accepts up to 100 MB (the job also refuses files over 100 MB). A 30-second 1080x1920 clip usually fits in 25 MB after compressing. With ffmpeg (untested here, adjust `-crf` higher for a smaller file):

```bash
ffmpeg -i in.mp4 -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2" \
  -c:v libx264 -preset slow -crf 26 -r 30 -pix_fmt yuv420p -c:a aac -b:a 128k -ar 48000 -movflags +faststart out.mp4
```

**Things to know**
- Git keeps every version of every file forever, so videos make the repo grow and slow the job's checkout. Once a Reel is published, delete its file from `media/` (the row can stay in `content.csv`; `posted.csv` prevents a repost).
- After publishing, the job waits up to 5 minutes for Facebook to finish processing and then posts the first comment. If processing is still going, the comment is not posted and its text is printed in the log for you to add by hand.
- If Facebook reports the Reel failed processing, the run fails and the row is already in `posted.csv`; fix the video and delete that line to retry.
- Facebook allows about 30 API-published posts and Reels per Page per 24 hours.
- The Page token you already have (`pages_manage_posts`, `pages_manage_engagement`) covers Reels.

### Rules
- A row with an invalid value fails the run **only once its time has come**, so you get an email. An invalid row dated in the future only logs a warning.
- Header names are matched ignoring case and spaces (`Post Type` works), and column order doesn't matter.
- Put quotes around any cell containing commas, quotes or line breaks. Spreadsheet apps do this on export.
- Dates must be `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. Excel and Google Sheets often reformat dates when saving, so check the CSV in a text editor before uploading.
- A warning is logged when fewer than 3 future posts remain.

---

## When posts actually go out

The workflow runs at minute 7 of every hour. A run publishes every row that is **due** (its time has passed) and **not yet in `posted.csv`**, oldest first.

- A row scheduled for `09:00` is published by the first run after 09:00, normally about 09:07. GitHub can delay scheduled runs by 15 to 60 minutes at busy times, so treat the time as "within the hour that follows".
- **Catch-up:** if a run is late or missed, the next run still publishes rows that became due in the last **6 hours** (`CATCHUP_HOURS`). Older unposted rows are not published automatically; the log says how many were skipped.
- Cost: 24 short runs a day. On a private repo that is roughly 720 billed minutes a month, inside the 2,000 free minutes on GitHub's Free plan. Check your own plan.

To change the timezone, default time or catch-up window, set `POST_TIMEZONE`, `DEFAULT_POST_TIME` or `CATCHUP_HOURS` as `env:` values on the "Run publishing script" step.

---

## Running it

### On GitHub (normal use)
Nothing to do: the hourly schedule handles it. Keep `content.csv` stocked.

### Manual run and dry run
1. Repo → **Actions** → **Auto Post to Facebook** → **Run workflow**.
2. **dry_run**: tick it to print what would be posted without publishing or logging anything.
3. **as_of** (optional): pretend "now" is `YYYY-MM-DD HH:MM`, or a plain `YYYY-MM-DD` to cover that whole day. Use it to preview any day, or to publish rows that were missed.
4. **Do a dry run first**, check the log, then do one real run and open the post in a logged-out browser window to confirm that the post and the comment are public.

A real manual run publishes everything that is due at that moment and records it in `posted.csv`.

### Locally
```bash
pip install requests

# Preview only: nothing is published and nothing is written
DRY_RUN=true python3 publish.py
DRY_RUN=true POST_AS_OF="2026-10-08" python3 publish.py   # preview a whole day

# Real publish: posts what is due now and writes posted.csv
FB_PAGE_ID=... FB_ACCESS_TOKEN=... python3 publish.py
```

---

## What the script does and when it fails

Each run:
1. Loads `content.csv` and `posted.csv`.
2. Picks the rows that are due and not yet posted. Nothing due: it finishes successfully.
3. For each, oldest first: publishes it (`text` and `link` to `/{PAGE_ID}/feed`, `image` to `/{PAGE_ID}/photos`, `images` as unpublished photos attached to one `/feed` post, `reel` through `/{PAGE_ID}/video_reels`: start, upload, finish, then wait for processing).
4. Immediately records it in `posted.csv`, so a later failure cannot cause a repost.
5. Adds the first comment, if `post_comment` is not blank.
6. The workflow commits `posted.csv`, even if something failed.

The run is marked **failed** (red, with a GitHub email) when:
- the secrets are missing, or Facebook rejects a post (the row is retried on the next hourly run while still inside the catch-up window);
- a due row is invalid (bad type, empty content, bad date, missing image or video file, or link);
- `content.csv` is missing or lacks a required column;
- a post went live but its first comment failed (the log prints the comment text so you can add it by hand);
- a request **timed out**: the post may or may not have gone live. The row is logged as `UNCERTAIN` in `posted.csv` and is **not** retried, to avoid a double post. Check the Page; if it is not there, delete that line from `posted.csv` and the next run will try again.

One bad row never blocks the others: the remaining due rows are still published in the same run.

---

## Optional: AI-assisted drafts, approved by merging a PR

Everything above works on its own. This optional layer adds an agent that proposes post ideas, writes the posts
for the ideas you approve, and opens them as pull requests. **You stay in control: nothing is posted unless you
merge, and the publisher only ever reads `content.csv` on the default branch.**

```
weekly      Claude proposes ideas ──► GitHub issue (label content-ideas) + optional Telegram notice
you         comment  /approve 1,3 hold 2 reject 4
approved    ideas ──► content-plan.csv + ideas/<id>.json ──► draft workflow
draft       Claude writes caption + card text, cards are rendered ──► pull request (label content-draft)
you         review / edit in the web editor, then MERGE = approval      (close = reject)
merged      publish.py posts it at its scheduled time; the next draft starts
```

### Setup (all of it is optional except the Anthropic key)

1. **Secrets**, in the same `production` environment as the Facebook secrets (section 5 above):

   | Name | Needed | Value |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | for this whole feature | API key from [console.anthropic.com](https://console.anthropic.com) |
   | `TELEGRAM_BOT_TOKEN` | optional | Token of a bot made with [@BotFather](https://t.me/BotFather) |
   | `TELEGRAM_CHAT_ID` | optional | Your chat id with that bot (see below) |

   Without the two Telegram secrets everything still works; you just get no phone notifications (and no reminder).
   To find your chat id: message your bot once, add the two Telegram secrets (use `0` as a placeholder id), run
   **Actions → Check Telegram → Run workflow**, read the id it lists, and put it in `TELEGRAM_CHAT_ID`.
2. **Repository settings** (Settings → Actions → General → Workflow permissions): choose *Read and write
   permissions* and tick *Allow GitHub Actions to create and approve pull requests*. Draft PRs are opened by the
   workflow, so without this they fail.
3. **Edit `config.json`**: brand name, language, link, disclaimer, hashtags, colours, cadence (table below).
   Copy `brand-guidelines.example.md` to `brand-guidelines.md` and describe your voice, audience and rules. The AI
   reads it for every idea and post. (If it is missing, the example is used.)
4. **Variables**: set `POST_TIMEZONE` as described under *Time zone and default time*. The generator uses the
   same variable, so draft dates and the publisher agree.
5. Run **Actions → Propose post ideas → Run workflow** once to try it. The branch protection of the default branch
   must allow the Actions bot to push (the approval workflow commits `content-plan.csv` and `ideas/` there).

The repository must belong to a personal account: commands are accepted only from `github.repository_owner`
(and `author_association == OWNER`). On an organisation repository no one would count as the owner; you would
need to change that condition in the workflows deliberately.

### `config.json`

| Key | Meaning (default) |
|---|---|
| `brand_name` | Shown on the cards and given to the AI (`Your Brand`) |
| `language` | Language the AI writes in (`English`). Any language works; also set `required_script` if you want a check |
| `model` | Claude model id (`claude-sonnet-5-5`). Override per run with the `CLAUDE_MODEL` env variable |
| `brand_guidelines` | Path of your guidelines file (`brand-guidelines.md`) |
| `default_link` | Link added once to the first comment of each draft; empty = no link (empty) |
| `disclaimer`, `hashtags` | Appended to every caption; empty = nothing added |
| `required_script` | Optional regex every text must match, e.g. `[\u0600-\u06ff]` to require Arabic letters |
| `blocked_patterns` | Extra regexes that block a draft or idea. A built-in list already blocks cure/diagnose/treat/guarantee/"100%" wording |
| `kinds` | Post kinds the AI may propose and their labels (`tip`, `myth`, `activity`) |
| `cadence` | `post_hour` (12), `spacing_days` (4 days after the last post), `lead_days` (3 days to review), `queue_below` (2: draft only when fewer future posts exist), `ideas_per_batch` (5), `max_pending` (4: stop proposing while this many approved topics wait) |
| `card_style` | `background`, `accent`, `text`, `font_family`, `cover_subtitle`, `last_slide_note` for the cards |

When the workflows run is set by the `cron:` lines in `.github/workflows/` (UTC): publisher hourly (`schedule.yml`),
ideas on Sundays 04:00 (`propose-ideas.yml`), reminder 2 hours later (`remind-ideas.yml`), drafting daily 03:41
(`generate-content.yml`). Edit them to change the cadence.

### Approving ideas: `/approve`

The weekly issue lists numbered ideas. Only **your** comment counts. The first line of the comment is the command:

| Comment | Effect |
|---|---|
| `/approve 1,3` | Approve 1 and 3; the rest are rejected |
| `/approve 1,3 hold 2` | Also hold 2: it returns first in the next batch (dropped after 3 holds) |
| `/approve 1 hold 2,4 reject 3,5` | Explicit; **unlisted ideas are rejected** |
| `/approve none` | Reject the whole batch |

Lines after the first line are instructions for the drafts of the ideas approved in that comment, for example
`Keep the language very simple`. Approved ideas go to `content-plan.csv` (status `pending`) and `ideas/<id>.json`;
all decisions are logged in `ideas/log.csv`. If the issue is still open two hours after the weekly proposal, a
Telegram reminder is sent (only if Telegram is set up).

### Drafts: one open PR at a time, `/regenerate`

After an approval, a merged draft, or the daily check, the draft workflow takes the next `pending` topic and opens
a PR labelled `content-draft` that adds a row to `content.csv` plus its card images in `media/`. Only one draft is
open at a time; a reminder is posted after 48 hours. **Merge before the first post date** or that post is skipped
(only rows at most `CATCHUP_HOURS` overdue are published).

Comment `/regenerate <feedback>` on the draft PR to replace it. Only you can. The old PR is closed and the topic is
drafted again from the default branch. In the feedback you can ask for a first-post date and for wording changes;
Claude reads it. Without an API key a line `date: 2026-11-20 09:30` still sets the date.

`sync-drafts.yml` runs after each push to the default branch and merges it into open draft PR branches,
resolving the append-only conflicts in `content.csv`. Anything else is left alone with a comment.

### Safety

- Commands are accepted only from the repository owner (`comment.user.login == repository_owner` and
  `author_association == 'OWNER'`); comment and issue text is read only through environment variables, never
  interpolated into shell scripts.
- The ideas stored in the issue (a base64 payload) are re-validated on approval, as is every model reply: the
  model can only return JSON through a single tool call; it has no tools and writes nothing itself.
- Medical, legal and money-claim wording ("cure", "diagnose", "guarantee", "100%", plus your own patterns) blocks an
  idea or draft; links are allowed only in the first comment, and the code adds the one link itself.
- Telegram and API errors never include tokens or keys.
- Rows are only appended to `content.csv` via a PR you merge, so a bad draft can be edited or closed.

### Cards and video

Drafts are `images` posts: a cover plus up to 3 text slides, rendered from a plain template
(`generator/render.py`) with the colours in `card_style`. The cards are 1080x1080 PNGs made with Playwright; to
test locally run `pip install -r requirements-generator.txt` and `python -m playwright install chromium`. To change the
design, edit `card_pages` in `generator/render.py`. Reel generation is not included; you can still add `reel`
rows by hand.

### Commands

```bash
python make_content.py ideas              # propose ideas (writes ideas-issue.md; needs ANTHROPIC_API_KEY)
python make_content.py next               # draft the next approved topic
python make_content.py telegram-check     # diagnose Telegram
python -m unittest discover -s tests -q   # run the tests
```

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `FB_PAGE_ID and FB_ACCESS_TOKEN must be set` | Secrets are in an Environment but the job doesn't declare it, or the names are misspelled. The workflow needs `environment: production`, and the secret names must match exactly. |
| Facebook error code `190` | Token is invalid or expired. Regenerate it (see Prerequisites, step 3). |
| Facebook error code `200` or `(#200) permissions` | Token is missing `pages_manage_posts` or `pages_manage_engagement`, or isn't a Page token. Re-check it in the Access Token Debugger. |
| Post is visible to you but not to others | The Meta app is in Development mode. Switch it to Live. |
| `me/accounts` returns an empty list | The Page wasn't selected when granting permissions, or it belongs to a Business portfolio. Use the direct `PAGE_ID?fields=access_token` call or regenerate the token and tick the Page. |
| Push step fails with `403` | The workflow needs `permissions: contents: write` (already set). Also check **Settings → Actions → General → Workflow permissions**. |
| Scheduled run never starts | Workflow must be on the default branch. Scheduled workflows can also be disabled by GitHub after long inactivity on public repos. Check the **Actions** tab. |
| Nothing was posted and the run is green | No row was due (check the date, time and timezone), the row is older than the catch-up window, or `posted.csv` already lists it. |
| Dates are rejected | Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. Excel and Sheets often convert them to `10/8/2026`; format the column as plain text or custom `yyyy-mm-dd hh:mm` before exporting. |
| Image post fails: file not found | The path is relative to the repo root, case-sensitive, and the image must be committed and pushed. |
| Reel fails: upload or processing error | Check the video against the Reel specs above (9:16, 3 to 90 s, H.264/AAC MP4). A failure before publishing is retried automatically; a processing failure needs its line deleted from `posted.csv`. |
| Image post fails: Facebook error | Try a smaller JPG or PNG. For a URL, it must serve the image directly and be publicly reachable. |
| Post went out at the wrong hour | Times are in `POST_TIMEZONE` and posts are published by the first hourly run after that time. |
| The same post published twice | Two runs overlapped, or the push of `posted.csv` failed and the row was run again. The workflow uses a `concurrency` group to prevent overlap. Check the log of the failed run. |

## Maintenance
- GitHub disables scheduled workflows in **public** repositories after 60 days without repository activity. The job's own `posted.csv` commits count as activity while you are posting; if it ever stops, re-enable it in the **Actions** tab.
- Keep the calendar stocked: add rows to `content.csv` before it runs out (a warning is logged when fewer than 3 future posts remain). Replacing `content.csv` never affects `posted.csv`.
- Meta periodically retires old Graph API versions. The version is set by `FB_GRAPH_VERSION` in `publish.py` (default `v25.0`). Check Meta's changelog now and then and update it.
- If the token is ever leaked, reset the App Secret (**App settings → Basic → Reset**) and generate a new Page token.

## License
MIT. See [LICENSE](LICENSE).
