# GardenPal

## Deployment

Push changes to the feature branch only. Vercel automatically creates a preview deployment from the feature branch, which the user reviews and manually promotes to production via the Vercel dashboard.

Do **not** merge into `main` or push `main` — that creates a redundant production deployment that bypasses the user's review step.

## Testing — required before every push

The user should not be the first person to find a bug in a fix. Verify every change yourself before pushing.

**Setup:** `scripts/setup_local_env.sh` starts a local Postgres with `gardenpal_test` and `gardenpal_dev` databases and installs dependencies. Cloud sessions run it automatically via the SessionStart hook. Tests and the local server refuse to connect to anything but localhost — never point them at Supabase.

**Every push:**
1. Run `pytest` (about 1 minute). All tests must pass. Never push with failing tests.
2. If you changed a route, template, CSS or JS, run the app with `python3 scripts/run_local.py` (http://127.0.0.1:5001, login `tester` / `password123`, seeded with sample plants, notes and a zone). Drive the changed flow in a real browser — Playwright with an iPhone viewport, Chromium at `/opt/pw-browsers`. Check for JS errors (`pageerror`), unexpected `alert` dialogs and the expected DOM, and take a screenshot and look at it.

**Bug fixes:** first add a test that reproduces the bug and confirm it fails, then fix the bug and confirm the test passes. The test stays in the suite as a regression guard.

**New features:** add route tests for the happy path, validation failures, and another user being unable to see or change the data (see `TestIsolation` in `tests/test_garden.py`).

**What tests can't cover:** real iOS behaviour — the keyboard, safe-area insets, standalone PWA mode, and some Safari-only CSS such as sticky positioning. For changes in these areas, tell the user explicitly that the fix is unverified on a real device and what to check, rather than presenting it as done.

**Test layout:** `tests/conftest.py` (fixtures: `client`, `user_client`, `db`, `signup()`), `test_auth_and_pages.py`, `test_garden.py` (Edibles and growth log), `test_library_yard_settings.py` (Ornamentals, Yard, Settings, shared gardens, chat), `test_fertilization.py` (pure date logic). The network is blocked and API keys are unset in tests, so AI and lookup calls take their "not configured" paths.

## Database

Production is Postgres (Supabase) via pg8000, behind a SQLite-style adapter in `web.py`: write `?` placeholders. Two pitfalls that have caused real bugs:
- Rows are dict-like `_Row` objects — use `row["id"]`, never `row[0]`.
- It is not SQLite: there is no `last_insert_rowid()`. Use `INSERT ... RETURNING id`.

## What's New changelog

`WHATS_NEW_CHANGELOG` lives at the top of `create_app()` in `web.py`. Entries with `"draft": True` are never shown to users — they are staging entries awaiting approval.

**Process:**
- When shipping a user-facing feature that seems worth highlighting, add a draft entry at the top of `WHATS_NEW_CHANGELOG` with `"draft": True`.
- In the same reply where you make the change, tell the user about the draft entry and ask whether they want to publish it.
- Only set `"draft": False` (or remove the key) after the user explicitly approves the entry and its copy.
- Admin-only features must also include `"admin_only": True` so they are filtered from the public dialog.
- Never publish a What's New entry without user approval.

## Admin activity logging

Any feature significant enough to get a What's New entry should also have admin-visible usage logging so engagement can be tracked.

**Process:**
- Add one or more `action` strings to `activity_log` via `_log_activity(db, user_id, action, item_name)` in `web.py`.
- For server-side entry points (page loads, form submissions) log inline in the route handler.
- For client-side actions (button clicks with no page navigation) fire `navigator.sendBeacon('/some/log/endpoint')` wrapped in `try { } catch(e) {}` and add a matching `POST` route that calls `_log_activity`.
- Add a query for the new action(s) in the `tools()` admin route (around line 2300) alongside the existing `diary_activity`, `care_activity`, etc. queries, and pass the result to `render_template`.
- Add a display section for it in `templates/settings.html` following the same pattern as the "Diary usage" or "Watering & fertilization activity" panels (scrollable list, colour-coded badge, username, timestamp).

## Frontend Architecture

### Add-plant mechanisms
Both `idea_new.html` (Library) and `yard_plant_new.html` (Yard) have four add-plant mechanisms:
- **name** — search dropdown
- **photo** — photo identification (may show suggestion panel)
- **label** — label/tag OCR scan
- **url** — image URL

When modifying any one mechanism, verify all four still work on both pages. Changes to shared behaviour (photo display, detail chips, field population) must be applied consistently across both pages and all four modes.

### Field population
All plant detail field population MUST go through the single page-level helper:
- `applyDetails(details)` in `idea_new.html`
- `applyYardDetails(details)` in `yard_plant_new.html`

Never set individual hidden fields manually in a new code path. If a new field is added, add it to the helper — it then works everywhere automatically.

### Shared JS utilities (`garden.js`)
Reusable client-side logic lives in `garden.js` under the `GP` namespace and is available on every page. Current utilities:

| Function | Purpose |
|---|---|
| `GP.compressImage(file, maxPx, quality)` | Compress a File/Blob to JPEG |
| `GP.blobToDataURL(blob)` | Read a Blob as a base-64 data URL |
| `GP.fetchPlantData(name, count)` | Fetch photos + details in parallel |
| `GP.renderPlantDropdown(el, results, onSelect, query)` | Render scored search dropdown |
| `GP.openLightbox(src)` | Full-screen image lightbox |
| `GP.renderPhotosInGrid(containerEl, photos, altText)` | Render photo array into a `.photos-grid` with lightbox |

When adding new shared behaviour, add it to `garden.js` as `GP.*` rather than inline in templates. Template-level JS should only contain page-specific wiring.

### Photo rendering
Always use `GP.renderPhotosInGrid(containerEl, photos, altText)` to render a list of photo URLs into any `.photos-grid` div. It handles item markup, lightbox click handlers, `onerror` cleanup, and `hidden = false`. Never write inline photo-rendering loops in templates.

