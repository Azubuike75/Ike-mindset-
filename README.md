# Novel Writer — what changed

Fixed 4 issues from the review:

1. **Auth added** — every route now requires HTTP Basic Auth against an
   `APP_PASSWORD` env var. Without it set, the app refuses to serve anything
   (fails loud instead of silently running open).
2. **`debug=True` removed** — now off by default; only turns on if you
   explicitly set `FLASK_DEBUG=true` locally. Never set that on Render.
3. **Gemini calls now retry** (3 attempts, 5s/10s backoff) instead of
   crashing on the first hiccup. If generation still fails, the daily job
   and "finish all" job email you an alert instead of failing silently.
4. **Per-project lock** — the daily cron, "run now", and "finish all" can no
   longer race each other on the same project.

Only kept one copy of `get_wordpress_token.py` (the two you sent were
identical).

## Deploy checklist
- Set `APP_PASSWORD` on Render (any long random string) before first deploy.
- Leave `FLASK_DEBUG` unset in production.
- Add `requirements.txt` to the repo (included in this update) — Render's
  build step needs it.
- Everything else (env vars) is unchanged from the original setup notes.

## Free-tier spin-down workaround
Render's free plan puts the app to sleep after inactivity, so the internal
06:00 daily job won't reliably fire — nothing is awake to run it.

Fix: set a `CRON_SECRET` env var (any random string), then use a free
external scheduler to hit this URL once a day:

    https://YOUR-APP-URL.onrender.com/cron/run-daily?key=YOUR_CRON_SECRET

Free options that can do this:
- **cron-job.org** (free, no card) — set it to call that URL daily around
  06:00 your time.
- **UptimeRobot** (free tier) — same idea, "monitor" the URL on a schedule.

The request itself wakes the sleeping app AND kicks off the same chapter
job the internal cron would have run. If you'd rather not deal with this,
upgrading to Render's $7/month tier keeps the app always-on and the
internal cron works as-is.

## Publishing removed, Google Docs added
WordPress/Facebook auto-publish (and the AI cover-image step that came with
it) has been removed — you review chapters by copying them, not by
publishing them externally. Every chapter has its own "View & copy" button,
plus a "Copy all chapters at once" button on the project page.

New: a "Send to Google Docs" button that automatically creates a Google Doc
per project and appends new chapters to it — no copy-paste needed. One-time
setup required first:

1. Go to **console.cloud.google.com** → create a project (any name)
2. Go to **APIs & Services → Library**, enable:
   - **Google Docs API**
   - **Google Drive API**
3. Go to **APIs & Services → OAuth consent screen**:
   - User type: External
   - Fill in app name, your email, save through the steps
   - Under "Test users," add your own Gmail address (required while the app
     is unpublished — otherwise Google blocks the login)
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Under "Authorized redirect URIs," add:
     `https://YOUR-APP-URL.onrender.com/oauth/google/callback`
   - Save — it gives you a **Client ID** and **Client Secret**
5. On Render, add env vars:
   - `GOOGLE_CLIENT_ID` = the client ID from step 4
   - `GOOGLE_CLIENT_SECRET` = the client secret from step 4
   - Redeploy
6. Visit `https://YOUR-APP-URL.onrender.com/oauth/google` in your browser,
   log into the Google account you want Docs created under, and approve
7. It'll show you a **refresh token** — copy it, add it to Render as
   `GOOGLE_REFRESH_TOKEN`, redeploy once more

After that one-time setup, "Send to Google Docs" just works — each project
gets its own Doc (created on first send), and later sends only append the
new chapters, never duplicating ones already there.
