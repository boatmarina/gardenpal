"""
Run GardenPal locally against the gardenpal_dev Postgres database, seeded with
a test account and sample data, for hands-on or browser-driven smoke testing.

    python3 scripts/run_local.py            # http://127.0.0.1:5001
    Login: tester / password123

Run scripts/setup_local_env.sh first. Never points at Supabase.
"""

import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEV_DATABASE_URL = os.environ.get(
    "DEV_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/gardenpal_dev"
)
if urlparse(DEV_DATABASE_URL).hostname not in ("localhost", "127.0.0.1", "::1"):
    sys.exit("Refusing to run against a non-local database.")

for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OCR_SPACE_API_KEY", "PERENUAL_API_KEY",
            "PLANT_ID_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "VERCEL"):
    os.environ.pop(key, None)
os.environ["DATABASE_URL"] = DEV_DATABASE_URL

from gardenpal.web import create_app  # noqa: E402

app = create_app()
app.config["SESSION_COOKIE_SECURE"] = False  # plain http locally

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def seed():
    c = app.test_client()
    c.post("/auth/signup", data={"username": "tester", "password": "password123", "confirm_password": "password123"})
    if c.post("/auth/login", data={"username": "tester", "password": "password123"}).status_code != 302:
        sys.exit("Could not log in as tester")
    if b"Leeks" in c.get("/garden").data:
        return
    c.post("/yard/zones/new", data={"name": "Front Bed", "description": "Full sun"})
    c.post("/garden/new", data={"plant_name": "Leeks", "variety": "Autumn Giant",
                                "planted_date": "2026-06-01", "zone_name": "Middle North"})
    c.post("/garden/new", data={"plant_name": "Tomato", "variety": "Sungold", "planted_date": "2026-05-10"})
    with app.app_context():
        from gardenpal.web import get_db
        entry_id = get_db().execute("SELECT id FROM garden_entries WHERE plant_name = 'Leeks'").fetchone()["id"]
    for i, text in enumerate(["Planted out", "Thinned to 6in", "Still harvesting regularly"]):
        c.post(f"/garden/{entry_id}/photos", data={"photo_notes": text, "photo_date": f"2026-0{7 + i}-15"}, headers=AJAX)
    c.post("/ideas/new", data={"name": "Purple Coneflower", "lookup_status": "draft", "tag_names": "Pollinator"})


if __name__ == "__main__":
    seed()
    port = int(os.environ.get("PORT", "5001"))
    print(f"GardenPal dev server: http://127.0.0.1:{port}  (login tester / password123)")
    app.run(host="127.0.0.1", port=port, debug=False)
