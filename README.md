# GardenPal

GardenPal is a mobile-friendly garden app with two main workflows: saving plant ideas and mapping plants in your own yard.

## What is included right now

- Two sections:
  - **Plant Ideas** for plants you see in the world/online and want to remember
  - **Yard Planner** for plants you have actually planted in yard zones
- Account system (signup/login/logout) so each user has a private dataset
- Plant Ideas capture: common/scientific names, lookup query, plant photo, label photo, categories
- Yard Planner capture: zone photo, planted item photo, rough x/y map marker per plant
- Baseline detail fields for future lookup automation (size, watering, sun, flowering, lifecycle, spread)
- Organize plant ideas by categories (for example: "Love this", "Front porch")
- Filter plant ideas by sun exposure, lifecycle, category, and text search

## Quick start

1. Create a virtual environment:

   ```bash
   python -m venv .venv
   ```

2. Activate it:

   - Windows PowerShell:

     ```bash
     .\.venv\Scripts\Activate.ps1
     ```

3. Install in editable mode:

   ```bash
   pip install -e .
   ```

4. Run the web app:

   ```bash
   gardenpal serve
   ```

5. Open:

   - [http://127.0.0.1:5000](http://127.0.0.1:5000)

## Deploy on Vercel

1. Push this repo to GitHub.
2. In Vercel, click **Add New Project** and import `boatmarina/gardenpal`.
3. Keep defaults (Vercel will detect `vercel.json` and use `api/index.py`).
4. Deploy.

### Vercel notes

- The app runs as a Python serverless function.
- Database and uploaded images are stored in `/tmp`, which is ephemeral on Vercel.
- That means plant data/images can reset between deployments or cold starts.
- For production persistence, move storage to a managed database and object storage:
  - Database: Neon, Supabase Postgres, or Turso
  - Images: Cloudinary, S3, or Supabase Storage

## Notes

- SQLite database is created at `instance/gardenpal.db`.
- Uploaded photos are stored in `instance/uploads/`.
- Existing pre-auth plants are migrated to a starter `demo` account on first launch.

## Suggested next features

- Automated plant identification from photos (Vision API or plant ID service)
- Automated plant lookup integration (for example, Trefle or Perenual APIs)
- Edit/delete flows
- Drag-and-drop yard marker placement on zone image
- Per-zone schedule/reminders (watering, pruning, fertilizer)
