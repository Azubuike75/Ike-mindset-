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
- Everything else (env vars) is unchanged from the original setup notes.
