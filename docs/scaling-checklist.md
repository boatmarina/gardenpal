# GardenPal — Scaling Checklist

Priority order within each category. Top 3 (file storage, DB connections, CSRF) are must-fix before any public launch.

---

## Infrastructure

1. **File storage** — Images are written to the local filesystem, which doesn't persist reliably across Vercel serverless invocations. Move to S3, Cloudflare R2, or Cloudinary before onboarding anyone else.

2. **Database connections** — pg8000 opens a new connection per request. Postgres has a hard connection limit (~100 on most managed plans). At modest concurrent load you'll hit it. Need PgBouncer, a connection pooler, or switch to a pooled connection string (Supabase has this built in).

3. ~~**Database indexes** — `garden_entries.user_id`, `garden_photos.entry_id`, and columns used in ORDER BY/WHERE probably have no indexes. Add them before the tables grow.~~ ✅ Done — 5 indexes added (user_id, user_id+planted_date, entry_id, entry_id+photo_date, entry_id+is_fertilization).

---

## Security

4. **CSRF protection** — All POST forms lack CSRF tokens. Flask-WTF or a simple token pattern needed; otherwise logged-in users are vulnerable to cross-site form submissions.

5. ~~**Rate limiting** — The chat endpoint and fertilization suggestion have no per-user limits. One user can spam Claude API calls and run up your bill or starve others. Flask-Limiter with Redis, or a DB counter.~~ ✅ Done — DB-based burst window (3s between chat messages) via `api_usage` table.

6. **File upload validation** — Extension checking alone (`ALLOWED_EXTENSIONS`) isn't sufficient. Validate MIME type server-side and cap file size; don't trust the client's Content-Type.

7. **Session secret key** — Verify `SECRET_KEY` is a strong random value set via environment variable, not a fallback default in code.

---

## Cost Control

8. ~~**Claude API spending caps** — Per-user daily limits on chat messages and AI fertilization suggestions. Track usage in the DB and reject calls over the limit.~~ ✅ Done — 40 chat messages/day, 100 fertilization suggestions/day per user; returns HTTP 429 when exceeded.

9. **Suggestion cache TTL** — Fertilization regen now triggers on every new note. Add a minimum re-generation interval (e.g. at most once per 24 hours per plant) to prevent runaway API calls.

---

## Reliability & Observability

10. **Error monitoring** — Add Sentry (free tier is fine) or structured logging so you know what's failing in production.

11. **Database migrations** — `ensure_column` is fine for solo development but fragile at scale. Move to Alembic migrations before you have data you can't afford to lose.

12. **Vercel function timeouts** — AI calls (chat, fertilization) can take 5–15 seconds. Vercel default is 10s on free, 60s on Pro. Audit which routes are at risk and either upgrade or move long calls to background jobs.

---

## Auth & Privacy

13. **Email verification** — Anyone can sign up with any email. Add a verification step before granting full access.

14. **Password reset** — No reset flow means locked-out users are lost forever.

15. **Account deletion + data export** — Required for GDPR if you have EU users. Time-consuming to retrofit later.

---

## Performance

16. **Pagination** — Garden index loads all entries in one query. Fine at 50 plants; starts to drag at 200+.

17. **N+1 queries** — Some pages fetch entries then loop to fetch photos/notes per entry. Audit index and detail pages for missing JOINs.

18. **CDN for static assets** — Cloudflare in front of Vercel would reduce latency and take load off serverless functions.
