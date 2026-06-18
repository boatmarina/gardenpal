import csv
import hashlib
import io
import json
import os
import secrets
import ssl
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import pg8000
import requests
from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from gardenpal.plant_lookup import extract_plant_name_from_text, extract_text_from_image, fetch_photos_for_suggestion, generate_plant_suggestion, generate_plant_suggestions_batch, identify_plant_from_image, lookup_plant_details, lookup_plant_image, lookup_plant_photos, resolve_scientific_name

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DEFAULT_CATEGORIES = ["Love this", "Front porch", "Backyard", "Wishlist", "Pollinator friendly"]

TAG_COLORS = [
    "#5B8A5F",
    "#8B6D9C",
    "#C4765A",
    "#4A7C95",
    "#B5803A",
    "#6B9E8A",
    "#C26B7E",
    "#7A8E5F",
]


def tag_color_for(name: str) -> str:
    digest = hashlib.md5(name.strip().lower().encode()).hexdigest()
    return TAG_COLORS[int(digest, 16) % len(TAG_COLORS)]


# Static edible plant suggestions: plant name -> list of common variety names.
PLANT_SUGGESTIONS = {
    "Tomato": ["Cherry", "Grape", "Roma", "Beefsteak", "San Marzano", "Celebrity", "Better Boy",
               "Early Girl", "Black Krim", "Brandywine", "Cherokee Purple", "Sungold", "Juliet",
               "Sweet 100", "Mortgage Lifter", "Yellow Pear"],
    "Cherry Tomato": ["Sungold", "Sweet 100", "Black Cherry", "Chocolate Cherry", "Yellow Pear", "Sun Gold"],
    "Pepper": ["Bell", "Jalapeño", "Serrano", "Habanero", "Banana", "Anaheim", "Poblano",
               "Cayenne", "Sweet Italian", "Shishito", "Cubanelle"],
    "Bell Pepper": ["California Wonder", "King of the North", "Red Knight", "Yolo Wonder"],
    "Hot Pepper": ["Jalapeño", "Serrano", "Habanero", "Ghost Pepper", "Carolina Reaper", "Thai Bird"],
    "Eggplant": ["Black Beauty", "Ichiban", "Fairytale", "Graffiti", "Thai", "White", "Listada de Gandia"],
    "Cucumber": ["Pickling", "Slicing", "English", "Armenian", "Lemon", "Marketmore",
                 "Straight Eight", "Muncher", "Spacemaster"],
    "Zucchini": ["Black Beauty", "Costata Romanesco", "Golden", "Cocozelle", "Patio Star"],
    "Summer Squash": ["Yellow Crookneck", "Pattypan", "Eight Ball", "Zephyr", "Scallopini"],
    "Winter Squash": ["Butternut", "Acorn", "Spaghetti", "Delicata", "Kabocha", "Hubbard", "Red Kuri", "Carnival"],
    "Butternut Squash": ["Waltham", "Hunter", "Butterscotch", "Honeynut"],
    "Pumpkin": ["Jack o' Lantern", "Sugar Pie", "Cinderella", "Baby Boo", "Atlantic Giant", "Howden"],
    "Watermelon": ["Sugar Baby", "Crimson Sweet", "Charleston Gray", "Jubilee", "Mini Seedless"],
    "Cantaloupe": ["Honey Rock", "Ambrosia", "Hale's Best", "Hearts of Gold", "Athena"],
    "Corn": ["Sweet", "Peaches & Cream", "Silver Queen", "Bodacious", "Honey Select", "Candy Corn"],
    "Green Bean": ["Bush", "Pole", "Kentucky Wonder", "Blue Lake", "Provider", "Dragon Tongue", "Romano", "Contender"],
    "Pea": ["Sugar Snap", "Snow", "Shelling", "Lincoln", "Little Marvel", "Sugar Ann", "Oregon Sugar Pod"],
    "Edamame": ["Midori Giant", "Chiba Green", "Beer Friend"],
    "Lima Bean": ["Fordhook", "Henderson", "King of the Garden", "Christmas"],
    "Lettuce": ["Romaine", "Butterhead", "Iceberg", "Looseleaf", "Red Leaf", "Green Leaf", "Bibb", "Little Gem", "Buttercrunch"],
    "Spinach": ["Baby", "Bloomsdale", "Savoy", "Regiment", "Tyee", "Catalina"],
    "Kale": ["Curly", "Lacinato", "Dinosaur", "Red Russian", "Redbor", "Siberian", "Winterbor"],
    "Swiss Chard": ["Rainbow", "Ruby Red", "Bright Lights", "Fordhook Giant", "Peppermint"],
    "Arugula": ["Astro", "Wild", "Slow Bolt", "Sylvetta"],
    "Bok Choy": ["Baby", "Shanghai", "Joi Choi"],
    "Collard Greens": ["Georgia Southern", "Flash", "Champion", "Vates"],
    "Mustard Greens": ["Red Giant", "Southern Giant", "Tendergreen"],
    "Radicchio": ["Chioggia", "Treviso", "Palla Rossa"],
    "Carrot": ["Nantes", "Chantenay", "Danvers", "Imperator", "Purple", "Rainbow", "Bolero", "Scarlet Nantes"],
    "Beet": ["Detroit Dark Red", "Chioggia", "Golden", "Bull's Blood", "Red Ace", "Cylindra"],
    "Radish": ["French Breakfast", "Cherry Belle", "Daikon", "Watermelon", "Easter Egg"],
    "Turnip": ["Purple Top White Globe", "Hakurei", "Tokyo Market"],
    "Parsnip": ["Hollow Crown", "Harris Model", "Javelin"],
    "Kohlrabi": ["Early White Vienna", "Purple Vienna", "Gigante"],
    "Sweet Potato": ["Beauregard", "Jewel", "Covington", "Purple", "Japanese"],
    "Potato": ["Yukon Gold", "Russet", "Red Pontiac", "Fingerling", "Blue", "Kennebec", "All Blue"],
    "Onion": ["Yellow", "White", "Red", "Vidalia", "Walla Walla", "Sweet", "Bunching", "Cipollini"],
    "Garlic": ["Hardneck", "Softneck", "Elephant", "Rocambole", "Silverskin", "Porcelain", "Music", "Russian Red"],
    "Green Onion": ["Evergreen", "Tokyo Long White", "Parade", "Guardsman"],
    "Shallot": ["French Red", "Banana", "Dutch Yellow"],
    "Leek": ["Giant Musselburgh", "King Richard", "Autumn Giant", "Lancelot"],
    "Chives": ["Common", "Garlic Chive"],
    "Broccoli": ["Calabrese", "Romanesco", "Di Cicco", "Belstar", "Green Magic", "Waltham 29"],
    "Cauliflower": ["White", "Purple", "Orange", "Romanesco", "Snowball", "Graffiti", "Cheddar"],
    "Cabbage": ["Green", "Red", "Savoy", "Napa", "Pointed", "January King"],
    "Brussels Sprouts": ["Long Island", "Jade Cross", "Churchill", "Diablo"],
    "Celery": ["Utah", "Tango", "Pascal", "Golden Self-Blanching"],
    "Fennel": ["Florence", "Bronze", "Sweet", "Perfection"],
    "Basil": ["Sweet", "Genovese", "Purple", "Thai", "Lemon", "Cinnamon", "Dark Opal", "Spicy Globe"],
    "Parsley": ["Flat-leaf", "Italian", "Curly", "Hamburg"],
    "Cilantro": ["Santo", "Calypso", "Leisure", "Slow Bolt"],
    "Dill": ["Fernleaf", "Bouquet", "Mammoth", "Hera"],
    "Mint": ["Spearmint", "Peppermint", "Apple", "Chocolate", "Mojito", "Lemon"],
    "Thyme": ["English", "French", "Lemon", "Creeping", "German Winter"],
    "Rosemary": ["Tuscan Blue", "Arp", "Prostratus", "Barbecue"],
    "Oregano": ["Greek", "Italian", "Mexican", "Hot & Spicy"],
    "Sage": ["Common", "Purple", "Berggarten", "Tricolor", "Pineapple"],
    "Tarragon": ["French", "Russian"],
    "Lavender": ["English", "French", "Hidcote", "Munstead", "Provence"],
    "Chamomile": ["German", "Roman"],
    "Lemongrass": [],
    "Lemon Balm": [],
    "Stevia": [],
    "Strawberry": ["Chandler", "Albion", "Seascape", "Earliglow", "Honeoye", "Fort Laramie", "Alexandria"],
    "Raspberry": ["Red", "Black", "Yellow", "Heritage", "Anne", "Fall Gold", "Caroline", "Nova"],
    "Blueberry": ["Highbush", "Lowbush", "Duke", "Bluecrop", "Patriot", "Sunshine Blue", "Top Hat"],
    "Blackberry": ["Thornless", "Triple Crown", "Apache", "Chester", "Prime-Ark", "Ouachita"],
    "Rhubarb": ["Victoria", "Canada Red", "Crimson Red"],
    "Currant": ["Red Lake", "Perfection", "Titania", "Black"],
    "Fig": ["Brown Turkey", "Celeste", "Chicago Hardy", "Black Mission", "Kadota"],
    "Grape": ["Concord", "Thompson Seedless", "Red Flame", "Niagara", "Marquette", "Reliance"],
    "Asparagus": ["Jersey Knight", "Jersey Giant", "Mary Washington", "Purple Passion"],
    "Artichoke": ["Green Globe", "Imperial Star", "Purple of Romagna"],
    "Okra": ["Clemson Spineless", "Burgundy", "Emerald"],
    "Tomatillo": ["Verde", "Purple", "Pineapple", "Grande Rio Verde"],
    "Ground Cherry": ["Cossack Pineapple", "Aunt Molly's"],
    "Sunflower": ["Mammoth", "Autumn Beauty", "Teddy Bear", "Lemon Queen"],
    "Sorrel": ["French", "Red-veined", "Garden"],
    "Nasturtium": ["Alaska", "Jewel Mix", "Empress of India", "Climbing"],
    "Rutabaga": ["American Purple Top", "Laurentian"],
}


class _Row:
    """Dict- and attribute-accessible row, like sqlite3.Row."""

    def __init__(self, description, values):
        object.__setattr__(self, "_data", dict(zip([d[0] for d in description], values)))

    def __getitem__(self, key):
        return self._data[key]

    def __getattr__(self, key):
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(key)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def __contains__(self, key):
        return key in self._data


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        return None if row is None else _Row(self._cur.description, row)

    def fetchall(self):
        return [_Row(self._cur.description, row) for row in self._cur.fetchall()]


class _PgDB:
    """Thin adapter giving pg8000 a SQLite-like execute() interface."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, query, params=()):
        cur = self._conn.cursor()
        cur.execute(query.replace("?", "%s"), params or ())
        return _PgCursor(cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _connect(database_url: str) -> _PgDB:
    parsed = urlparse(database_url)
    if not parsed.hostname or not parsed.username:
        raise ValueError(
            f"DATABASE_URL could not be parsed (scheme={parsed.scheme!r}, "
            f"host={parsed.hostname!r}, user={parsed.username!r}). "
            f"It must be a URI in the form: "
            f"postgresql://postgres:YOUR_PASSWORD@db.PROJECT.supabase.co:5432/postgres"
        )
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    is_local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
    conn = pg8000.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password or None,
        ssl_context=None if is_local else ssl_ctx,
    )
    return _PgDB(conn)


def _parse_date_to_iso(value: str, today: str) -> str | None:
    """Normalize a date string to YYYY-MM-DD; returns None if blank or unparseable."""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    # Already ISO
    import re
    if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        return v
    # Try common natural-language formats
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                "%B %d", "%b %d", "%m/%d/%Y", "%m/%d/%y", "%d %B %Y", "%d %b %Y"):
        try:
            parsed = datetime.strptime(v, fmt)
            # Formats without a year default to the current year
            if "%Y" not in fmt and "%y" not in fmt:
                parsed = parsed.replace(year=datetime.strptime(today, "%Y-%m-%d").year)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Unknown format — store as-is and let the DB reject if invalid
    return v


def _log_chat_error(db, user_id, username, user_message, error_type, error_detail=""):
    try:
        db.execute(
            """INSERT INTO chat_error_log (user_id, username, user_message, error_type, error_detail, logged_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, username, (user_message or "")[:500], error_type, (error_detail or "")[:500],
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        db.commit()
    except Exception:
        pass  # never let logging break the response


def _log_ai_chat(db, user_id, username, context, user_message, plant_name=None):
    """Log a successful AI chat message for admin visibility."""
    try:
        db.execute(
            "INSERT INTO ai_chat_log (user_id, username, context, plant_name, user_message, logged_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, context, plant_name, (user_message or "")[:1000],
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        db.commit()
    except Exception:
        pass


def create_app() -> Flask:
    # Newest first. Add a new entry at the top when releasing a feature.
    # WHATS_NEW_VERSION must always equal WHATS_NEW_CHANGELOG[0]["version"].
    WHATS_NEW_CHANGELOG = [
        {
            "version": "2026-06-e",
            "title": "Your garden assistant, on the home screen",
            "body": "Tap \u201cAsk your garden assistant\u201d to get instant help with your whole garden \u2014 ask care questions, log notes, or make changes hands-free. Try things like \u201cWhen should I fertilize my tomatoes?\u201d or \u201cAdd zucchini to the Front Bed, planted today.\u201d",
        },
        {
            "version": "2026-06-d",
            "title": "Plant suggestions on your home screen",
            "body": "GardenPal now suggests an ornamental plant each time you log in, tailored to your location and existing collection. Tap to see photos, care details, and a one-tap add to your library.",
        },
        {
            "version": "2026-06-c",
            "title": "Ask AI about any ornamental plant",
            "body": "Every ornamental plant detail page now has an Ask AI panel at the bottom. Ask care questions, pruning tips, or anything about the plant — it knows your location and any notes you've logged.",
        },
        {
            "version": "2026-06-b",
            "title": "Edible plants in zones, now AI-assignable",
            "body": "Assign edible plants to a yard zone from any plant's detail page or the Yard tab. The garden assistant can also move plants between zones — try: \"Put my tomatoes in the Front Bed.\"",
        },
        {
            "version": "2026-06-a",
            "title": "Impersonate users (admin)",
            "body": "Admins can now tap View As on any user's profile to see the app exactly as they see it, without logging out.",
            "admin_only": True,
        },
    ]
    WHATS_NEW_VERSION = WHATS_NEW_CHANGELOG[0]["version"]

    app = Flask(__name__, instance_relative_config=True)
    upload_dir = Path("/tmp/gardenpal/uploads") if os.environ.get("VERCEL") else Path(app.instance_path) / "uploads"
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        UPLOAD_FOLDER=str(upload_dir),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        _DB_READY=False,
    )

    os.makedirs(upload_dir, exist_ok=True)

    @app.template_filter("media_url")
    def media_url_filter(path):
        if not path:
            return ""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return url_for("uploads", filename=path)

    @app.template_filter("first_photo")
    def first_photo_filter(photo_urls_json):
        if not photo_urls_json:
            return None
        try:
            urls = json.loads(photo_urls_json)
            return urls[0] if urls else None
        except Exception:
            return None

    @app.template_filter("month_day")
    def month_day_filter(date_str):
        if not date_str:
            return ""
        try:
            from datetime import date
            d = date.fromisoformat(str(date_str)[:10])
            return d.strftime("%B %-d")
        except Exception:
            return str(date_str)

    @app.template_filter("planted_date")
    def planted_date_filter(date_str):
        if not date_str:
            return ""
        try:
            from datetime import date
            d = date.fromisoformat(str(date_str)[:10])
            today = date.today()
            one_year_ago = today.replace(year=today.year - 1)
            if d >= one_year_ago:
                return d.strftime("%b %-d")
            else:
                return d.strftime("%b %Y")
        except Exception:
            return date_str

    @app.template_filter("relative_login")
    def relative_login_filter(dt_str):
        if not dt_str:
            return "never"
        try:
            dt = datetime.fromisoformat(str(dt_str)[:19])
            days = (datetime.utcnow() - dt).days
            if days <= 0:
                return "today"
            if days == 1:
                return "yesterday"
            if days < 7:
                return f"{days}d ago"
            if days < 14:
                return "last week"
            weeks = days // 7
            if weeks < 5:
                return f"{weeks}w ago"
            return "over a month ago"
        except Exception:
            return str(dt_str)[:10]

    @app.before_request
    def ensure_db_ready():
        if not app.config["_DB_READY"]:
            db_url = app.config.get("DATABASE_URL")
            if not db_url:
                return "<pre>Error: DATABASE_URL environment variable is not set.</pre>", 500
            try:
                init_db()
                app.config["_DB_READY"] = True
            except Exception as exc:
                return f"<pre>Database connection failed:\n{exc}</pre>", 500

    @app.before_request
    def load_logged_in_user():
        user_id = session.get("user_id")
        g.user = None
        g.real_admin = None
        if user_id is not None:
            real_user = get_db().execute("SELECT id, username, api_token, is_admin, photo_id_provider, location, whats_new_seen FROM users WHERE id = ?", (user_id,)).fetchone()
            impersonating_id = session.get("impersonating_id")
            if impersonating_id and real_user and real_user["is_admin"]:
                impersonated = get_db().execute("SELECT id, username, api_token, is_admin, photo_id_provider, location, whats_new_seen FROM users WHERE id = ?", (impersonating_id,)).fetchone()
                if impersonated:
                    g.real_admin = real_user
                    g.user = impersonated
                else:
                    session.pop("impersonating_id", None)
                    g.user = real_user
            else:
                g.user = real_user

    @app.context_processor
    def inject_auth_user():
        user = g.get("user")
        real_admin = g.get("real_admin")
        whats_new_entries = []
        if user and not real_admin:
            seen = user.get("whats_new_seen")
            seen_idx = next(
                (i for i, e in enumerate(WHATS_NEW_CHANGELOG) if e["version"] == seen),
                len(WHATS_NEW_CHANGELOG),
            )
            whats_new_entries = [e for e in WHATS_NEW_CHANGELOG[:seen_idx] if not e.get("admin_only") and not e.get("draft")][:5]
        return {
            "current_user": user,
            "show_whats_new": bool(whats_new_entries),
            "whats_new_entries": whats_new_entries,
            "real_admin": real_admin,
        }

    @app.route("/auth/signup", methods=["GET", "POST"])
    def signup():
        if g.user:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            db = get_db()

            if len(username) < 3:
                flash("Username must be at least 3 characters.")
                return render_template("signup.html")
            if len(password) < 8:
                flash("Password must be at least 8 characters.")
                return render_template("signup.html")
            if password != confirm_password:
                flash("Passwords do not match.")
                return render_template("signup.html")

            exists = db.execute("SELECT id FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
            if exists:
                flash("That username is already taken.")
                return render_template("signup.html")

            db.execute(
                "INSERT INTO users (username, password_hash, whats_new_seen, created_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), WHATS_NEW_VERSION, datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.commit()

            user = db.execute("SELECT id, username FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
            session.clear()
            session["user_id"] = user["id"]
            db.execute(
                "INSERT INTO login_log (user_id, logged_in_at) VALUES (?, ?)",
                (user["id"], datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.commit()
            return redirect(url_for("dashboard"))

        return render_template("signup.html")

    @app.route("/auth/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            db = get_db()
            user = db.execute(
                "SELECT id, username, password_hash FROM users WHERE lower(username) = lower(?)",
                (username,),
            ).fetchone()

            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Invalid username or password.")
                return render_template("login.html")

            session.clear()
            session["user_id"] = user["id"]
            db.execute(
                "INSERT INTO login_log (user_id, logged_in_at) VALUES (?, ?)",
                (user["id"], datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.commit()
            return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.route("/whats-new/dismiss", methods=["POST"])
    @login_required
    def whats_new_dismiss():
        get_db().execute(
            "UPDATE users SET whats_new_seen = ? WHERE id = ?",
            (WHATS_NEW_VERSION, g.user["id"]),
        )
        get_db().commit()
        return ("", 204)

    @app.route("/auth/logout", methods=["POST"])
    def logout():
        session.clear()
        flash("You have been logged out.")
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)
        idea_count = db.execute(f"SELECT COUNT(*) AS count FROM plants WHERE user_id IN {ph}", id_args).fetchone()["count"]
        zone_count = db.execute(
            f"SELECT COUNT(*) AS count FROM yard_zones WHERE user_id IN {ph}",
            id_args,
        ).fetchone()["count"]
        yard_plant_count = db.execute(
            f"SELECT COUNT(*) AS count FROM yard_plants WHERE user_id IN {ph}",
            id_args,
        ).fetchone()["count"]
        garden_count = db.execute(
            f"SELECT COUNT(*) AS count FROM garden_entries WHERE user_id IN {ph}",
            id_args,
        ).fetchone()["count"]

        today = _local_today()
        fert_alerts = []
        ff_fert = _feature_fertilization(g.user)
        if ff_fert:
            deadline = _local_date_plus(3)
            edible_rows = db.execute(
                f"SELECT id, plant_name AS name, next_fertilization_date, planned_fertilization_date,"
                f" last_fertilized_date, last_fertilizer_type, next_fertilization_note, never_fertilize"
                f" FROM garden_entries WHERE user_id IN {ph}"
                f" AND (never_fertilize IS NULL OR never_fertilize = 0)"
                f" AND next_fertilization_date IS NOT NULL"
                f" AND COALESCE(planned_fertilization_date, next_fertilization_date) <= ?",
                id_args + [deadline],
            ).fetchall()
            for r in edible_rows:
                eff = r["planned_fertilization_date"] or r["next_fertilization_date"]
                fert_alerts.append({
                    "kind": "edible", "id": r["id"], "name": r["name"],
                    "date": eff, "overdue": eff < today,
                    "last_fertilized_date": r["last_fertilized_date"],
                    "last_fertilizer_type": r["last_fertilizer_type"],
                    "next_fertilization_note": r["next_fertilization_note"],
                    "planned_date": r["planned_fertilization_date"],
                    "never": bool(r["never_fertilize"]),
                })
            ornamental_rows = db.execute(
                f"SELECT id, name, next_fertilization_date, planned_fertilization_date,"
                f" last_fertilized_date, last_fertilizer_type, next_fertilization_note, never_fertilize"
                f" FROM plants WHERE user_id IN {ph}"
                f" AND (never_fertilize IS NULL OR never_fertilize = 0)"
                f" AND next_fertilization_date IS NOT NULL"
                f" AND COALESCE(planned_fertilization_date, next_fertilization_date) <= ?",
                id_args + [deadline],
            ).fetchall()
            for r in ornamental_rows:
                eff = r["planned_fertilization_date"] or r["next_fertilization_date"]
                fert_alerts.append({
                    "kind": "ornamental", "id": r["id"], "name": r["name"],
                    "date": eff, "overdue": eff < today,
                    "last_fertilized_date": r["last_fertilized_date"],
                    "last_fertilizer_type": r["last_fertilizer_type"],
                    "next_fertilization_note": r["next_fertilization_note"],
                    "planned_date": r["planned_fertilization_date"],
                    "never": bool(r["never_fertilize"]),
                })
            fert_alerts.sort(key=lambda x: x["date"])

        watering_alerts = []
        ff_water = _feature_watering(g.user)
        if ff_water:
            water_today = today
            water_deadline = _local_date_plus(1)
            water_edible_rows = db.execute(
                f"SELECT id, plant_name AS name, last_watered_date, next_watering_date,"
                f" watering_note, watering_frequency_days"
                f" FROM garden_entries WHERE user_id IN {ph}"
                f" AND (never_water IS NULL OR never_water = 0)"
                f" AND watering_frequency_days IS NOT NULL"
                f" AND next_watering_date IS NOT NULL"
                f" AND next_watering_date <= ?",
                id_args + [water_deadline],
            ).fetchall()
            for r in water_edible_rows:
                watering_alerts.append({
                    "kind": "edible", "id": r["id"], "name": r["name"],
                    "date": r["next_watering_date"],
                    "overdue": r["next_watering_date"] < water_today,
                    "last_watered_date": r["last_watered_date"],
                    "watering_note": r["watering_note"],
                    "frequency_days": r["watering_frequency_days"],
                })
            water_ornamental_rows = db.execute(
                f"SELECT id, name, last_watered_date, next_watering_date,"
                f" watering_note, watering_frequency_days"
                f" FROM plants WHERE user_id IN {ph}"
                f" AND (never_water IS NULL OR never_water = 0)"
                f" AND watering_frequency_days IS NOT NULL"
                f" AND next_watering_date IS NOT NULL"
                f" AND next_watering_date <= ?",
                id_args + [water_deadline],
            ).fetchall()
            for r in water_ornamental_rows:
                watering_alerts.append({
                    "kind": "ornamental", "id": r["id"], "name": r["name"],
                    "date": r["next_watering_date"],
                    "overdue": r["next_watering_date"] < water_today,
                    "last_watered_date": r["last_watered_date"],
                    "watering_note": r["watering_note"],
                    "frequency_days": r["watering_frequency_days"],
                })
            watering_alerts.sort(key=lambda x: x["date"])

        return render_template(
            "dashboard.html",
            idea_count=idea_count,
            zone_count=zone_count,
            yard_plant_count=yard_plant_count,
            garden_count=garden_count,
            feature_home_assistant=_feature_home_assistant(g.user),
            ff_fert=ff_fert,
            fert_alerts=fert_alerts,
            ff_water=ff_water,
            watering_alerts=watering_alerts,
            today=today,
        )

    @app.route("/fert-never", methods=["POST"])
    @login_required
    def fert_never():
        kind = request.form.get("kind", "")
        item_id = request.form.get("id", "")
        if not item_id.isdigit():
            return redirect(url_for("dashboard"))
        item_id = int(item_id)
        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)
        if kind == "edible":
            db.execute(
                f"UPDATE garden_entries SET never_fertilize = 1, planned_fertilization_date = NULL,"
                f" next_fertilization_date = NULL, next_fertilization_note = NULL,"
                f" next_fertilization_generated_at = NULL"
                f" WHERE id = ? AND user_id IN {ph}",
                [item_id] + id_args,
            )
        elif kind == "ornamental":
            db.execute(
                f"UPDATE plants SET never_fertilize = 1, planned_fertilization_date = NULL,"
                f" next_fertilization_date = NULL, next_fertilization_note = NULL,"
                f" next_fertilization_generated_at = NULL"
                f" WHERE id = ? AND user_id IN {ph}",
                [item_id] + id_args,
            )
        db.commit()
        return redirect(url_for("dashboard"))

    @app.route("/ideas")
    @login_required
    def ideas_index():
        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)
        shared_names = _shared_user_names(db, user_id)
        q = request.args.get("q", "").strip()
        sun = request.args.get("sun", "").strip()
        lifecycle = request.args.get("lifecycle", "").strip()
        evergreen = request.args.get("evergreen", "").strip()
        plant_form = request.args.get("plant_form", "").strip()
        height_category = request.args.get("height_category", "").strip()
        water_needs = request.args.get("water_needs", "").strip()
        category_id = request.args.get("category", "").strip()
        tag_id = request.args.get("tag", "").strip()

        query = f"""
            SELECT p.*
            FROM plants p
            WHERE p.user_id IN {ph}
        """
        params = list(id_args)

        if q:
            query += " AND (p.name LIKE ? OR p.notes LIKE ? OR p.source_note LIKE ?)"
            like_q = f"%{q}%"
            params.extend([like_q, like_q, like_q])
        if sun:
            query += " AND p.sun_exposure = ?"
            params.append(sun)
        if lifecycle:
            query += " AND p.lifecycle = ?"
            params.append(lifecycle)
        if evergreen:
            query += " AND p.evergreen_status = ?"
            params.append(evergreen)
        if plant_form:
            query += " AND p.plant_form = ?"
            params.append(plant_form)
        if height_category:
            query += " AND p.height_category = ?"
            params.append(height_category)
        if water_needs:
            query += " AND p.water_needs = ?"
            params.append(water_needs)
        if category_id:
            query += " AND EXISTS (SELECT 1 FROM plant_categories WHERE plant_id = p.id AND category_id = ?)"
            params.append(category_id)
        if tag_id:
            query += " AND p.id IN (SELECT plant_id FROM plant_tags WHERE tag_id = ?)"
            params.append(tag_id)

        query += " ORDER BY p.created_at DESC"
        plants = db.execute(query, params).fetchall()

        # Build tags map: plant_id → list of tag dicts
        tags_map = {}
        if plants:
            plant_ids = [p["id"] for p in plants]
            placeholders = ",".join("?" * len(plant_ids))
            tag_rows = db.execute(
                f"SELECT pt.plant_id, t.id, t.name, t.color FROM plant_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.plant_id IN ({placeholders}) ORDER BY t.name ASC",
                plant_ids,
            ).fetchall()
            for row in tag_rows:
                pid = row["plant_id"]
                if pid not in tags_map:
                    tags_map[pid] = []
                tags_map[pid].append({"id": row["id"], "name": row["name"], "color": row["color"]})

        # Tags that exist on any visible plant (for filter bar)
        user_tags = [
            {"id": t["id"], "name": t["name"], "color": t["color"]}
            for t in db.execute(
                f"""
                SELECT DISTINCT t.id, t.name, t.color
                FROM tags t
                JOIN plant_tags pt ON pt.tag_id = t.id
                JOIN plants p ON p.id = pt.plant_id
                WHERE p.user_id IN {ph}
                ORDER BY t.name ASC
                """,
                id_args,
            ).fetchall()
        ]

        categories = db.execute(
            f"""
            SELECT DISTINCT c.*
            FROM categories c
            JOIN plant_categories pc ON c.id = pc.category_id
            JOIN plants p ON p.id = pc.plant_id
            WHERE p.user_id IN {ph}
            ORDER BY c.name ASC
            """,
            id_args,
        ).fetchall()
        _today_str = _local_today()
        _fert_deadline = _local_date_plus(3)
        _water_deadline = _local_date_plus(1)
        zoned_plant_names = set()
        if plants:
            _zoned = db.execute(
                f"SELECT DISTINCT lower(plant_name) AS pn FROM yard_plants WHERE user_id IN {ph}",
                list(id_args),
            ).fetchall()
            zoned_plant_names = {r["pn"] for r in _zoned}
        return render_template(
            "ideas_index.html",
            plants=plants,
            tags_map=tags_map,
            user_tags=user_tags,
            categories=categories,
            shared_names=shared_names,
            active_filters={"q": q, "sun": sun, "lifecycle": lifecycle, "evergreen": evergreen, "plant_form": plant_form, "height_category": height_category, "water_needs": water_needs, "category": category_id, "tag": tag_id},
            today=_today_str,
            fert_deadline=_fert_deadline,
            ff_fert=_feature_fertilization(g.user),
            water_deadline=_water_deadline,
            ff_water=_feature_watering(g.user),
            zoned_plant_names=zoned_plant_names,
        )

    @app.route("/ideas/new", methods=["GET", "POST"])
    @login_required
    def new_idea():
        db = get_db()
        user_id = g.user["id"]
        _lib_rows = db.execute(
            "SELECT DISTINCT name, scientific_name FROM plants WHERE user_id = ? ORDER BY name ASC",
            (user_id,),
        ).fetchall()
        plant_names    = [r["name"] for r in _lib_rows]
        library_plants = [{"name": r["name"], "sci": r["scientific_name"] or ""} for r in _lib_rows]

        form_values = {
            "name": "",
            "scientific_name": "",
            "lookup_query": "",
            "source_type": "world",
            "source_note": "",
            "image_url": "",
            "size_info": "",
            "flowering_schedule": "",
            "sun_exposure": "",
            "lifecycle": "",
            "notes": "",
            "lookup_status": "not-started",
            "pnw_native": None,
            "photo_urls": "",
            "evergreen_status": "",
            "plant_form": "",
            "height_category": "",
            "description": "",
            "water_needs": "",
            "deadheading": "",
            "deer_resistant": "",
            "active_mode": "name",
        }

        if request.method == "POST":
            form_action = request.form.get("form_action", "save")
            pnw_raw = request.form.get("pnw_native", "")
            form_values = {
                "name": request.form.get("name", "").strip(),
                "scientific_name": request.form.get("scientific_name", "").strip(),
                "lookup_query": request.form.get("lookup_query", "").strip(),
                "source_type": request.form.get("source_type", "world").strip(),
                "source_note": request.form.get("source_note", "").strip(),
                "image_url": request.form.get("image_url", "").strip(),
                "size_info": request.form.get("size_info", "").strip(),
                "flowering_schedule": request.form.get("flowering_schedule", "").strip(),
                "sun_exposure": request.form.get("sun_exposure", "").strip(),
                "lifecycle": request.form.get("lifecycle", "").strip(),
                "notes": request.form.get("notes", "").strip(),
                "lookup_status": request.form.get("lookup_status", "not-started").strip(),
                "pnw_native": True if pnw_raw == "1" else (False if pnw_raw == "0" else None),
                "photo_urls": request.form.get("photo_urls", "").strip(),
                "evergreen_status": request.form.get("evergreen_status", "").strip(),
                "plant_form": request.form.get("plant_form", "").strip(),
                "height_category": request.form.get("height_category", "").strip(),
                "description": request.form.get("description", "").strip(),
                "water_needs": request.form.get("water_needs", "").strip(),
                "deadheading": request.form.get("deadheading", "").strip(),
                "deer_resistant": request.form.get("deer_resistant", "").strip(),
                "active_mode": request.form.get("active_mode", "name").strip(),
                "pre_uploaded_image_path": request.form.get("pre_uploaded_image_path", "").strip(),
                "photo_id_suggestions": request.form.get("photo_id_suggestions", "").strip(),
            }

            if form_action == "autofill_name":
                details, error = lookup_plant_details(form_values["lookup_query"] or form_values["name"], location=g.user.get("location"))
                if error:
                    flash(error)
                else:
                    apply_lookup_to_form(form_values, details, use_common_name=True)
                    flash("Plant details autofilled.")
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names, library_plants=library_plants)

            if form_action == "autofill_label":
                text, error = extract_text_from_image(request.files.get("label_photo"))
                if error:
                    flash(error)
                else:
                    guessed = infer_query_from_text(text)
                    if guessed:
                        if not form_values["name"]:
                            form_values["name"] = guessed
                        form_values["lookup_query"] = guessed
                        form_values["active_mode"] = "name"
                        flash(f"Label read: \"{guessed}\" — review the name then save.")
                    else:
                        flash("Could not extract a plant name from that label.")
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names, library_plants=library_plants)

            if form_action == "autofill_photo":
                photo_file = request.files.get("photo")
                if not photo_file or not photo_file.filename:
                    flash("Please choose a photo first.")
                    return render_template("idea_new.html", form_values=form_values, plant_names=plant_names, library_plants=library_plants)
                suggestions, error = identify_plant_from_image(photo_file, provider=g.user.get("photo_id_provider") or "claude", location=g.user.get("location"))
                if error:
                    flash(error)
                elif suggestions:
                    top = suggestions[0]
                    if top.get("common_name") and not form_values["name"]:
                        form_values["name"] = top["common_name"]
                    if top.get("scientific_name"):
                        form_values["scientific_name"] = top["scientific_name"]
                        form_values["lookup_query"] = top["scientific_name"]
                    form_values["photo_id_suggestions"] = json.dumps(suggestions)
                    details, lookup_error = lookup_plant_details(form_values["lookup_query"] or form_values["name"], location=g.user.get("location"))
                    if not lookup_error:
                        apply_lookup_to_form(form_values, details, use_common_name=not form_values["name"])
                # Save photo now so it survives the round-trip (identify already seeked stream back to 0)
                if not form_values["pre_uploaded_image_path"]:
                    saved_path = save_upload(photo_file, app.config["UPLOAD_FOLDER"], user_id, "idea")
                    if saved_path:
                        form_values["pre_uploaded_image_path"] = saved_path
                form_values["image_url"] = ""  # keep user's own photo, not API images
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names, library_plants=library_plants)

            if not form_values["name"]:
                flash("Plant name is required.")
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names, library_plants=library_plants)

            # Auto-fill details on save if not already done via an explicit autofill action
            if form_values["lookup_status"] != "draft":
                details, lookup_err = lookup_plant_details(form_values["lookup_query"] or form_values["name"], location=g.user.get("location"))
                if details:
                    apply_lookup_to_form(form_values, details, use_common_name=False)
                elif lookup_err:
                    flash(f"Plant details could not be fetched: {lookup_err}")

            image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"], user_id, "idea")
            if not image_path:
                image_path = form_values.get("pre_uploaded_image_path", "")
            label_photo_path = save_upload(
                request.files.get("label_photo"),
                app.config["UPLOAD_FOLDER"],
                user_id,
                "label",
            )
            plant_id = db.execute(
                """
                INSERT INTO plants
                (user_id, name, scientific_name, lookup_query, source_type, source_note, image_path, label_photo_path,
                 image_url, size_info, flowering_schedule, sun_exposure, lifecycle, lookup_status, notes, pnw_native, photo_urls, evergreen_status, plant_form, height_category, description, water_needs, deadheading, deer_resistant, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    user_id,
                    form_values["name"],
                    form_values["scientific_name"],
                    form_values["lookup_query"],
                    form_values["source_type"],
                    form_values["source_note"],
                    image_path,
                    label_photo_path,
                    form_values["image_url"],
                    form_values["size_info"],
                    form_values["flowering_schedule"],
                    form_values["sun_exposure"],
                    form_values["lifecycle"],
                    form_values["lookup_status"],
                    form_values["notes"],
                    1 if form_values["pnw_native"] is True else (0 if form_values["pnw_native"] is False else None),
                    form_values["photo_urls"] or None,
                    form_values["evergreen_status"] or None,
                    form_values["plant_form"] or None,
                    form_values["height_category"] or None,
                    form_values["description"] or None,
                    form_values["water_needs"] or None,
                    form_values["deadheading"] or None,
                    form_values["deer_resistant"] or None,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            ).fetchone()["id"]

            tag_names_raw = request.form.get("tag_names", "").strip()
            if tag_names_raw:
                for tn in [t.strip() for t in tag_names_raw.split(",") if t.strip()]:
                    tn = tn[:50]
                    color = tag_color_for(tn)
                    et = db.execute(
                        "SELECT id, name FROM tags WHERE user_id = ? AND lower(name) = lower(?)",
                        (user_id, tn),
                    ).fetchone()
                    if et:
                        tid = et["id"]
                    else:
                        tid = db.execute(
                            "INSERT INTO tags (user_id, name, color) VALUES (?, ?, ?) RETURNING id",
                            (user_id, tn, color),
                        ).fetchone()["id"]
                    db.execute(
                        "INSERT INTO plant_tags (plant_id, tag_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                        (plant_id, tid),
                    )
            _add_method = form_values.get("active_mode", "name") or "name"
            _log_activity(db, user_id, f"plant_added_{_add_method}", form_values["name"])
            db.commit()
            flash("Plant idea added.")
            return redirect(url_for("idea_detail", plant_id=plant_id))

        return render_template("idea_new.html", form_values=form_values, plant_names=plant_names, library_plants=library_plants)

    @app.route("/ideas/<int:plant_id>")
    @login_required
    def idea_detail(plant_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        plant = db.execute(f"SELECT * FROM plants WHERE id = ? AND user_id IN {ph}", (plant_id, *id_args)).fetchone()
        if plant is None:
            flash("Plant was not found.")
            return redirect(url_for("ideas_index"))
        is_owner = plant["user_id"] in ids

        zone_id = request.args.get("zone_id", type=int)
        yard_plant_id = request.args.get("yard_plant_id", type=int)
        zone = None
        if zone_id:
            zone = db.execute(
                f"SELECT id, name FROM yard_zones WHERE id = ? AND user_id IN {ph}",
                (zone_id, *id_args),
            ).fetchone()

        categories = db.execute(
            """
            SELECT c.*
            FROM categories c
            JOIN plant_categories pc ON c.id = pc.category_id
            WHERE pc.plant_id = ?
            ORDER BY c.name ASC
            """,
            (plant_id,),
        ).fetchall()
        plant_tags = [
            {"id": t["id"], "name": t["name"], "color": t["color"]}
            for t in db.execute(
                """
                SELECT t.id, t.name, t.color
                FROM tags t JOIN plant_tags pt ON pt.tag_id = t.id
                WHERE pt.plant_id = ?
                ORDER BY t.name ASC
                """,
                (plant_id,),
            ).fetchall()
        ]
        user_tags = [
            {"id": t["id"], "name": t["name"], "color": t["color"]}
            for t in db.execute(
                "SELECT id, name, color FROM tags WHERE user_id = ? ORDER BY name ASC",
                (g.user["id"],),
            ).fetchall()
        ]
        all_zones = db.execute(
            f"SELECT id, name FROM yard_zones WHERE user_id IN {ph} ORDER BY name ASC",
            id_args,
        ).fetchall()
        shared_names = _shared_user_names(db, g.user["id"])
        pz_rows = db.execute(
            f"""
            SELECT yp.id, z.id AS zone_id, z.name AS zone_name
            FROM yard_plants yp
            JOIN yard_zones z ON z.id = yp.zone_id
            WHERE yp.user_id IN {ph} AND lower(yp.plant_name) = lower(?)
            ORDER BY z.name ASC
            """,
            (*id_args, plant["name"]),
        ).fetchall()
        plant_zones = [{"id": r["id"], "zone_id": r["zone_id"], "zone_name": r["zone_name"], "yard_notes": []} for r in pz_rows]
        if plant_zones:
            ph2, pz_id_args = _in_ids([pz["id"] for pz in plant_zones])
            note_rows = db.execute(
                f"SELECT * FROM yard_plant_notes WHERE yard_plant_id IN {ph2} ORDER BY note_date DESC NULLS LAST, created_at DESC",
                pz_id_args,
            ).fetchall()
            notes_by_pz: dict = {}
            for n in note_rows:
                notes_by_pz.setdefault(n["yard_plant_id"], []).append(n)
            for pz in plant_zones:
                pz["yard_notes"] = notes_by_pz.get(pz["id"], [])
        today = _local_today()

        ff_fert = _feature_fertilization(g.user)
        last_fertilized = None
        next_fertilization = None
        fert_deadline = _local_date_plus(3)
        if ff_fert:
            last_fert_date = plant["last_fertilized_date"] if plant.get("last_fertilized_date") else None
            last_fertilized = {"date": last_fert_date, "type": plant.get("last_fertilizer_type")}
            never = bool(plant.get("never_fertilize"))
            if not never:
                gen_at = plant.get("next_fertilization_generated_at") or None
                needs_regen = (
                    not gen_at
                    or (last_fert_date and last_fert_date > gen_at[:10])
                    or (plant.get("next_fertilization_date") and plant["next_fertilization_date"] < today)
                )
                if needs_regen:
                    fert_allowed, _ = _check_api_rate(db, g.user["id"], "fertilization")
                    if fert_allowed:
                        user_location = g.user.get("location", "")
                        _suggest_next_fertilization_ornamental(db, plant, user_location, last_fert_date)
                        plant = db.execute(
                            f"SELECT * FROM plants WHERE id = ? AND user_id IN {ph}",
                            (plant_id, *id_args),
                        ).fetchone()
                next_fertilization = {
                    "date": plant.get("next_fertilization_date"),
                    "note": plant.get("next_fertilization_note"),
                    "planned_date": plant.get("planned_fertilization_date"),
                    "never": False,
                }
            else:
                next_fertilization = {"date": None, "note": None, "planned_date": None, "never": True}

        ff_water = _feature_watering(g.user)
        last_watered = None
        next_watering = None
        water_deadline = _local_date_plus(1)
        if ff_water and plant_zones:
            last_watered_date = plant.get("last_watered_date") or None
            last_watered = {"date": last_watered_date}
            if plant.get("never_water"):
                next_watering = {"date": None, "note": None, "frequency_days": None, "never": True}
            else:
                needs_regen = not plant.get("watering_generated_at")
                if needs_regen:
                    water_allowed, _ = _check_api_rate(db, g.user["id"], "watering")
                    if water_allowed:
                        _suggest_watering_frequency_ornamental(db, plant, g.user.get("location", ""), last_watered_date)
                        plant = db.execute(f"SELECT * FROM plants WHERE id = ? AND user_id IN {ph}", (plant_id, *id_args)).fetchone()
                next_watering = {
                    "date": plant.get("next_watering_date"),
                    "note": plant.get("watering_note"),
                    "frequency_days": plant.get("watering_frequency_days"),
                    "never": False,
                }

        return render_template("idea_detail.html", plant=plant, categories=categories,
                               zone=zone, yard_plant_id=yard_plant_id,
                               plant_tags=plant_tags, user_tags=user_tags,
                               all_zones=all_zones, is_owner=is_owner,
                               shared_names=shared_names, today=today,
                               plant_zones=plant_zones,
                               ff_fert=ff_fert, last_fertilized=last_fertilized,
                               next_fertilization=next_fertilization,
                               fert_deadline=fert_deadline,
                               ff_water=ff_water, last_watered=last_watered,
                               next_watering=next_watering, water_deadline=water_deadline)

    @app.route("/ideas/<int:plant_id>/add-to-zone", methods=["POST"])
    @login_required
    def idea_add_to_zone(plant_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        plant = db.execute(
            f"SELECT * FROM plants WHERE id = ? AND user_id IN {ph}",
            (plant_id, *id_args),
        ).fetchone()
        if plant is None:
            flash("Plant not found.")
            return redirect(url_for("ideas_index"))
        zone_id = request.form.get("zone_id", "").strip()
        if not zone_id:
            flash("Please choose a zone.")
            return redirect(url_for("idea_detail", plant_id=plant_id))
        zone_ids = _shared_user_ids(db, g.user["id"])
        zph, z_id_args = _in_ids(zone_ids)
        zone = db.execute(
            f"SELECT id FROM yard_zones WHERE id = ? AND user_id IN {zph}",
            (zone_id, *z_id_args),
        ).fetchone()
        if zone is None:
            flash("Zone not found.")
            return redirect(url_for("idea_detail", plant_id=plant_id))
        db.execute(
            """
            INSERT INTO yard_plants
            (user_id, zone_id, plant_name, scientific_name, lookup_query, image_path, location_x, location_y,
             size_info, watering_needs, sun_needs, flowering_schedule, lifecycle, spreads, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                g.user["id"],
                int(zone_id),
                plant["name"],
                plant["scientific_name"] or "",
                plant["lookup_query"] or plant["name"],
                plant["image_path"],
                50,
                50,
                plant["size_info"] or "",
                "",
                plant["sun_exposure"] or "",
                plant["flowering_schedule"] or "",
                plant["lifecycle"] or "",
                "",
                "",
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        _log_activity(db, g.user["id"], "yard_plant_added", plant["name"])
        db.commit()
        flash("Added to zone.")
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/ideas/<int:plant_id>/plan-fertilize", methods=["POST"])
    @login_required
    def idea_plan_fertilize(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id, user_id, last_fertilized_date FROM plants WHERE id = ? AND user_id = ?",
            (plant_id, g.user["id"]),
        ).fetchone()
        if plant is None:
            flash("Plant not found.")
            return redirect(url_for("ideas_index"))
        planned_date = request.form.get("planned_date", "").strip()
        never_fertilize = 1 if request.form.get("never_fertilize") else 0
        last_fertilized_date = request.form.get("last_fertilized_date", "").strip()
        last_fertilizer_type = request.form.get("last_fertilizer_type", "").strip() or None
        if planned_date and not _ISO_DATE_RE.match(planned_date):
            planned_date = ""
        if last_fertilized_date and not _ISO_DATE_RE.match(last_fertilized_date):
            last_fertilized_date = ""
        invalidate = bool(last_fertilized_date) and last_fertilized_date != (plant["last_fertilized_date"] or "")
        db.execute(
            "UPDATE plants SET planned_fertilization_date = ?, never_fertilize = ?,"
            " last_fertilized_date = ?, last_fertilizer_type = COALESCE(?, last_fertilizer_type)"
            + (", next_fertilization_generated_at = NULL" if invalidate else "")
            + " WHERE id = ?",
            (planned_date or None, never_fertilize, last_fertilized_date or None, last_fertilizer_type, plant_id),
        )
        db.commit()
        if request.form.get("from_dashboard"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/ideas/<int:plant_id>/never-fertilize", methods=["POST"])
    @login_required
    def idea_set_never_fertilize(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id FROM plants WHERE id = ? AND user_id = ?",
            (plant_id, g.user["id"]),
        ).fetchone()
        if plant is None:
            return jsonify({"error": "Not found"}), 404
        never = 1 if request.form.get("never_fertilize") else 0
        if never:
            db.execute(
                "UPDATE plants SET never_fertilize = 1, planned_fertilization_date = NULL,"
                " next_fertilization_date = NULL, next_fertilization_generated_at = NULL WHERE id = ?",
                (plant_id,),
            )
        else:
            db.execute("UPDATE plants SET never_fertilize = 0 WHERE id = ?", (plant_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.route("/ideas/<int:plant_id>/plan-watering", methods=["POST"])
    @login_required
    def idea_plan_watering(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id, user_id, watering_frequency_days FROM plants WHERE id = ? AND user_id = ?",
            (plant_id, g.user["id"]),
        ).fetchone()
        if plant is None:
            flash("Plant not found.")
            return redirect(url_for("ideas_index"))
        if "has_tracking_update" in request.form:
            never_water = 1 if request.form.get("never_water") else 0
            if never_water:
                db.execute(
                    "UPDATE plants SET never_water = 1, next_watering_date = NULL, watering_generated_at = NULL WHERE id = ?",
                    (plant_id,),
                )
                db.commit()
                return redirect(url_for("idea_detail", plant_id=plant_id))
            else:
                db.execute("UPDATE plants SET never_water = 0 WHERE id = ?", (plant_id,))
        raw_date = request.form.get("last_watered_date", "").strip()
        if not raw_date:
            raw_date = _local_today()
        if not _ISO_DATE_RE.match(raw_date):
            raw_date = _local_today()
        freq = plant["watering_frequency_days"]
        if freq:
            next_date = (date.fromisoformat(raw_date) + timedelta(days=freq)).isoformat()
        else:
            next_date = None
        db.execute(
            "UPDATE plants SET last_watered_date = ?, next_watering_date = ? WHERE id = ?",
            (raw_date, next_date, plant_id),
        )
        db.commit()
        if request.form.get("from_dashboard"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/ideas/<int:plant_id>/never-water", methods=["POST"])
    @login_required
    def idea_set_never_water(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id FROM plants WHERE id = ? AND user_id = ?",
            (plant_id, g.user["id"]),
        ).fetchone()
        if plant is None:
            return jsonify({"error": "Not found"}), 404
        never = 1 if request.form.get("never_water") else 0
        if never:
            db.execute(
                "UPDATE plants SET never_water = 1, next_watering_date = NULL, watering_generated_at = NULL WHERE id = ?",
                (plant_id,),
            )
        else:
            db.execute("UPDATE plants SET never_water = 0 WHERE id = ?", (plant_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.route("/ideas/<int:plant_id>/watered-today", methods=["POST"])
    @login_required
    def idea_watered_today(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id, watering_frequency_days FROM plants WHERE id = ? AND user_id = ?",
            (plant_id, g.user["id"]),
        ).fetchone()
        if plant is None:
            return jsonify({"error": "Not found"}), 404
        today = _local_today()
        freq = plant["watering_frequency_days"]
        next_date = (date.fromisoformat(today) + timedelta(days=freq)).isoformat() if freq else None
        db.execute(
            "UPDATE plants SET last_watered_date = ?, next_watering_date = ?, never_water = 0 WHERE id = ?",
            (today, next_date, plant_id),
        )
        db.commit()
        return jsonify({"ok": True, "today": today, "next_date": next_date})

    @app.route("/ideas/<int:plant_id>/fertilized-today", methods=["POST"])
    @login_required
    def idea_fertilized_today(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id, name, scientific_name, plant_form, lifecycle, flowering_schedule,"
            " sun_exposure, water_needs FROM plants WHERE id = ? AND user_id = ?",
            (plant_id, g.user["id"]),
        ).fetchone()
        if plant is None:
            return jsonify({"error": "Not found"}), 404
        today = _local_today()
        fertilizer_type = (request.form.get("fertilizer_type") or "").strip() or None
        db.execute(
            "UPDATE plants SET last_fertilized_date = ?, last_fertilizer_type = COALESCE(?, last_fertilizer_type),"
            " next_fertilization_generated_at = NULL WHERE id = ?",
            (today, fertilizer_type, plant_id),
        )
        db.commit()
        user_location = g.user.get("location") if g.user else None
        result = _suggest_next_fertilization_ornamental(db, plant, user_location, today)
        next_date = result["date"] if result else None
        next_note = result["note"] if result else None
        return jsonify({"ok": True, "today": today, "next_date": next_date, "next_note": next_note})

    @app.route("/yard")
    @login_required
    def yard_index():
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        zones = db.execute(
            f"""
            SELECT z.*,
                   COUNT(DISTINCT yp.id) + COUNT(DISTINCT ge.id) AS plant_count
            FROM yard_zones z
            LEFT JOIN yard_plants yp ON yp.zone_id = z.id
            LEFT JOIN garden_entries ge ON ge.zone_id = z.id
            WHERE z.user_id IN {ph}
            GROUP BY z.id
            ORDER BY z.created_at DESC
            """,
            id_args,
        ).fetchall()
        # For zones with no photo, gather up to 4 edible plant names for a thumbnail collage
        zone_edible_plants = {}
        if _feature_garden_zones(g.user):
            no_photo_ids = [z["id"] for z in zones if not z["reference_image_path"]]
            if no_photo_ids:
                ph2 = ",".join("?" * len(no_photo_ids))
                for row in db.execute(
                    f"SELECT zone_id, plant_name FROM garden_entries"
                    f" WHERE zone_id IN ({ph2}) ORDER BY id ASC",
                    no_photo_ids,
                ).fetchall():
                    bucket = zone_edible_plants.setdefault(row["zone_id"], [])
                    if len(bucket) < 4:
                        bucket.append(row["plant_name"])
        return render_template("yard_index.html", zones=zones, zone_edible_plants=zone_edible_plants)

    @app.route("/yard/zones/new", methods=["GET", "POST"])
    @login_required
    def yard_zone_new():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name:
                flash("Zone name is required.")
                return render_template("yard_zone_new.html")
            ref_image = save_upload(request.files.get("reference_photo"), app.config["UPLOAD_FOLDER"], g.user["id"], "zone")
            db = get_db()
            db.execute(
                """
                INSERT INTO yard_zones (user_id, name, description, reference_image_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (g.user["id"], name, description, ref_image, datetime.utcnow().isoformat(timespec="seconds")),
            )
            _log_activity(db, g.user["id"], "zone_added", name)
            db.commit()
            flash("Yard zone created.")
            return redirect(url_for("yard_index"))
        return render_template("yard_zone_new.html")

    @app.route("/yard/zones/<int:zone_id>/photo", methods=["POST"])
    @login_required
    def yard_zone_update_photo(zone_id: int):
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        zone = db.execute(f"SELECT id FROM yard_zones WHERE id = ? AND user_id IN {ph}", [zone_id] + id_args).fetchone()
        if zone is None:
            if is_ajax:
                return jsonify(error="Yard zone not found."), 404
            flash("Yard zone not found.")
            return redirect(url_for("yard_index"))
        image_path = save_upload(request.files.get("reference_photo"), app.config["UPLOAD_FOLDER"], g.user["id"], "zone")
        if not image_path:
            if is_ajax:
                return jsonify(error="Please select a photo."), 400
            flash("Please select a photo.")
            return redirect(url_for("yard_zone_detail", zone_id=zone_id))
        db.execute(f"UPDATE yard_zones SET reference_image_path = ? WHERE id = ? AND user_id IN {ph}", [image_path, zone_id] + id_args)
        db.commit()
        if is_ajax:
            return jsonify(ok=True, url=image_path)
        flash("Zone photo updated.")
        return redirect(url_for("yard_zone_detail", zone_id=zone_id))

    @app.route("/yard/zones/<int:zone_id>/edit", methods=["GET", "POST"])
    @login_required
    def yard_zone_edit(zone_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        zone = db.execute(f"SELECT * FROM yard_zones WHERE id = ? AND user_id IN {ph}", [zone_id] + id_args).fetchone()
        if zone is None:
            flash("Yard zone not found.")
            return redirect(url_for("yard_index"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name:
                flash("Zone name is required.")
            else:
                db.execute(
                    f"UPDATE yard_zones SET name = ?, description = ? WHERE id = ? AND user_id IN {ph}",
                    [name, description, zone_id] + id_args,
                )
                db.commit()
                flash("Zone updated.")
                return redirect(url_for("yard_zone_detail", zone_id=zone_id))
        return render_template("yard_zone_edit.html", zone=zone)

    @app.route("/yard/zones/<int:zone_id>")
    @login_required
    def yard_zone_detail(zone_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        zone = db.execute(f"SELECT * FROM yard_zones WHERE id = ? AND user_id IN {ph}", [zone_id] + id_args).fetchone()
        if zone is None:
            flash("Yard zone not found.")
            return redirect(url_for("yard_index"))
        plants = db.execute(
            f"""SELECT yp.*,
                      p.id                          AS lib_plant_id,
                      p.photo_urls                  AS lib_photo_urls,
                      p.image_path                  AS lib_image_path,
                      p.image_url                   AS lib_image_url,
                      p.next_watering_date          AS next_watering_date,
                      p.never_water                 AS never_water,
                      p.next_fertilization_date     AS next_fertilization_date,
                      p.planned_fertilization_date  AS planned_fertilization_date,
                      p.never_fertilize             AS never_fertilize
               FROM yard_plants yp
               LEFT JOIN plants p ON p.name = yp.plant_name AND p.user_id = yp.user_id
               WHERE yp.zone_id = ? AND yp.user_id IN {ph}
               ORDER BY yp.created_at DESC""",
            [zone_id] + id_args,
        ).fetchall()
        lib_plant_ids = [p["lib_plant_id"] for p in plants if p["lib_plant_id"]]
        tags_map = {}
        user_ph = ph  # preserve before possible overwrite below
        if lib_plant_ids:
            ph = ",".join("?" * len(lib_plant_ids))
            for row in db.execute(
                f"SELECT pt.plant_id, t.id, t.name, t.color FROM plant_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.plant_id IN ({ph}) ORDER BY t.name ASC",
                lib_plant_ids,
            ).fetchall():
                tags_map.setdefault(row["plant_id"], []).append({"id": row["id"], "name": row["name"], "color": row["color"]})
        shared_names = _shared_user_names(db, g.user["id"])
        feature_gz = _feature_garden_zones(g.user)
        garden_entries = []
        if feature_gz:
            garden_entries = db.execute(
                f"SELECT id, plant_name, variety, location_name, planted_date,"
                f"       next_watering_date, never_water,"
                f"       next_fertilization_date, planned_fertilization_date, never_fertilize"
                f" FROM garden_entries"
                f" WHERE zone_id = ? AND user_id IN {user_ph} ORDER BY plant_name ASC",
                [zone_id] + id_args,
            ).fetchall()
        ff_water = _feature_watering(g.user)
        ff_fert = _feature_fertilization(g.user)
        today = _local_today()
        water_deadline = _local_date_plus(1)
        fert_deadline = _local_date_plus(3)
        return render_template("yard_zone_detail.html", zone=zone, plants=plants, tags_map=tags_map,
                               shared_names=shared_names, feature_garden_zones=feature_gz,
                               garden_entries=garden_entries, ff_water=ff_water, ff_fert=ff_fert,
                               today=today, water_deadline=water_deadline, fert_deadline=fert_deadline)

    @app.route("/yard/zones/<int:zone_id>/add-edible")
    @login_required
    def yard_zone_add_edible(zone_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        zone = db.execute(f"SELECT * FROM yard_zones WHERE id = ? AND user_id IN {ph}", [zone_id] + id_args).fetchone()
        if zone is None:
            flash("Yard zone not found.")
            return redirect(url_for("yard_index"))
        all_entries = db.execute(
            f"SELECT id, plant_name, variety, location_name,"
            f" CASE WHEN zone_id = ? THEN 1 ELSE 0 END AS in_this_zone"
            f" FROM garden_entries WHERE user_id IN {ph} ORDER BY in_this_zone ASC, plant_name ASC",
            [zone_id] + id_args,
        ).fetchall()
        return render_template("yard_zone_add_edible.html", zone=zone, all_entries=all_entries)

    @app.route("/yard/plants/new", methods=["GET", "POST"])
    @login_required
    def yard_plant_new():
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        zones = db.execute(f"SELECT * FROM yard_zones WHERE user_id IN {ph} ORDER BY name ASC", id_args).fetchall()
        plant_names = sorted(set(
            [r["name"] for r in db.execute(f"SELECT DISTINCT name FROM plants WHERE user_id IN {ph} ORDER BY name ASC", id_args).fetchall()]
            + [r["plant_name"] for r in db.execute(f"SELECT DISTINCT plant_name FROM yard_plants WHERE user_id IN {ph} ORDER BY plant_name ASC", id_args).fetchall()]
        ))
        library_plants = db.execute(
            f"SELECT id, name, scientific_name, image_path, image_url, photo_urls, sun_exposure, lifecycle, size_info, flowering_schedule, description FROM plants WHERE user_id IN {ph} ORDER BY name ASC",
            id_args,
        ).fetchall()
        library_plants_json = [{"name": r["name"], "sci": r["scientific_name"] or "", "description": r["description"] or ""} for r in library_plants]

        prefill_name = request.args.get("plant_name", "").strip()
        form_values = {
            "zone_id": request.args.get("zone_id", ""),
            "plant_name": prefill_name,
            "scientific_name": "",
            "lookup_query": "",
            "watering_needs": "",
            "sun_needs": "",
            "flowering_schedule": "",
            "lifecycle": "",
            "size_info": "",
            "spreads": "",
            "description": "",
            "source_note": "",
            "notes": "",
            "image_url": "",
            "active_mode": "library",
            "yard_input_mode": "name",
        }

        if request.method == "POST":
            form_action = request.form.get("form_action", "save")
            form_values = {
                "zone_id": request.form.get("zone_id", "").strip(),
                "plant_name": request.form.get("plant_name", "").strip(),
                "scientific_name": request.form.get("scientific_name", "").strip(),
                "lookup_query": request.form.get("lookup_query", "").strip(),
                "watering_needs": request.form.get("watering_needs", "").strip(),
                "sun_needs": request.form.get("sun_needs", "").strip(),
                "flowering_schedule": request.form.get("flowering_schedule", "").strip(),
                "lifecycle": request.form.get("lifecycle", "").strip(),
                "size_info": request.form.get("size_info", "").strip(),
                "spreads": request.form.get("spreads", "").strip(),
                "description": request.form.get("description", "").strip(),
                "source_note": request.form.get("source_note", "").strip(),
                "notes": request.form.get("notes", "").strip(),
                "image_url": request.form.get("image_url", "").strip(),
                "photo_urls_json": request.form.get("photo_urls_json", "").strip(),
                "active_mode": request.form.get("active_mode", "library"),
                "yard_input_mode": request.form.get("yard_input_mode", "name"),
                "photo_id_suggestions": request.form.get("photo_id_suggestions", "").strip(),
            }

            if form_action == "autofill_name":
                details, error = lookup_plant_details(form_values["lookup_query"] or form_values["plant_name"], location=g.user.get("location"))
                if error:
                    flash(error)
                else:
                    apply_lookup_to_yard_form(form_values, details)
                    flash("Planted item details autofilled from name lookup.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants, library_plants_json=library_plants_json)

            if form_action == "autofill_photo":
                suggestions, error = identify_plant_from_image(request.files.get("photo"), provider=g.user.get("photo_id_provider") or "claude", location=g.user.get("location"))
                if error:
                    flash(error)
                elif suggestions:
                    top = suggestions[0]
                    if top.get("common_name") and not form_values["plant_name"]:
                        form_values["plant_name"] = top["common_name"]
                    if top.get("scientific_name"):
                        form_values["scientific_name"] = top["scientific_name"]
                        form_values["lookup_query"] = top["scientific_name"]
                    details, lookup_error = lookup_plant_details(form_values["lookup_query"] or form_values["plant_name"], location=g.user.get("location"))
                    if not lookup_error:
                        apply_lookup_to_yard_form(form_values, details)
                    form_values["photo_id_suggestions"] = json.dumps(suggestions)
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants, library_plants_json=library_plants_json)

            if form_action == "autofill_label":
                text, error = extract_text_from_image(request.files.get("label_photo"))
                if error:
                    flash(error)
                else:
                    query = infer_query_from_text(text)
                    if query:
                        if not form_values.get("plant_name"):
                            form_values["plant_name"] = query
                        form_values["yard_input_mode"] = "name"
                        flash(f"Label read: \"{query}\" — review the name then save.")
                    else:
                        flash("Could not extract a plant name from that label.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants, library_plants_json=library_plants_json)

            if not form_values["zone_id"] or not form_values["plant_name"]:
                flash("Zone and plant name are required.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants, library_plants_json=library_plants_json)

            zone = db.execute(
                f"SELECT id FROM yard_zones WHERE id = ? AND user_id IN {ph}",
                [form_values["zone_id"]] + id_args,
            ).fetchone()
            if zone is None:
                flash("Please choose a valid zone.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants, library_plants_json=library_plants_json)

            image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"], g.user["id"], "yardplant")
            db.execute(
                """
                INSERT INTO yard_plants
                (user_id, zone_id, plant_name, scientific_name, lookup_query, image_path, location_x, location_y,
                 size_info, watering_needs, sun_needs, flowering_schedule, lifecycle, spreads, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    g.user["id"],
                    form_values["zone_id"],
                    form_values["plant_name"],
                    form_values["scientific_name"],
                    form_values["lookup_query"],
                    image_path,
                    50,
                    50,
                    form_values["size_info"],
                    form_values["watering_needs"],
                    form_values["sun_needs"],
                    form_values["flowering_schedule"],
                    form_values["lifecycle"],
                    form_values["spreads"],
                    form_values["notes"],
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            # Add to library if not already there; enrich existing record if it is
            existing = db.execute(
                "SELECT id FROM plants WHERE user_id = ? AND name = ?",
                (g.user["id"], form_values["plant_name"]),
            ).fetchone()
            if existing is None:
                library_plant_id = db.execute(
                    """
                    INSERT INTO plants
                    (user_id, name, scientific_name, lookup_query, source_type, source_note, image_path,
                     label_photo_path, image_url, size_info, flowering_schedule, sun_exposure, lifecycle,
                     lookup_status, notes, pnw_native, photo_urls, evergreen_status, plant_form, height_category,
                     description, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (
                        g.user["id"],
                        form_values["plant_name"],
                        form_values["scientific_name"],
                        form_values["lookup_query"],
                        "yard",
                        form_values.get("source_note") or None,
                        image_path,
                        None,
                        form_values.get("image_url") or None,
                        form_values["size_info"],
                        form_values["flowering_schedule"],
                        normalize_sun_value(form_values["sun_needs"] or ""),
                        form_values["lifecycle"],
                        None,
                        form_values["notes"],
                        None,
                        form_values.get("photo_urls_json") or None,
                        None,
                        None,
                        None,
                        form_values.get("description") or None,
                        datetime.utcnow().isoformat(timespec="seconds"),
                    ),
                ).fetchone()["id"]
            else:
                library_plant_id = existing["id"]
                updates = {k: v for k, v in [
                    ("scientific_name",    form_values["scientific_name"] or None),
                    ("lookup_query",       form_values["lookup_query"] or None),
                    ("image_url",          form_values.get("image_url") or None),
                    ("photo_urls",         form_values.get("photo_urls_json") or None),
                    ("sun_exposure",       normalize_sun_value(form_values.get("sun_needs") or "") or None),
                    ("lifecycle",          form_values["lifecycle"] or None),
                    ("size_info",          form_values["size_info"] or None),
                    ("flowering_schedule", form_values["flowering_schedule"] or None),
                    ("description",        form_values.get("description") or None),
                    ("source_note",        form_values.get("source_note") or None),
                ] if v}
                if updates:
                    set_clause = ", ".join(
                        f"{col} = COALESCE(NULLIF({col}, ''), ?)" for col in updates
                    )
                    db.execute(
                        f"UPDATE plants SET {set_clause} WHERE id = ?",
                        [*updates.values(), existing["id"]],
                    )
            # Associate tags with the library plant record
            tag_names_raw = request.form.get("tag_names", "").strip()
            if tag_names_raw:
                for tn in [t.strip() for t in tag_names_raw.split(",") if t.strip()]:
                    tn = tn[:50]
                    color = tag_color_for(tn)
                    et = db.execute(
                        "SELECT id FROM tags WHERE user_id = ? AND lower(name) = lower(?)",
                        (g.user["id"], tn),
                    ).fetchone()
                    if et:
                        tid = et["id"]
                    else:
                        tid = db.execute(
                            "INSERT INTO tags (user_id, name, color) VALUES (?, ?, ?) RETURNING id",
                            (g.user["id"], tn, color),
                        ).fetchone()["id"]
                    db.execute(
                        "INSERT INTO plant_tags (plant_id, tag_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                        (library_plant_id, tid),
                    )
            _yard_method = "library" if form_values.get("active_mode") == "library" else (form_values.get("yard_input_mode") or "name")
            _log_activity(db, g.user["id"], f"yard_plant_added_{_yard_method}", form_values["plant_name"])
            db.commit()
            flash("Planted item saved.")
            return redirect(url_for("yard_zone_detail", zone_id=form_values["zone_id"]))
        return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants, library_plants_json=library_plants_json)

    @app.route("/yard/plants/<int:yard_plant_id>/remove", methods=["POST"])
    @login_required
    def yard_plant_remove(yard_plant_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        row = db.execute(
            f"SELECT zone_id FROM yard_plants WHERE id = ? AND user_id IN {ph}",
            [yard_plant_id] + id_args,
        ).fetchone()
        if row:
            db.execute(
                f"DELETE FROM yard_plants WHERE id = ? AND user_id IN {ph}",
                [yard_plant_id] + id_args,
            )
            db.commit()
            flash("Plant removed from zone.")
            back = request.args.get("back", type=int)
            if back:
                return redirect(url_for("idea_detail", plant_id=back))
            return redirect(url_for("yard_zone_detail", zone_id=row["zone_id"]))
        flash("Plant not found.")
        return redirect(url_for("yard_index"))

    @app.route("/yard/plants/<int:yard_plant_id>/update-notes", methods=["POST"])
    @login_required
    def yard_plant_update_notes(yard_plant_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        row = db.execute(
            f"SELECT zone_id FROM yard_plants WHERE id = ? AND user_id IN {ph}",
            [yard_plant_id] + id_args,
        ).fetchone()
        if row is None:
            flash("Plant entry not found.")
            return redirect(url_for("yard_index"))
        notes = request.form.get("notes", "").strip() or None
        db.execute(
            f"UPDATE yard_plants SET notes = ? WHERE id = ? AND user_id IN {ph}",
            [notes, yard_plant_id] + id_args,
        )
        db.commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": True})
        back = request.args.get("back", type=int)
        if back:
            return redirect(url_for("idea_detail", plant_id=back))
        return redirect(url_for("yard_zone_detail", zone_id=row["zone_id"]))

    @app.route("/yard/plants/<int:yard_plant_id>/notes", methods=["POST"])
    @login_required
    def yard_plant_add_note(yard_plant_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        row = db.execute(
            f"SELECT id FROM yard_plants WHERE id = ? AND user_id IN {ph}",
            (yard_plant_id, *id_args),
        ).fetchone()
        if row is None:
            flash("Plant not found.")
            return redirect(url_for("yard_index"))
        notes = request.form.get("notes", "").strip()
        note_date = request.form.get("note_date", "").strip() or _local_today()
        if notes:
            db.execute(
                "INSERT INTO yard_plant_notes (yard_plant_id, user_id, note_date, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                (yard_plant_id, g.user["id"], note_date, notes, datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.commit()
        back = request.form.get("plant_id", type=int)
        if back:
            return redirect(url_for("idea_detail", plant_id=back))
        return redirect(url_for("yard_index"))

    @app.route("/yard/plant-notes/<int:note_id>/delete", methods=["POST"])
    @login_required
    def yard_plant_note_delete(note_id: int):
        db = get_db()
        row = db.execute(
            "SELECT id FROM yard_plant_notes WHERE id = ? AND user_id = ?",
            (note_id, g.user["id"]),
        ).fetchone()
        if row is None:
            flash("Note not found.")
            return redirect(url_for("yard_index"))
        db.execute("DELETE FROM yard_plant_notes WHERE id = ?", (note_id,))
        db.commit()
        back = request.form.get("plant_id", type=int)
        if back:
            return redirect(url_for("idea_detail", plant_id=back))
        return redirect(url_for("yard_index"))

    # ── Tools (settings + exports) ───────────────────────────────────────────

    @app.route("/settings")
    def settings_redirect():
        return redirect(url_for("tools"))

    def _log_activity(db, user_id, action, item_name):
        db.execute(
            "INSERT INTO activity_log (user_id, action, item_name, logged_at) VALUES (?, ?, ?, ?)",
            (user_id, action, item_name, datetime.utcnow().isoformat(timespec="seconds")),
        )

    def _shared_user_ids(db, user_id):
        rows = db.execute(
            "SELECT CASE WHEN user_a_id = ? THEN user_b_id ELSE user_a_id END AS pid "
            "FROM garden_shares WHERE (user_a_id = ? OR user_b_id = ?) AND confirmed = 1",
            (user_id, user_id, user_id),
        ).fetchall()
        return [user_id] + [r["pid"] for r in rows]

    def _in_ids(ids):
        return "({})".format(",".join("?" * len(ids))), ids

    def _shared_user_names(db, user_id):
        """Return {partner_user_id: username} for all confirmed garden-share partners."""
        rows = db.execute(
            "SELECT CASE WHEN gs.user_a_id = ? THEN gs.user_b_id ELSE gs.user_a_id END AS pid, u.username "
            "FROM garden_shares gs "
            "JOIN users u ON u.id = CASE WHEN gs.user_a_id = ? THEN gs.user_b_id ELSE gs.user_a_id END "
            "WHERE (gs.user_a_id = ? OR gs.user_b_id = ?) AND gs.confirmed = 1",
            (user_id, user_id, user_id, user_id),
        ).fetchall()
        return {r["pid"]: r["username"] for r in rows}

    def _user_stats(db, user_id, created_at_str):
        plants  = db.execute("SELECT COUNT(*) AS n FROM plants WHERE user_id = ?",        (user_id,)).fetchone()["n"]
        zones   = db.execute("SELECT COUNT(*) AS n FROM yard_zones WHERE user_id = ?",    (user_id,)).fetchone()["n"]
        garden  = db.execute("SELECT COUNT(*) AS n FROM garden_entries WHERE user_id = ?",(user_id,)).fetchone()["n"]
        log_row = db.execute(
            "SELECT COUNT(*) AS n, MAX(logged_in_at) AS last FROM login_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        login_count = log_row["n"] or 0
        last_login  = log_row["last"]
        try:
            created = datetime.fromisoformat(created_at_str)
            weeks   = max(1.0, (datetime.utcnow() - created).days / 7.0)
        except Exception:
            weeks = 1.0
        avg_per_week = round(login_count / weeks, 1)
        week_since = (datetime.utcnow() - timedelta(days=7)).isoformat(timespec="seconds")
        activity_rows = db.execute(
            "SELECT action, item_name, logged_at FROM activity_log"
            " WHERE user_id = ? AND logged_at >= ? ORDER BY logged_at ASC",
            (user_id, week_since),
        ).fetchall()
        login_rows = db.execute(
            "SELECT logged_in_at FROM login_log"
            " WHERE user_id = ? AND logged_in_at >= ? ORDER BY logged_in_at ASC",
            (user_id, week_since),
        ).fetchall()

        _METHOD_LABELS = {
            "name": "by name", "photo": "from photo", "label": "from label",
            "url": "by URL", "library": "from library",
        }
        day_data: dict = {}

        def _day(day_str):
            return day_data.setdefault(day_str, {
                "logins": 0, "plant_entries": [], "yard_entries": [],
                "zones": [], "garden_entries": [], "chat_queries": [],
                "garden_notes": [], "tags": [], "suggestion_adds": [],
                "_sp": set(), "_sy": set(),
            })

        for row in login_rows:
            _day(row["logged_in_at"][:10])["logins"] += 1

        for row in activity_rows:
            d = _day(row["logged_at"][:10])
            act = row["action"]
            name = (row["item_name"] or "").strip()
            if not name:
                continue
            if act.startswith("plant_added"):
                if name not in d["_sp"]:
                    d["_sp"].add(name)
                    suffix = act[len("plant_added"):].lstrip("_")
                    d["plant_entries"].append({"name": name, "method_label": _METHOD_LABELS.get(suffix, "")})
            elif act.startswith("yard_plant_added"):
                if name not in d["_sy"]:
                    d["_sy"].add(name)
                    suffix = act[len("yard_plant_added"):].lstrip("_")
                    d["yard_entries"].append({"name": name, "method_label": _METHOD_LABELS.get(suffix, "")})
            elif act == "zone_added" and name not in d["zones"]:
                d["zones"].append(name)
            elif act == "garden_entry_added" and name not in d["garden_entries"]:
                d["garden_entries"].append(name)
            elif act == "garden_chat":
                d["chat_queries"].append(name)
            elif act == "tag_applied":
                d["tags"].append(name)
            elif act == "suggestion_added" and name not in d.get("suggestion_adds", []):
                d.setdefault("suggestion_adds", []).append(name)

        note_rows = db.execute(
            "SELECT gp.created_at, gp.image_path, gp.is_fertilization, gp.fertilizer_type,"
            " gp.fertilization_date, gp.notes, ge.plant_name"
            " FROM garden_photos gp JOIN garden_entries ge ON ge.id = gp.entry_id"
            " WHERE gp.user_id = ? AND gp.created_at >= ? ORDER BY gp.created_at ASC",
            (user_id, week_since),
        ).fetchall()
        for row in note_rows:
            _day(row["created_at"][:10])["garden_notes"].append({
                "plant_name": row["plant_name"],
                "has_photo": bool(row["image_path"]),
                "is_fertilization": bool(row["is_fertilization"]),
                "fertilizer_type": row["fertilizer_type"],
                "fertilization_date": row["fertilization_date"],
                "note_text": (row["notes"] or "")[:120],
            })

        today = date.fromisoformat(_local_today())
        week_activity = []
        for day_str in sorted(day_data.keys(), reverse=True):
            try:
                day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
            except Exception:
                continue
            diff = (today - day_date).days
            if diff <= 0:
                date_label = "Today"
            elif diff == 1:
                date_label = "Yesterday"
            else:
                date_label = day_date.strftime("%a %b %-d")
            entry = {k: v for k, v in day_data[day_str].items() if not k.startswith("_")}
            entry["date_label"] = date_label
            entry["day_str"] = day_str
            week_activity.append(entry)

        return {
            "plants": plants, "zones": zones, "garden": garden,
            "login_count": login_count, "last_login": last_login,
            "avg_per_week": avg_per_week,
            "week_activity": week_activity,
        }

    @app.route("/tools")
    @login_required
    def tools():
        users_with_stats = []
        perenual_log = []
        db = get_db()
        chat_error_log = []
        if g.user.get("is_admin"):
            rows = db.execute(
                "SELECT id, username, is_admin, created_at FROM users ORDER BY created_at ASC"
            ).fetchall()
            for u in rows:
                stats = _user_stats(db, u["id"], u["created_at"])
                users_with_stats.append({"user": u, "stats": stats})
            users_with_stats.sort(key=lambda x: x["stats"].get("last_login") or "", reverse=True)
            perenual_log = db.execute(
                "SELECT pl.query, pl.result_count, pl.logged_at, u.username"
                " FROM perenual_log pl LEFT JOIN users u ON u.id = pl.user_id"
                " ORDER BY pl.logged_at DESC LIMIT 200"
            ).fetchall()
            chat_error_log = db.execute(
                "SELECT username, user_message, error_type, error_detail, logged_at"
                " FROM chat_error_log ORDER BY logged_at DESC LIMIT 200"
            ).fetchall()
            ai_chat_entries = db.execute(
                "SELECT username, context, plant_name, user_message, logged_at"
                " FROM ai_chat_log ORDER BY logged_at DESC LIMIT 300"
            ).fetchall()
            suggestion_adds = db.execute(
                "SELECT al.item_name, al.logged_at, u.username"
                " FROM activity_log al JOIN users u ON u.id = al.user_id"
                " WHERE al.action = 'suggestion_added'"
                " ORDER BY al.logged_at DESC LIMIT 100"
            ).fetchall()
        else:
            ai_chat_entries = []
            suggestion_adds = []
        uid = g.user["id"]
        share_rows = db.execute(
            "SELECT gs.id, gs.confirmed, gs.requested_by, u.username AS partner_name "
            "FROM garden_shares gs "
            "JOIN users u ON u.id = CASE WHEN gs.user_a_id = ? THEN gs.user_b_id ELSE gs.user_a_id END "
            "WHERE gs.user_a_id = ? OR gs.user_b_id = ?",
            (uid, uid, uid),
        ).fetchall()
        garden_shares        = [{"id": r["id"], "partner_name": r["partner_name"]} for r in share_rows if r["confirmed"]]
        garden_shares_in     = [{"id": r["id"], "partner_name": r["partner_name"]} for r in share_rows if not r["confirmed"] and r["requested_by"] != uid]
        garden_shares_out    = [{"id": r["id"], "partner_name": r["partner_name"]} for r in share_rows if not r["confirmed"] and r["requested_by"] == uid]
        return render_template("settings.html", user=g.user, all_users=users_with_stats,
                               perenual_log=perenual_log,
                               chat_error_log=chat_error_log,
                               ai_chat_entries=ai_chat_entries,
                               suggestion_adds=suggestion_adds,
                               garden_shares=garden_shares,
                               garden_shares_in=garden_shares_in,
                               garden_shares_out=garden_shares_out,
                               plantid_configured=bool(os.environ.get("PLANT_ID_API_KEY", "").strip()),
                               gemini_configured=bool(os.environ.get("GEMINI_API_KEY", "").strip()),
                               claude_configured=bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
                               changelog=WHATS_NEW_CHANGELOG)

    @app.route("/admin/users/<int:target_id>", methods=["GET", "POST"])
    @login_required
    def admin_user_detail(target_id):
        if not g.user.get("is_admin"):
            return redirect(url_for("tools"))
        db = get_db()
        target = db.execute(
            "SELECT id, username, is_admin, created_at FROM users WHERE id = ?", (target_id,)
        ).fetchone()
        if target is None:
            flash("User not found.")
            return redirect(url_for("tools"))
        if request.method == "POST":
            new_password = request.form.get("new_password", "").strip()
            if not new_password:
                flash("New password is required.")
            elif len(new_password) < 8:
                flash("Password must be at least 8 characters.")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), target_id),
                )
                db.commit()
                flash(f"Password reset for {target['username']}.")
            return redirect(url_for("admin_user_detail", target_id=target_id))
        stats = _user_stats(db, target_id, target["created_at"])
        return render_template("admin_user_detail.html", target=target, stats=stats)

    @app.route("/admin/impersonate/<int:target_id>", methods=["POST"])
    @login_required
    def admin_impersonate(target_id):
        if not g.user.get("is_admin") or g.real_admin:
            return redirect(url_for("dashboard"))
        db = get_db()
        target = db.execute("SELECT id FROM users WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            flash("User not found.")
            return redirect(url_for("tools"))
        session["impersonating_id"] = target_id
        return redirect(url_for("dashboard"))

    @app.route("/admin/impersonate/stop", methods=["POST"])
    @login_required
    def admin_impersonate_stop():
        session.pop("impersonating_id", None)
        return redirect(url_for("tools"))

    @app.route("/admin/reset-password", methods=["POST"])
    @login_required
    def admin_reset_password():
        if not g.user.get("is_admin"):
            return jsonify(error="Forbidden"), 403
        user_id = request.form.get("user_id", "").strip()
        new_password = request.form.get("new_password", "").strip()
        if not user_id or not new_password:
            flash("User and new password are required.")
            return redirect(url_for("tools"))
        if len(new_password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("tools"))
        db = get_db()
        target = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            flash("User not found.")
            return redirect(url_for("tools"))
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user_id),
        )
        db.commit()
        flash(f"Password reset for {target['username']}.")
        return redirect(url_for("tools"))

    @app.route("/export/library.csv")
    @login_required
    def export_library_csv():
        db = get_db()
        uid = g.user["id"]
        plants = db.execute(
            "SELECT * FROM plants WHERE user_id = ? ORDER BY name ASC", (uid,)
        ).fetchall()
        tags_map = {}
        if plants:
            pids = [p["id"] for p in plants]
            ph = ",".join("?" * len(pids))
            for row in db.execute(
                f"SELECT pt.plant_id, t.name FROM plant_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.plant_id IN ({ph}) ORDER BY t.name ASC",
                pids,
            ).fetchall():
                tags_map.setdefault(row["plant_id"], []).append(row["name"])
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Name", "Scientific Name", "Sun", "Lifecycle", "Evergreen Status",
                    "Size", "Flowering Schedule", "Tags", "Notes", "Source", "Added Date"])
        for p in plants:
            w.writerow([
                p["name"], p["scientific_name"] or "", p["sun_exposure"] or "",
                p["lifecycle"] or "", p["evergreen_status"] or "", p["size_info"] or "",
                p["flowering_schedule"] or "", ", ".join(tags_map.get(p["id"], [])),
                p["notes"] or "", p["source_note"] or "",
                str(p["created_at"])[:10] if p["created_at"] else "",
            ])
        return Response(
            "﻿" + out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="gardenpal-library.csv"'},
        )

    @app.route("/export/yard.csv")
    @login_required
    def export_yard_csv():
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        rows = db.execute(
            f"""
            SELECT z.name AS zone_name, z.description AS zone_desc,
                   yp.plant_name, yp.scientific_name, yp.sun_needs, yp.lifecycle,
                   yp.size_info, yp.notes, yp.created_at
            FROM yard_plants yp
            JOIN yard_zones z ON z.id = yp.zone_id
            WHERE yp.user_id IN {ph}
            ORDER BY z.name ASC, yp.plant_name ASC
            """,
            id_args,
        ).fetchall()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Zone", "Zone Description", "Plant", "Scientific Name",
                    "Sun", "Lifecycle", "Size", "Notes", "Added Date"])
        for r in rows:
            w.writerow([
                r["zone_name"], r["zone_desc"] or "", r["plant_name"],
                r["scientific_name"] or "", r["sun_needs"] or "", r["lifecycle"] or "",
                r["size_info"] or "", r["notes"] or "",
                str(r["created_at"])[:10] if r["created_at"] else "",
            ])
        return Response(
            "﻿" + out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="gardenpal-yard.csv"'},
        )

    @app.route("/export/garden.csv")
    @login_required
    def export_garden_csv():
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entries = db.execute(
            f"""
            SELECT plant_name, variety, location_type, location_name,
                   planted_date, notes, created_at
            FROM garden_entries WHERE user_id IN {ph}
            ORDER BY planted_date ASC NULLS LAST, plant_name ASC
            """,
            id_args,
        ).fetchall()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Plant", "Variety", "Location Type", "Location Name",
                    "Planted Date", "Notes", "Added Date"])
        for e in entries:
            w.writerow([
                e["plant_name"], e["variety"] or "",
                e["location_type"] or "", e["location_name"] or "",
                str(e["planted_date"])[:10] if e["planted_date"] else "",
                e["notes"] or "",
                str(e["created_at"])[:10] if e["created_at"] else "",
            ])
        return Response(
            "﻿" + out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="gardenpal-garden.csv"'},
        )

    @app.route("/settings/photo-id-provider", methods=["POST"])
    @login_required
    def set_photo_id_provider():
        provider = request.form.get("provider", "plantid").strip()
        if provider not in ("plantid", "gemini", "claude"):
            provider = "plantid"
        db = get_db()
        db.execute("UPDATE users SET photo_id_provider = ? WHERE id = ?", (provider, g.user["id"]))
        db.commit()
        flash("Plant photo identification method updated.")
        return redirect(url_for("tools"))

    @app.route("/settings/location", methods=["POST"])
    @login_required
    def set_location():
        location = request.form.get("location", "").strip()
        db = get_db()
        db.execute("UPDATE users SET location = ? WHERE id = ?", (location or None, g.user["id"]))
        db.commit()
        flash("Location updated." if location else "Location cleared.")
        return redirect(url_for("tools"))

    @app.route("/settings/share-garden", methods=["POST"])
    @login_required
    def share_garden():
        partner_username = request.form.get("partner_username", "").strip()
        db = get_db()
        partner = db.execute(
            "SELECT id, username FROM users WHERE lower(username) = lower(?)",
            (partner_username,),
        ).fetchone()
        if partner is None:
            flash("No account found with that username.")
            return redirect(url_for("tools"))
        if partner["id"] == g.user["id"]:
            flash("You can't share a garden with yourself.")
            return redirect(url_for("tools"))
        a_id = min(g.user["id"], partner["id"])
        b_id = max(g.user["id"], partner["id"])
        existing = db.execute(
            "SELECT id, confirmed, requested_by FROM garden_shares WHERE user_a_id = ? AND user_b_id = ?",
            (a_id, b_id),
        ).fetchone()
        if existing:
            if existing["confirmed"]:
                flash(f"You're already sharing a garden with {partner['username']}.")
            elif existing["requested_by"] == g.user["id"]:
                flash(f"You've already sent an invite to {partner['username']} — waiting for them to add you back.")
            else:
                # Partner already invited us — confirm the share
                db.execute("UPDATE garden_shares SET confirmed = 1 WHERE id = ?", (existing["id"],))
                db.commit()
                flash(f"Garden sharing with {partner['username']} is now active.")
            return redirect(url_for("tools"))
        db.execute(
            "INSERT INTO garden_shares (user_a_id, user_b_id, created_at, confirmed, requested_by) VALUES (?, ?, ?, 0, ?)",
            (a_id, b_id, datetime.utcnow().isoformat(timespec="seconds"), g.user["id"]),
        )
        db.commit()
        flash(f"Partner added. Sharing becomes active once {partner['username']} adds you back in their settings.")
        return redirect(url_for("tools"))

    @app.route("/settings/unshare-garden/<int:share_id>", methods=["POST"])
    @login_required
    def unshare_garden(share_id):
        db = get_db()
        row = db.execute(
            "SELECT confirmed FROM garden_shares WHERE id = ? AND (user_a_id = ? OR user_b_id = ?)",
            (share_id, g.user["id"], g.user["id"]),
        ).fetchone()
        if row:
            db.execute("DELETE FROM garden_shares WHERE id = ?", (share_id,))
            db.commit()
            flash("Invite cancelled." if not row["confirmed"] else "Garden sharing removed.")
        return redirect(url_for("tools"))

    @app.route("/settings/generate-token", methods=["POST"])
    @login_required
    def generate_api_token():
        db = get_db()
        token = secrets.token_urlsafe(32)
        db.execute("UPDATE users SET api_token = ? WHERE id = ?", (token, g.user["id"]))
        db.commit()
        flash("New API token generated.")
        return redirect(url_for("tools"))

    @app.route("/settings/revoke-token", methods=["POST"])
    @login_required
    def revoke_api_token():
        db = get_db()
        db.execute("UPDATE users SET api_token = NULL WHERE id = ?", (g.user["id"],))
        db.commit()
        flash("API token revoked.")
        return redirect(url_for("tools"))

    # ── Garden tracker (UI) ─────────────────────────────────────────────────

    @app.route("/garden")
    @login_required
    def garden_index():
        from datetime import date, timedelta
        from collections import defaultdict
        db = get_db()
        uid = g.user["id"]
        ids = _shared_user_ids(db, uid)
        ph, id_args = _in_ids(ids)
        today_str = _local_today()
        current_year = date.fromisoformat(today_str).year
        fert_deadline = _local_date_plus(3)

        # All dated entries across all years
        entries = db.execute(
            f"SELECT * FROM garden_entries WHERE user_id IN {ph} AND planted_date IS NOT NULL"
            " ORDER BY planted_date ASC, plant_name ASC",
            id_args,
        ).fetchall()

        # Undated entries always shown
        unscheduled = db.execute(
            f"SELECT * FROM garden_entries WHERE user_id IN {ph} AND planted_date IS NULL"
            " ORDER BY plant_name ASC",
            id_args,
        ).fetchall()

        # Group by (year, month)
        grouped = defaultdict(list)
        for entry in entries:
            pd = entry.planted_date
            yr = pd.year if hasattr(pd, "year") else int(str(pd)[:4])
            mo = pd.month if hasattr(pd, "month") else int(str(pd)[5:7])
            grouped[(yr, mo)].append(entry)

        grouped_entries = sorted(grouped.items())  # [((yr, mo), [entries]), ...]

        shared_names = _shared_user_names(db, uid)
        ff_fert = _feature_fertilization(g.user)
        ff_water = _feature_watering(g.user)
        water_deadline = _local_date_plus(1)
        return render_template(
            "garden_index.html",
            grouped_entries=grouped_entries,
            unscheduled=unscheduled,
            current_year=current_year,
            shared_names=shared_names,
            today=today_str,
            fert_deadline=fert_deadline,
            ff_fert=ff_fert,
            ff_water=ff_water,
            water_deadline=water_deadline,
        )

    @app.route("/garden/new", methods=["GET", "POST"])
    @login_required
    def garden_new():
        db = get_db()
        feature_gz = _feature_garden_zones(g.user)
        zones_json = []
        if feature_gz:
            zones_json = [{"id": r["id"], "name": r["name"]} for r in
                          db.execute("SELECT id, name FROM yard_zones WHERE user_id = ? ORDER BY name ASC",
                                     (g.user["id"],)).fetchall()]
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        if request.method == "POST":
            plant_name = request.form.get("plant_name", "").strip()
            if not plant_name:
                flash("Plant name is required.")
                location_names = [r["location_name"] for r in db.execute(f"SELECT DISTINCT location_name FROM garden_entries WHERE user_id IN {ph} AND location_name IS NOT NULL ORDER BY location_name ASC", id_args).fetchall()]
                plant_names, plant_varieties = _build_plant_autocomplete_data(db, ids)
                return render_template("garden_entry_new.html", form_values=request.form,
                                       feature_garden_zones=feature_gz, zones_json=zones_json,
                                       location_names=location_names,
                                       plant_names=plant_names, plant_varieties=plant_varieties)
            zone_id_to_save = None
            if feature_gz:
                zone_name_input = request.form.get("zone_name", "").strip()
                zone_id_input = request.form.get("zone_id", "").strip()
                if zone_name_input:
                    if zone_id_input:
                        try:
                            zid = int(zone_id_input)
                            zrow = db.execute("SELECT id FROM yard_zones WHERE id = ? AND user_id = ?", (zid, g.user["id"])).fetchone()
                            if zrow:
                                zone_id_to_save = zrow["id"]
                        except (ValueError, TypeError):
                            pass
                    if zone_id_to_save is None:
                        zrow = db.execute("SELECT id FROM yard_zones WHERE user_id = ? AND lower(name) = lower(?)", (g.user["id"], zone_name_input)).fetchone()
                        if zrow:
                            zone_id_to_save = zrow["id"]
                        else:
                            now_z = datetime.utcnow().isoformat(timespec="seconds")
                            zone_id_to_save = db.execute(
                                "INSERT INTO yard_zones (user_id, name, created_at) VALUES (?, ?, ?) RETURNING id",
                                (g.user["id"], zone_name_input, now_z),
                            ).fetchone()["id"]
            now = datetime.utcnow().isoformat(timespec="seconds")
            entry_id = db.execute(
                """INSERT INTO garden_entries
                   (user_id, plant_name, variety, location_type, location_name, planted_date, notes, zone_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    g.user["id"],
                    plant_name,
                    request.form.get("variety", "").strip() or None,
                    request.form.get("location_type", "").strip() or None,
                    request.form.get("location_name", "").strip() or None,
                    request.form.get("planted_date", "").strip() or None,
                    request.form.get("notes", "").strip() or None,
                    zone_id_to_save,
                    now, now,
                ),
            ).fetchone()["id"]
            _log_activity(db, g.user["id"], "garden_entry_added", plant_name)
            db.commit()
            flash("Entry added to your garden tracker.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        today = _local_today()
        location_names = [
            r["location_name"] for r in db.execute(
                f"SELECT DISTINCT location_name FROM garden_entries WHERE user_id IN {ph} AND location_name IS NOT NULL ORDER BY location_name ASC",
                id_args,
            ).fetchall()
        ]
        plant_names, plant_varieties = _build_plant_autocomplete_data(db, ids)
        # Pre-populate zone if navigated from a zone page
        prefill_zone_id = request.args.get("zone_id", type=int)
        prefill_zone_name = ""
        if feature_gz and prefill_zone_id:
            zrow = db.execute(
                "SELECT name FROM yard_zones WHERE id = ? AND user_id = ?",
                (prefill_zone_id, g.user["id"]),
            ).fetchone()
            if zrow:
                prefill_zone_name = zrow["name"]
        return render_template("garden_entry_new.html",
                               form_values={"planted_date": today,
                                            "zone_name": prefill_zone_name,
                                            "zone_id": str(prefill_zone_id) if prefill_zone_name else ""},
                               feature_garden_zones=feature_gz, zones_json=zones_json,
                               location_names=location_names,
                               plant_names=plant_names, plant_varieties=plant_varieties)

    @app.route("/garden/<int:entry_id>")
    @login_required
    def garden_detail(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT * FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        photos = db.execute(
            "SELECT * FROM garden_photos WHERE entry_id = ? ORDER BY photo_date DESC NULLS LAST, created_at DESC",
            (entry_id,),
        ).fetchall()
        today = _local_today()

        # Determine last fertilized: most recent of explicit entry field vs detected growth-log note
        # fertilization_date is the AI-inferred actual date; falls back to photo_date if not set
        last_fert_photo = db.execute(
            "SELECT COALESCE(fertilization_date, photo_date) AS fert_date, fertilizer_type"
            " FROM garden_photos WHERE entry_id = ? AND is_fertilization = 1"
            " ORDER BY COALESCE(fertilization_date, photo_date) DESC NULLS LAST, created_at DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        explicit_date = (entry["last_fertilized_date"] or "") if entry["last_fertilized_date"] else ""
        photo_fert_date = (last_fert_photo["fert_date"] or "") if last_fert_photo else ""
        if explicit_date and (not photo_fert_date or explicit_date >= photo_fert_date):
            last_fertilized = {"date": explicit_date, "type": entry["last_fertilizer_type"]}
        elif last_fert_photo:
            last_fertilized = {"date": photo_fert_date or None, "type": last_fert_photo["fertilizer_type"]}
        else:
            last_fertilized = None

        # Refresh next-fertilization suggestion when stale (skip if plant is never-fertilize)
        if not entry["never_fertilize"]:
            gen_at = entry["next_fertilization_generated_at"] if entry["next_fertilization_generated_at"] else None
            last_fert_date = last_fertilized["date"] if last_fertilized else None
            last_note_row = db.execute(
                "SELECT created_at FROM garden_photos WHERE entry_id = ? ORDER BY created_at DESC LIMIT 1",
                (entry_id,),
            ).fetchone()
            last_note_at = last_note_row["created_at"] if last_note_row else None
            needs_regen = (
                not gen_at
                or (last_fert_date and last_fert_date > gen_at[:10])
                or (entry["next_fertilization_date"] and entry["next_fertilization_date"] < today and not last_fert_date)
                or (last_note_at and gen_at and last_note_at > gen_at)
            )
            if needs_regen:
                growth_notes = [
                    (r["photo_date"], r["notes"])
                    for r in db.execute(
                        "SELECT photo_date, notes FROM garden_photos WHERE entry_id = ? AND notes IS NOT NULL"
                        " ORDER BY photo_date ASC NULLS LAST, created_at ASC",
                        (entry_id,),
                    ).fetchall()
                ]
                user_location = g.user.get("location", "")
                fert_allowed, _ = _check_api_rate(db, g.user["id"], "fertilization")
                suggestion = _suggest_next_fertilization(db, entry, user_location, last_fertilized, growth_notes) if fert_allowed else None
                if suggestion:
                    entry = db.execute("SELECT * FROM garden_entries WHERE id = ?", (entry_id,)).fetchone()
            next_fertilization = {
                "date": entry["next_fertilization_date"],
                "note": entry["next_fertilization_note"],
                "planned_date": entry["planned_fertilization_date"],
                "never": False,
            }
        else:
            next_fertilization = {"date": None, "note": None, "planned_date": None, "never": True}

        fert_deadline = _local_date_plus(3)
        ff_fert = _feature_fertilization(g.user)

        ff_water = _feature_watering(g.user)
        last_watered = None
        next_watering = None
        water_deadline = _local_date_plus(1)
        if ff_water:
            last_watered_date = entry.get("last_watered_date") or None
            last_watered = {"date": last_watered_date}
            if entry.get("never_water"):
                next_watering = {"date": None, "note": None, "frequency_days": None, "never": True}
            else:
                needs_regen = not entry.get("watering_generated_at")
                if needs_regen:
                    water_allowed, _ = _check_api_rate(db, g.user["id"], "watering")
                    if water_allowed:
                        water_growth_notes = [
                            (r["photo_date"], r["notes"])
                            for r in db.execute(
                                "SELECT photo_date, notes FROM garden_photos WHERE entry_id = ?"
                                " AND notes IS NOT NULL ORDER BY photo_date ASC NULLS LAST, created_at ASC",
                                (entry_id,),
                            ).fetchall()
                        ]
                        _suggest_watering_frequency(db, entry, g.user.get("location", ""), last_watered_date, water_growth_notes)
                        entry = db.execute("SELECT * FROM garden_entries WHERE id = ?", (entry_id,)).fetchone()
                next_watering = {
                    "date": entry.get("next_watering_date"),
                    "note": entry.get("watering_note"),
                    "frequency_days": entry.get("watering_frequency_days"),
                    "never": False,
                }

        feature_gz = _feature_garden_zones(g.user)
        entry_zone = None
        if feature_gz and entry["zone_id"]:
            zone_row = db.execute(
                "SELECT id, name FROM yard_zones WHERE id = ?", (entry["zone_id"],)
            ).fetchone()
            if zone_row:
                entry_zone = {"id": zone_row["id"], "name": zone_row["name"]}
        from_zone_id = request.args.get("from_zone", type=int)
        if feature_gz and from_zone_id:
            back_href = url_for("yard_zone_detail", zone_id=from_zone_id)
            back_label = "Zone"
        else:
            back_href = url_for("garden_index")
            back_label = "Edibles"
        return render_template("garden_entry_detail.html", entry=entry, photos=photos, today=today,
                               last_fertilized=last_fertilized, next_fertilization=next_fertilization,
                               fert_deadline=fert_deadline, ff_fert=ff_fert,
                               ff_water=ff_water, last_watered=last_watered, next_watering=next_watering,
                               water_deadline=water_deadline,
                               feature_gz=feature_gz, entry_zone=entry_zone,
                               back_href=back_href, back_label=back_label)

    @app.route("/garden/<int:entry_id>/photos", methods=["POST"])
    @login_required
    def garden_add_photo(entry_id):
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT id FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            if is_ajax:
                return jsonify(error="Entry not found."), 404
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"], g.user["id"], "garden")
        note_text = request.form.get("photo_notes", "").strip() or None
        if not image_path and not note_text:
            if is_ajax:
                return jsonify(error="Please add a photo or write a note."), 400
            flash("Please add a photo or write a note.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        photo_date = request.form.get("photo_date", "").strip() or None
        planted_row = db.execute("SELECT planted_date FROM garden_entries WHERE id = ?", (entry_id,)).fetchone()
        planted_date = planted_row["planted_date"] if planted_row else None
        is_fert, fert_type, fert_date = _detect_fertilization(note_text)
        if is_fert and _has_date_hint(note_text):
            fert_date = _extract_fertilization_date(note_text, photo_date, planted_date)
        elif not is_fert and note_text:
            is_fert, fert_type, fert_date = _ai_detect_fertilization(note_text, photo_date, planted_date)
        db.execute(
            "INSERT INTO garden_photos (entry_id, user_id, image_path, photo_date, notes, created_at, is_fertilization, fertilizer_type, fertilization_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                g.user["id"],
                image_path,
                photo_date,
                note_text,
                datetime.utcnow().isoformat(timespec="seconds"),
                is_fert,
                fert_type,
                fert_date,
            ),
        )
        if is_fert:
            effective_date = fert_date or photo_date or _local_today()
            db.execute(
                "UPDATE garden_entries SET next_fertilization_generated_at = NULL,"
                " planned_fertilization_date = CASE"
                " WHEN planned_fertilization_date IS NOT NULL AND planned_fertilization_date <= ? THEN NULL"
                " ELSE planned_fertilization_date END WHERE id = ?",
                (effective_date, entry_id),
            )
        db.commit()
        if is_ajax:
            return jsonify(ok=True)
        flash("Note added.")
        return redirect(url_for("garden_detail", entry_id=entry_id))

    @app.route("/garden/photos/<int:photo_id>/edit", methods=["GET", "POST"])
    @login_required
    def garden_photo_edit(photo_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        photo = db.execute(
            f"SELECT gp.*, ge.id AS entry_id, ge.plant_name, ge.planted_date"
            f" FROM garden_photos gp JOIN garden_entries ge ON ge.id = gp.entry_id"
            f" WHERE gp.id = ? AND ge.user_id IN {ph}",
            [photo_id] + id_args,
        ).fetchone()
        if photo is None:
            flash("Note not found.")
            return redirect(url_for("garden_index"))
        if request.method == "POST":
            notes = request.form.get("notes", "").strip() or None
            photo_date = request.form.get("photo_date", "").strip() or None
            planted_date = photo["planted_date"]
            is_fert, fert_type, fert_date = _detect_fertilization(notes)
            if is_fert and _has_date_hint(notes):
                fert_date = _extract_fertilization_date(notes, photo_date, planted_date)
            elif not is_fert and notes:
                is_fert, fert_type, fert_date = _ai_detect_fertilization(notes, photo_date, planted_date)
            new_image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"], g.user["id"], "garden")
            update_fields = [notes, photo_date, is_fert, fert_type, fert_date]
            if new_image_path:
                db.execute(
                    "UPDATE garden_photos SET notes = ?, photo_date = ?, is_fertilization = ?,"
                    " fertilizer_type = ?, fertilization_date = ?, image_path = ? WHERE id = ?",
                    update_fields + [new_image_path, photo_id],
                )
            else:
                db.execute(
                    "UPDATE garden_photos SET notes = ?, photo_date = ?, is_fertilization = ?,"
                    " fertilizer_type = ?, fertilization_date = ? WHERE id = ?",
                    update_fields + [photo_id],
                )
            db.commit()
            return redirect(url_for("garden_detail", entry_id=photo["entry_id"]))
        return render_template("garden_photo_edit.html", photo=photo)

    @app.route("/garden/photos/<int:photo_id>/delete", methods=["POST"])
    @login_required
    def garden_photo_delete(photo_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        photo = db.execute(
            f"SELECT gp.entry_id FROM garden_photos gp"
            f" JOIN garden_entries ge ON ge.id = gp.entry_id"
            f" WHERE gp.id = ? AND ge.user_id IN {ph}",
            [photo_id] + id_args,
        ).fetchone()
        if photo is None:
            flash("Note not found.")
            return redirect(url_for("garden_index"))
        entry_id = photo["entry_id"]
        db.execute("DELETE FROM garden_photos WHERE id = ?", (photo_id,))
        db.commit()
        return redirect(url_for("garden_detail", entry_id=entry_id))

    @app.route("/garden/photos/<int:photo_id>/update", methods=["POST"])
    @login_required
    def garden_photo_update(photo_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        photo = db.execute(
            f"SELECT id FROM garden_photos WHERE id = ? AND user_id IN {ph}",
            [photo_id] + id_args,
        ).fetchone()
        if photo is None:
            return jsonify(error="Not found"), 404
        notes = request.form.get("notes", "").strip() or None
        photo_date = request.form.get("photo_date", "").strip() or None
        planted_row = db.execute(
            "SELECT ge.planted_date FROM garden_photos gp"
            " JOIN garden_entries ge ON ge.id = gp.entry_id WHERE gp.id = ?",
            (photo_id,),
        ).fetchone()
        planted_date = planted_row["planted_date"] if planted_row else None
        is_fert, fert_type, fert_date = _detect_fertilization(notes)
        if is_fert and _has_date_hint(notes):
            fert_date = _extract_fertilization_date(notes, photo_date, planted_date)
        elif not is_fert and notes:
            is_fert, fert_type, fert_date = _ai_detect_fertilization(notes, photo_date, planted_date)
        db.execute(
            "UPDATE garden_photos SET notes = ?, photo_date = ?, is_fertilization = ?, fertilizer_type = ?, fertilization_date = ? WHERE id = ?",
            [notes, photo_date, is_fert, fert_type, fert_date, photo_id],
        )
        db.commit()
        return jsonify(ok=True)

    @app.route("/garden/<int:entry_id>/edit", methods=["GET", "POST"])
    @login_required
    def garden_edit(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT * FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        feature_gz = _feature_garden_zones(g.user)
        zones_json = []
        current_zone_name = ""
        current_zone_id = ""
        if feature_gz:
            zones_json = [{"id": r["id"], "name": r["name"]} for r in
                          db.execute("SELECT id, name FROM yard_zones WHERE user_id = ? ORDER BY name ASC",
                                     (g.user["id"],)).fetchall()]
            if entry["zone_id"]:
                zrow = db.execute("SELECT id, name FROM yard_zones WHERE id = ?", (entry["zone_id"],)).fetchone()
                if zrow:
                    current_zone_name = zrow["name"]
                    current_zone_id = str(zrow["id"])
        plant_names, plant_varieties = _build_plant_autocomplete_data(db, ids)
        if request.method == "POST":
            plant_name = request.form.get("plant_name", "").strip()
            if not plant_name:
                flash("Plant name is required.")
                _loc_names = [r["location_name"] for r in db.execute(f"SELECT DISTINCT location_name FROM garden_entries WHERE user_id IN {ph} AND location_name IS NOT NULL ORDER BY location_name ASC", id_args).fetchall()]
                return render_template("garden_entry_edit.html", entry=entry, form_values=request.form,
                                       feature_garden_zones=feature_gz, zones_json=zones_json,
                                       zone_name=request.form.get("zone_name", ""),
                                       zone_id=request.form.get("zone_id", ""),
                                       location_names=_loc_names,
                                       plant_names=plant_names, plant_varieties=plant_varieties)
            zone_id_to_save = entry["zone_id"]  # default: keep existing
            if feature_gz:
                zone_name_input = request.form.get("zone_name", "").strip()
                zone_id_input = request.form.get("zone_id", "").strip()
                if zone_name_input:
                    resolved = None
                    if zone_id_input:
                        try:
                            zid = int(zone_id_input)
                            zrow = db.execute("SELECT id FROM yard_zones WHERE id = ? AND user_id = ?", (zid, g.user["id"])).fetchone()
                            if zrow:
                                resolved = zrow["id"]
                        except (ValueError, TypeError):
                            pass
                    if resolved is None:
                        zrow = db.execute("SELECT id FROM yard_zones WHERE user_id = ? AND lower(name) = lower(?)", (g.user["id"], zone_name_input)).fetchone()
                        if zrow:
                            resolved = zrow["id"]
                        else:
                            now_z = datetime.utcnow().isoformat(timespec="seconds")
                            resolved = db.execute(
                                "INSERT INTO yard_zones (user_id, name, created_at) VALUES (?, ?, ?) RETURNING id",
                                (g.user["id"], zone_name_input, now_z),
                            ).fetchone()["id"]
                    zone_id_to_save = resolved
                else:
                    zone_id_to_save = None  # user cleared the zone field
            never_fertilize = 1 if request.form.get("never_fertilize") else 0
            db.execute(
                f"""UPDATE garden_entries
                   SET plant_name = ?, variety = ?, location_type = ?, location_name = ?,
                       planted_date = ?, notes = ?, last_fertilized_date = ?, last_fertilizer_type = ?,
                       never_fertilize = ?, zone_id = ?, updated_at = ?
                   WHERE id = ? AND user_id IN {ph}""",
                [
                    plant_name,
                    request.form.get("variety", "").strip() or None,
                    request.form.get("location_type", "").strip() or None,
                    request.form.get("location_name", "").strip() or None,
                    request.form.get("planted_date", "").strip() or None,
                    request.form.get("notes", "").strip() or None,
                    request.form.get("last_fertilized_date", "").strip() or None,
                    request.form.get("last_fertilizer_type", "").strip() or None,
                    never_fertilize,
                    zone_id_to_save,
                    datetime.utcnow().isoformat(timespec="seconds"),
                    entry_id,
                ] + id_args,
            )
            new_fert_date = request.form.get("last_fertilized_date", "").strip() or None
            new_fert_type = request.form.get("last_fertilizer_type", "").strip() or None
            old_fert_date = entry["last_fertilized_date"] if entry["last_fertilized_date"] else None
            if never_fertilize:
                db.execute(
                    "UPDATE garden_entries SET next_fertilization_date = NULL, next_fertilization_note = NULL,"
                    " next_fertilization_generated_at = NULL, planned_fertilization_date = NULL WHERE id = ?",
                    (entry_id,),
                )
            elif new_fert_date and new_fert_date != old_fert_date:
                note_text = "Fertilized" + (f" with {new_fert_type}" if new_fert_type else "")
                db.execute(
                    "INSERT INTO garden_photos (entry_id, user_id, photo_date, notes, created_at, is_fertilization, fertilizer_type, fertilization_date)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (entry_id, g.user["id"], new_fert_date, note_text,
                     datetime.utcnow().isoformat(timespec="seconds"), new_fert_type, new_fert_date),
                )
            if not never_fertilize and new_fert_date != old_fert_date:
                db.execute(
                    "UPDATE garden_entries SET next_fertilization_generated_at = NULL,"
                    " planned_fertilization_date = CASE"
                    " WHEN planned_fertilization_date IS NOT NULL AND ? IS NOT NULL AND planned_fertilization_date <= ? THEN NULL"
                    " ELSE planned_fertilization_date END WHERE id = ?",
                    (new_fert_date, new_fert_date, entry_id),
                )
            db.commit()
            flash("Entry updated.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        location_names = [r["location_name"] for r in db.execute(f"SELECT DISTINCT location_name FROM garden_entries WHERE user_id IN {ph} AND location_name IS NOT NULL ORDER BY location_name ASC", id_args).fetchall()]
        last_fert_photo = db.execute(
            "SELECT COALESCE(fertilization_date, photo_date) AS fert_date, fertilizer_type"
            " FROM garden_photos WHERE entry_id = ? AND is_fertilization = 1"
            " ORDER BY COALESCE(fertilization_date, photo_date) DESC NULLS LAST, created_at DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        explicit_date = (entry["last_fertilized_date"] or "") if entry["last_fertilized_date"] else ""
        photo_fert_date = (last_fert_photo["fert_date"] or "") if last_fert_photo else ""
        if explicit_date and (not photo_fert_date or explicit_date >= photo_fert_date):
            last_fertilized = {"date": explicit_date, "type": entry["last_fertilizer_type"]}
        elif last_fert_photo:
            last_fertilized = {"date": photo_fert_date or None, "type": last_fert_photo["fertilizer_type"]}
        else:
            last_fertilized = None
        next_fertilization = {
            "date": entry["next_fertilization_date"],
            "note": entry["next_fertilization_note"],
            "planned_date": entry["planned_fertilization_date"],
            "never": bool(entry["never_fertilize"]),
        }
        _today = _local_today()
        _fert_deadline = _local_date_plus(3)
        return render_template("garden_entry_edit.html", entry=entry, form_values=entry,
                               feature_garden_zones=feature_gz, zones_json=zones_json,
                               zone_name=current_zone_name, zone_id=current_zone_id,
                               location_names=location_names, last_fertilized=last_fertilized,
                               next_fertilization=next_fertilization,
                               today=_today, fert_deadline=_fert_deadline,
                               plant_names=plant_names, plant_varieties=plant_varieties)

    @app.route("/garden/<int:entry_id>/delete", methods=["POST"])
    @login_required
    def garden_delete(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        db.execute(f"DELETE FROM garden_entries WHERE id = ? AND user_id IN {ph}", [entry_id] + id_args)
        db.commit()
        flash("Entry deleted.")
        return redirect(url_for("garden_index"))

    @app.route("/garden/<int:entry_id>/plan-fertilize", methods=["POST"])
    @login_required
    def garden_plan_fertilize(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT id, last_fertilized_date FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        planned_date = request.form.get("planned_date", "").strip() or None
        never = 1 if request.form.get("never_fertilize") else 0
        clear_fertilized = bool(request.form.get("clear_fertilized"))
        last_fert_date = request.form.get("last_fertilized_date", "").strip() or None
        last_fert_type = request.form.get("last_fertilizer_type", "").strip() or None
        if last_fert_date and not _ISO_DATE_RE.match(last_fert_date):
            last_fert_date = None
        if clear_fertilized:
            last_fert_date = None
            last_fert_type = None
        fert_date_changed = last_fert_date != (entry["last_fertilized_date"] or None)
        if never:
            db.execute(
                "UPDATE garden_entries SET never_fertilize = 1, planned_fertilization_date = NULL,"
                " next_fertilization_date = NULL, next_fertilization_note = NULL,"
                " next_fertilization_generated_at = NULL,"
                " last_fertilized_date = ?, last_fertilizer_type = ? WHERE id = ?",
                (last_fert_date, last_fert_type, entry_id),
            )
        else:
            db.execute(
                "UPDATE garden_entries SET planned_fertilization_date = ?, never_fertilize = 0,"
                " last_fertilized_date = ?,"
                " last_fertilizer_type = COALESCE(?, last_fertilizer_type)"
                + (", next_fertilization_generated_at = NULL" if fert_date_changed else "")
                + " WHERE id = ?",
                (planned_date, last_fert_date, last_fert_type, entry_id),
            )
        if clear_fertilized:
            db.execute(
                "UPDATE garden_entries SET last_fertilizer_type = NULL WHERE id = ?",
                (entry_id,),
            )
            db.execute(
                "UPDATE garden_photos SET is_fertilization = 0, fertilization_date = NULL, fertilizer_type = NULL"
                " WHERE entry_id = ? AND is_fertilization = 1",
                (entry_id,),
            )
        db.commit()
        if request.form.get("from_dashboard"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("garden_detail", entry_id=entry_id))

    @app.route("/garden/<int:entry_id>/never-fertilize", methods=["POST"])
    @login_required
    def garden_set_never_fertilize(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT id FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            return jsonify({"error": "Not found"}), 404
        never = 1 if request.form.get("never_fertilize") else 0
        if never:
            db.execute(
                "UPDATE garden_entries SET never_fertilize = 1, planned_fertilization_date = NULL,"
                " next_fertilization_date = NULL, next_fertilization_note = NULL,"
                " next_fertilization_generated_at = NULL WHERE id = ?",
                (entry_id,),
            )
        else:
            db.execute("UPDATE garden_entries SET never_fertilize = 0 WHERE id = ?", (entry_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.route("/garden/<int:entry_id>/plan-watering", methods=["POST"])
    @login_required
    def garden_plan_watering(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT id, watering_frequency_days FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        if "has_tracking_update" in request.form:
            never_water = 1 if request.form.get("never_water") else 0
            if never_water:
                db.execute(
                    "UPDATE garden_entries SET never_water = 1, next_watering_date = NULL, watering_generated_at = NULL WHERE id = ?",
                    (entry_id,),
                )
                db.commit()
                return redirect(url_for("garden_detail", entry_id=entry_id))
            else:
                db.execute("UPDATE garden_entries SET never_water = 0 WHERE id = ?", (entry_id,))
        raw_date = request.form.get("last_watered_date", "").strip()
        if not raw_date:
            raw_date = _local_today()
        if not _ISO_DATE_RE.match(raw_date):
            raw_date = _local_today()
        freq = entry["watering_frequency_days"]
        if freq:
            next_date = (date.fromisoformat(raw_date) + timedelta(days=freq)).isoformat()
        else:
            next_date = None
        db.execute(
            "UPDATE garden_entries SET last_watered_date = ?, next_watering_date = ? WHERE id = ?",
            (raw_date, next_date, entry_id),
        )
        db.commit()
        if request.form.get("from_dashboard"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("garden_detail", entry_id=entry_id))

    @app.route("/garden/<int:entry_id>/never-water", methods=["POST"])
    @login_required
    def garden_set_never_water(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT id FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            return jsonify({"error": "Not found"}), 404
        never = 1 if request.form.get("never_water") else 0
        if never:
            db.execute(
                "UPDATE garden_entries SET never_water = 1, next_watering_date = NULL, watering_generated_at = NULL WHERE id = ?",
                (entry_id,),
            )
        else:
            db.execute("UPDATE garden_entries SET never_water = 0 WHERE id = ?", (entry_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.route("/garden/<int:entry_id>/watered-today", methods=["POST"])
    @login_required
    def garden_watered_today(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT id, watering_frequency_days FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            return jsonify({"error": "Not found"}), 404
        today = _local_today()
        freq = entry["watering_frequency_days"]
        next_date = (date.fromisoformat(today) + timedelta(days=freq)).isoformat() if freq else None
        db.execute(
            "UPDATE garden_entries SET last_watered_date = ?, next_watering_date = ?, never_water = 0 WHERE id = ?",
            (today, next_date, entry_id),
        )
        db.commit()
        return jsonify({"ok": True, "today": today, "next_date": next_date})

    @app.route("/garden/<int:entry_id>/fertilized-today", methods=["POST"])
    @login_required
    def garden_fertilized_today(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT * FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            return jsonify({"error": "Not found"}), 404
        today = _local_today()
        fertilizer_type = (request.form.get("fertilizer_type") or "").strip() or None
        db.execute(
            "UPDATE garden_entries SET last_fertilized_date = ?, last_fertilizer_type = COALESCE(?, last_fertilizer_type),"
            " next_fertilization_generated_at = NULL WHERE id = ?",
            (today, fertilizer_type, entry_id),
        )
        note_text = "Fertilized" + (f" — {fertilizer_type}" if fertilizer_type else "")
        db.execute(
            "INSERT INTO garden_photos"
            " (entry_id, user_id, photo_date, notes, created_at, is_fertilization, fertilizer_type, fertilization_date)"
            " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (entry_id, g.user["id"], today, note_text,
             datetime.utcnow().isoformat(timespec="seconds"), fertilizer_type, today),
        )
        db.commit()
        entry = db.execute("SELECT * FROM garden_entries WHERE id = ?", [entry_id]).fetchone()
        growth_notes = db.execute(
            "SELECT photo_date, notes FROM garden_photos WHERE entry_id = ? ORDER BY photo_date",
            (entry_id,),
        ).fetchall()
        growth_notes_list = [(r["photo_date"], r["notes"]) for r in growth_notes]
        user_location = g.user.get("location") if g.user else None
        last_fertilized = {"date": today, "type": fertilizer_type}
        result = _suggest_next_fertilization(db, entry, user_location, last_fertilized, growth_notes_list)
        next_date = result["date"] if result else None
        next_note = result["note"] if result else None
        return jsonify({"ok": True, "today": today, "next_date": next_date, "next_note": next_note})

    @app.route("/watering/mark-all-today", methods=["POST"])
    @login_required
    def watering_mark_all_today():
        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)
        today = _local_today()
        items = request.form.getlist("item")
        for item in items:
            try:
                kind, raw_id = item.split(":", 1)
                eid = int(raw_id)
            except (ValueError, AttributeError):
                continue
            if kind == "edible":
                row = db.execute(
                    f"SELECT id, watering_frequency_days FROM garden_entries WHERE id = ? AND user_id IN {ph}",
                    [eid] + id_args,
                ).fetchone()
                if row:
                    freq = row["watering_frequency_days"]
                    next_date = (datetime.fromisoformat(today).date() + _tdw(days=freq)).isoformat() if freq else None
                    db.execute(
                        "UPDATE garden_entries SET last_watered_date = ?, next_watering_date = ? WHERE id = ?",
                        (today, next_date, eid),
                    )
            elif kind == "ornamental":
                row = db.execute(
                    "SELECT id, watering_frequency_days FROM plants WHERE id = ? AND user_id = ?",
                    (eid, user_id),
                ).fetchone()
                if row:
                    freq = row["watering_frequency_days"]
                    next_date = (datetime.fromisoformat(today).date() + _tdw(days=freq)).isoformat() if freq else None
                    db.execute(
                        "UPDATE plants SET last_watered_date = ?, next_watering_date = ? WHERE id = ?",
                        (today, next_date, eid),
                    )
        db.commit()
        return redirect(url_for("dashboard"))

    @app.route("/garden/<int:entry_id>/duplicate", methods=["POST"])
    @login_required
    def garden_duplicate(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        src = db.execute(
            f"SELECT * FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if src is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        now = datetime.utcnow().isoformat(timespec="seconds")
        row = db.execute(
            """INSERT INTO garden_entries
               (user_id, plant_name, variety, location_type, location_name, planted_date, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                g.user["id"],
                src["plant_name"],
                src["variety"],
                src["location_type"],
                src["location_name"],
                src["planted_date"],
                src["notes"],
                now,
                now,
            ),
        ).fetchone()
        db.commit()
        return redirect(url_for("garden_edit", entry_id=row["id"]))

    @app.route("/garden/<int:entry_id>/assign-zone", methods=["POST"])
    @login_required
    def garden_assign_zone(entry_id):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        entry = db.execute(
            f"SELECT id FROM garden_entries WHERE id = ? AND user_id IN {ph}",
            [entry_id] + id_args,
        ).fetchone()
        if entry is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        zone_id = request.form.get("zone_id", type=int)
        if zone_id:
            zone = db.execute(
                "SELECT id FROM yard_zones WHERE id = ? AND user_id = ?",
                (zone_id, g.user["id"]),
            ).fetchone()
            if zone is None:
                flash("Zone not found.")
                return redirect(url_for("garden_index"))
        db.execute("UPDATE garden_entries SET zone_id = ? WHERE id = ?", (zone_id, entry_id))
        db.commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(ok=True)
        return_zone = request.form.get("return_zone_id", type=int) or zone_id
        if return_zone:
            return redirect(url_for("yard_zone_detail", zone_id=return_zone))
        return redirect(url_for("garden_index"))

    @app.route("/api/plant-chat", methods=["POST"])
    @login_required
    def plant_chat():
        try:
            import anthropic as _anthropic
        except ImportError:
            return jsonify(error="Chat assistant not available."), 503

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return jsonify(error="Chat assistant not configured."), 503

        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        plant_id = data.get("plant_id")
        history = (data.get("history") or [])[-10:]

        if not message or not plant_id:
            return jsonify(error="Message and plant_id are required."), 400

        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)

        plant = db.execute(
            f"SELECT * FROM plants WHERE id = ? AND user_id IN {ph}",
            (plant_id, *id_args),
        ).fetchone()
        if plant is None:
            return jsonify(error="Plant not found."), 404

        allowed, reason = _check_api_rate(db, user_id, "chat")
        if not allowed:
            return jsonify(error=reason), 429

        _log_activity(db, user_id, "plant_chat", message[:200])
        db.commit()

        today = _local_today()
        user_location = (g.user.get("location") or "").strip()

        # Build plant context
        lines = [f"Plant: {plant['name']}"]
        if plant["scientific_name"]: lines.append(f"Scientific name: {plant['scientific_name']}")
        if plant["description"]:     lines.append(f"Description: {plant['description']}")
        if plant["plant_form"]:      lines.append(f"Form: {plant['plant_form']}")
        if plant["height_category"]: lines.append(f"Height: {plant['height_category']}")
        if plant["size_info"]:       lines.append(f"Size: {plant['size_info']}")
        if plant["flowering_schedule"]: lines.append(f"Flowering: {plant['flowering_schedule']}")
        if plant["sun_exposure"]:    lines.append(f"Sun: {plant['sun_exposure']}")
        if plant["lifecycle"]:       lines.append(f"Lifecycle: {plant['lifecycle']}")
        if plant["evergreen_status"]: lines.append(f"Evergreen status: {plant['evergreen_status']}")
        if plant["pnw_native"]:      lines.append("Native to Pacific Northwest: yes")

        # Zone placements and yard notes
        pz_rows = db.execute(
            f"""SELECT yp.id, z.name AS zone_name
                FROM yard_plants yp JOIN yard_zones z ON z.id = yp.zone_id
                WHERE yp.user_id IN {ph} AND lower(yp.plant_name) = lower(?)
                ORDER BY z.name ASC""",
            (*id_args, plant["name"]),
        ).fetchall()
        if pz_rows:
            pz_ids = [r["id"] for r in pz_rows]
            ph2, pz_id_args = _in_ids(pz_ids)
            note_rows = db.execute(
                f"SELECT yard_plant_id, note_date, notes FROM yard_plant_notes"
                f" WHERE yard_plant_id IN {ph2} ORDER BY note_date ASC NULLS LAST, created_at ASC",
                pz_id_args,
            ).fetchall()
            notes_by_pz = {}
            for n in note_rows:
                notes_by_pz.setdefault(n["yard_plant_id"], []).append(n)
            for r in pz_rows:
                lines.append(f"Yard zone: {r['zone_name']}")
                for n in notes_by_pz.get(r["id"], []):
                    date_part = f" ({n['note_date']})" if n["note_date"] else ""
                    lines.append(f"  Note{date_part}: {n['notes']}")

        plant_context = "\n".join(lines)

        note_zone = pz_rows[0]["zone_name"] if pz_rows else None
        system = (
            f"You are a knowledgeable gardening assistant helping with a specific ornamental plant. Today is {today}.\n"
            + (f"The user's location: {user_location}\n" if user_location else "")
            + f"\nPlant details and notes:\n{plant_context}\n\n"
            "Answer questions about this plant — care, pruning, pests, companion planting, seasonal needs, etc. "
            "Use the notes and location to give specific, relevant advice. "
            "Keep replies concise and practical. "
            "Never use markdown bold (**text**) or other markdown formatting in your replies."
            + (f" When the user wants to save an observation or care note, use the save_note tool (saves to zone: {note_zone})." if note_zone else "")
        )

        tools = []
        if pz_rows:
            tools = [
                {
                    "name": "save_note",
                    "description": "Save an observation, care note, or milestone about this plant. Call this when the user wants to record something they've done or noticed.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "note_text": {"type": "string", "description": "The text of the note to save"},
                            "note_date": {"type": "string", "description": "Date in YYYY-MM-DD format. Defaults to today if omitted."},
                        },
                        "required": ["note_text"],
                    },
                }
            ]

        messages = [{"role": m["role"], "content": m["content"]} for m in history if m.get("role") in ("user", "assistant")]
        messages.append({"role": "user", "content": message})

        note_saved = None
        client = _anthropic.Anthropic(api_key=api_key)

        try:
            for _ in range(4):
                create_kwargs = dict(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    system=system,
                    messages=messages,
                )
                if tools:
                    create_kwargs["tools"] = tools
                resp = client.messages.create(**create_kwargs)
                messages.append({"role": "assistant", "content": resp.content})

                if resp.stop_reason != "tool_use":
                    reply = next((b.text for b in resp.content if hasattr(b, "text")), "").strip()
                    _log_ai_chat(db, user_id, g.user["username"], "ornamental", message, plant_name=plant["name"])
                    result = {"reply": reply}
                    if note_saved:
                        result["note_saved"] = note_saved
                    return jsonify(result)

                tool_results = []
                for block in resp.content:
                    if not hasattr(block, "type") or block.type != "tool_use":
                        continue
                    inp = block.input
                    if block.name == "save_note" and pz_rows:
                        note_text = (inp.get("note_text") or "").strip()
                        raw_date = (inp.get("note_date") or today).strip()
                        note_date = raw_date if _ISO_DATE_RE.match(raw_date) else today
                        if note_text:
                            db.execute(
                                "INSERT INTO yard_plant_notes (yard_plant_id, user_id, note_date, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                                (pz_rows[0]["id"], g.user["id"], note_date, note_text, datetime.utcnow().isoformat(timespec="seconds")),
                            )
                            db.commit()
                            note_saved = {"text": note_text, "date": note_date, "zone": pz_rows[0]["zone_name"]}
                            tool_result = "Note saved."
                        else:
                            tool_result = "Error: note text was empty."
                    else:
                        tool_result = "Unknown tool."
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": tool_result})

                messages.append({"role": "user", "content": tool_results})

            _log_ai_chat(db, user_id, g.user["username"], "ornamental", message, plant_name=plant["name"])
            result = {"reply": "Done."}
            if note_saved:
                result["note_saved"] = note_saved
            return jsonify(result)

        except Exception as exc:
            try:
                db.execute(
                    "INSERT INTO chat_error_log (user_id, error_type, error_message, logged_at) VALUES (?, ?, ?, ?)",
                    (user_id, type(exc).__name__, str(exc)[:500], datetime.utcnow().isoformat(timespec="seconds")),
                )
                db.commit()
            except Exception:
                pass
            return jsonify(error="Assistant unavailable — please try again."), 503

    @app.route("/api/entry-chat", methods=["POST"])
    @login_required
    def entry_chat():
        try:
            import anthropic as _anthropic
        except ImportError:
            return jsonify(error="Chat assistant not available."), 503
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return jsonify(error="Chat assistant not configured."), 503

        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        entry_id = data.get("entry_id")
        history = (data.get("history") or [])[-10:]
        if not message or not entry_id:
            return jsonify(error="message and entry_id are required."), 400

        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)

        entry = db.execute(
            f"SELECT ge.*, yz.name AS zone_name"
            f" FROM garden_entries ge"
            f" LEFT JOIN yard_zones yz ON yz.id = ge.zone_id"
            f" WHERE ge.id = ? AND ge.user_id IN {ph}",
            (entry_id, *id_args),
        ).fetchone()
        if entry is None:
            return jsonify(error="Entry not found."), 404

        allowed, reason = _check_api_rate(db, user_id, "chat")
        if not allowed:
            return jsonify(error=reason), 429

        _log_activity(db, user_id, "garden_chat", message[:200])
        db.commit()

        today = _local_today()
        user_location = (g.user.get("location") or "").strip()

        # Build entry context
        lines = [f"Plant: {entry['plant_name']}"]
        if entry["variety"]:       lines.append(f"Variety: {entry['variety']}")
        if entry["location_type"]: lines.append(f"Location type: {entry['location_type'].replace('_', ' ')}")
        if entry["location_name"]: lines.append(f"Location name: {entry['location_name']}")
        if entry["zone_name"]:     lines.append(f"Yard zone: {entry['zone_name']}")
        if entry["planted_date"]:  lines.append(f"Planted: {entry['planted_date']}")
        if entry["notes"]:         lines.append(f"Entry notes: {entry['notes']}")

        # Last fertilization
        last_fert_photo = db.execute(
            "SELECT COALESCE(fertilization_date, photo_date) AS fert_date, fertilizer_type"
            " FROM garden_photos WHERE entry_id = ? AND is_fertilization = 1"
            " ORDER BY COALESCE(fertilization_date, photo_date) DESC NULLS LAST, created_at DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        explicit_date = (entry["last_fertilized_date"] or "") if entry["last_fertilized_date"] else ""
        photo_fert_date = (last_fert_photo["fert_date"] or "") if last_fert_photo else ""
        if explicit_date and (not photo_fert_date or explicit_date >= photo_fert_date):
            fert_str = explicit_date
            fert_type = entry["last_fertilizer_type"]
        elif last_fert_photo:
            fert_str = photo_fert_date or None
            fert_type = last_fert_photo["fertilizer_type"]
        else:
            fert_str = None
            fert_type = None
        if fert_str:
            ft = f" ({fert_type})" if fert_type else ""
            lines.append(f"Last fertilized: {fert_str}{ft}")

        # Growth log notes
        note_rows = db.execute(
            "SELECT photo_date, notes FROM garden_photos"
            " WHERE entry_id = ? AND notes IS NOT NULL"
            " ORDER BY photo_date ASC NULLS LAST, created_at ASC",
            (entry_id,),
        ).fetchall()
        if note_rows:
            lines.append("Growth log:")
            for r in note_rows:
                date_part = f" ({r['photo_date']})" if r["photo_date"] else ""
                lines.append(f"  Log{date_part}: {r['notes']}")

        entry_context = "\n".join(lines)

        system = (
            f"You are a knowledgeable gardening assistant helping with a specific edible plant. Today is {today}.\n"
            + (f"The user's location: {user_location}\n" if user_location else "")
            + f"\nPlant details:\n{entry_context}\n\n"
            "Answer questions about this plant — care, watering, fertilizing, pests, harvesting, companion planting, seasonal needs, etc. "
            "Use the growth log notes and location to give specific, relevant advice. "
            "Keep replies concise and practical. "
            "When the user wants to record an observation or care note, use the save_note tool. "
            "Never use markdown bold (**text**) or other markdown formatting. "
            "Always format dates as 'Month Day' (e.g. 'May 17'), never as YYYY-MM-DD."
        )

        tools = [
            {
                "name": "save_note",
                "description": "Save an observation, care note, or milestone to this plant's growth log.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "note_text": {"type": "string", "description": "The note to save"},
                        "note_date": {"type": "string", "description": "Date in YYYY-MM-DD format, defaults to today"},
                    },
                    "required": ["note_text"],
                },
            }
        ]

        messages = [{"role": m["role"], "content": m["content"]} for m in history if m.get("role") in ("user", "assistant")]
        messages.append({"role": "user", "content": message})

        note_saved = None
        client = _anthropic.Anthropic(api_key=api_key)
        try:
            for _ in range(4):
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": resp.content})

                if resp.stop_reason != "tool_use":
                    reply = next((b.text for b in resp.content if hasattr(b, "text")), "").strip()
                    _log_ai_chat(db, user_id, g.user["username"], "edible", message, plant_name=entry["plant_name"])
                    result = {"reply": reply}
                    if note_saved:
                        result["note_saved"] = note_saved
                    return jsonify(result)

                tool_results = []
                for block in resp.content:
                    if not hasattr(block, "type") or block.type != "tool_use":
                        continue
                    inp = block.input
                    if block.name == "save_note":
                        note_text = (inp.get("note_text") or "").strip()
                        raw_date = (inp.get("note_date") or today).strip()
                        note_date = raw_date if _ISO_DATE_RE.match(raw_date) else today
                        if note_text and entry["user_id"] == user_id:
                            planted_date = entry["planted_date"]
                            is_fert, fert_type_det, fert_date_det = _detect_fertilization(note_text)
                            db.execute(
                                "INSERT INTO garden_photos"
                                " (entry_id, user_id, image_path, photo_date, notes, created_at, is_fertilization, fertilizer_type, fertilization_date)"
                                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                                (entry_id, user_id, note_date, note_text,
                                 datetime.utcnow().isoformat(timespec="seconds"),
                                 is_fert, fert_type_det, fert_date_det),
                            )
                            db.commit()
                            note_saved = {"text": note_text, "date": note_date}
                            tool_result = "Note saved."
                        else:
                            tool_result = "Error: note text was empty or entry is read-only."
                    else:
                        tool_result = "Unknown tool."
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": tool_result})

                messages.append({"role": "user", "content": tool_results})

            _log_ai_chat(db, user_id, g.user["username"], "edible", message, plant_name=entry["plant_name"])
            result = {"reply": "Done."}
            if note_saved:
                result["note_saved"] = note_saved
            return jsonify(result)

        except Exception as exc:
            _log_chat_error(db, user_id, g.user["username"], message, type(exc).__name__, str(exc)[:500])
            return jsonify(error="Assistant unavailable — please try again."), 503

    @app.route("/api/garden-chat", methods=["POST"])
    @login_required
    def garden_chat():
        try:
            import anthropic as _anthropic
        except ImportError:
            return jsonify(error="Chat assistant not available."), 503

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return jsonify(error="Chat assistant not configured."), 503

        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        history = (data.get("history") or [])[-10:]  # cap at last 10 messages

        if not message:
            return jsonify(error="Message is required."), 400

        db = get_db()
        user_id = g.user["id"]

        allowed, reason = _check_api_rate(db, user_id, "chat")
        if not allowed:
            return jsonify(error=reason), 429

        _log_activity(db, user_id, "garden_chat", message[:200])
        db.commit()
        today = _local_today()
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)

        entries = db.execute(
            f"""SELECT ge.id, ge.plant_name, ge.variety, ge.location_type, ge.location_name,
                       ge.planted_date, ge.notes, ge.user_id, ge.zone_id, yz.name AS zone_name
            FROM garden_entries ge
            LEFT JOIN yard_zones yz ON yz.id = ge.zone_id
            WHERE ge.user_id IN {ph}
            ORDER BY CASE WHEN ge.planted_date IS NULL THEN 1 ELSE 0 END, ge.planted_date DESC""",
            id_args,
        ).fetchall()

        zones = db.execute(
            f"SELECT id, name FROM yard_zones WHERE user_id IN {ph} ORDER BY name ASC",
            id_args,
        ).fetchall()

        # Fetch all growth-log notes grouped by entry
        growth_notes: dict = {}
        if entries:
            eids = [e["id"] for e in entries]
            eid_ph = "(" + ",".join(["?"] * len(eids)) + ")"
            for row in db.execute(
                f"SELECT entry_id, photo_date, notes FROM garden_photos"
                f" WHERE entry_id IN {eid_ph} AND notes IS NOT NULL"
                f" ORDER BY photo_date ASC NULLS LAST, created_at ASC",
                eids,
            ).fetchall():
                growth_notes.setdefault(row["entry_id"], []).append(
                    (row["photo_date"], row["notes"])
                )

        if entries:
            lines = []
            for e in entries:
                ln = f"  ID {e['id']}: {e['plant_name']}"
                if e["variety"]: ln += f" ({e['variety']})"
                if e["location_type"]: ln += f" [{e['location_type'].replace('_', ' ')}]"
                if e["location_name"]: ln += f" in {e['location_name']}"
                if e["planted_date"]: ln += f", planted {e['planted_date']}"
                if e["zone_name"]: ln += f" [zone: {e['zone_name']}]"
                if e["user_id"] != user_id: ln += " [partner's — read only]"
                if e["notes"]: ln += f"\n    Notes: {e['notes']}"
                for note_date, note_text in growth_notes.get(e["id"], []):
                    date_part = f" ({note_date})" if note_date else ""
                    ln += f"\n    Log{date_part}: {note_text}"
                lines.append(ln)
            entries_text = "\n".join(lines)
        else:
            entries_text = "  (no entries yet)"

        zones_text = "\n".join(f"  - {z['name']} (ID {z['id']})" for z in zones) or "  (no zones yet)"
        system = (
            f"You are a concise assistant for an edible garden tracker. Today is {today}.\n\n"
            f"Current garden entries (includes notes, log entries, location types, dates, and yard zone):\n{entries_text}\n\n"
            f"Available yard zones:\n{zones_text}\n\n"
            "Answer questions using all the information provided above — notes and log entries contain details like "
            "soil amendments, fertilizers, observations, and other specifics the user has recorded. "
            "If the answer is in the notes or logs, use it. Only say you don't know something if it genuinely "
            "isn't recorded anywhere in the data above. "
            "Help the user add plants, add notes, update details, assign or change yard zones, or answer questions about their garden. "
            "Entries marked [partner's — read only] belong to a shared garden partner — answer questions about them but do NOT call any tool on them. "
            "For bulk changes call the relevant tool once per entry. "
            "If a tool returns an error, skip that entry silently and continue with the remaining ones — do NOT apologise or stop early. "
            "Only mention a failure at the end if every single attempt failed. "
            "Keep replies short — just confirm what was done. "
            "If a plant name is ambiguous (multiple matches), list the options and ask which one. "
            "Never mention entry IDs or zone IDs to the user — they are internal only. "
            "Always format dates for the user as 'Month Day' (e.g. 'May 17', never '2026-05-17'). "
            "Never use markdown bold (**text**) or other markdown in your replies."
        )

        tools = [
            {
                "name": "add_garden_entry",
                "description": "Add a new plant to the user's edible garden.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "plant_name": {"type": "string"},
                        "variety": {"type": "string"},
                        "location_type": {"type": "string", "enum": ["raised_bed", "in_ground", "container", "greenhouse", "grow_bag", "other"]},
                        "location_name": {"type": "string", "description": "e.g. 'Raised bed 1'"},
                        "planted_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "notes": {"type": "string"},
                        "zone_name": {"type": "string", "description": "Name of an existing yard zone to assign this plant to"},
                    },
                    "required": ["plant_name"],
                },
            },
            {
                "name": "add_garden_note",
                "description": "Add a text note or observation to an existing garden entry.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "note": {"type": "string"},
                        "note_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                    },
                    "required": ["entry_id", "note"],
                },
            },
            {
                "name": "update_garden_entry",
                "description": "Update fields of an existing garden entry (variety, location, planted date, etc.).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "plant_name": {"type": "string"},
                        "variety": {"type": "string"},
                        "location_type": {"type": "string", "enum": ["raised_bed", "in_ground", "container", "greenhouse", "grow_bag", "other"]},
                        "location_name": {"type": "string"},
                        "planted_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "notes": {"type": "string"},
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "set_entry_zone",
                "description": "Assign or change the yard zone for an existing garden entry. Use zone_name to identify the zone, or omit it to remove the zone assignment.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "zone_name": {"type": "string", "description": "Name of the yard zone to assign. Omit or pass null to clear the zone."},
                    },
                    "required": ["entry_id"],
                },
            },
        ]

        messages = list(history) + [{"role": "user", "content": message}]
        changed = False
        client = _anthropic.Anthropic(api_key=api_key)

        try:
            for _ in range(10):
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    text = next((b.text for b in response.content if hasattr(b, "text")), "Done.")
                    _log_ai_chat(db, user_id, g.user["username"], "edible", message)
                    return jsonify(reply=text, changed=changed)

                # Execute tool calls
                tool_results = []
                for block in response.content:
                    if not hasattr(block, "type") or block.type != "tool_use":
                        continue
                    inp = block.input
                    result = {}
                    try:
                        if block.name == "add_garden_entry":
                            pname = (inp.get("plant_name") or "").strip()
                            if not pname:
                                result = {"error": "plant_name is required"}
                            else:
                                zone_id_val = None
                                zname = (inp.get("zone_name") or "").strip()
                                if zname:
                                    zrow = db.execute(
                                        f"SELECT id FROM yard_zones WHERE user_id IN {ph} AND lower(name) = lower(?)",
                                        (*id_args, zname),
                                    ).fetchone()
                                    if zrow:
                                        zone_id_val = zrow["id"]
                                now = datetime.utcnow().isoformat(timespec="seconds")
                                row = db.execute(
                                    """INSERT INTO garden_entries
                                    (user_id, plant_name, variety, location_type, location_name, planted_date, notes, zone_id, created_at, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                                    (
                                        user_id, pname,
                                        (inp.get("variety") or "").strip() or None,
                                        (inp.get("location_type") or "").strip() or None,
                                        (inp.get("location_name") or "").strip() or None,
                                        _parse_date_to_iso((inp.get("planted_date") or "").strip(), today),
                                        (inp.get("notes") or "").strip() or None,
                                        zone_id_val,
                                        now, now,
                                    ),
                                ).fetchone()
                                _log_activity(db, user_id, "garden_entry_added", pname)
                                db.commit()
                                changed = True
                                result = {"ok": True, "entry_id": row["id"]}

                        elif block.name == "add_garden_note":
                            eid = inp.get("entry_id")
                            note = (inp.get("note") or "").strip()
                            if not eid or not note:
                                result = {"error": "entry_id and note are required"}
                            else:
                                entry = db.execute(
                                    "SELECT id, user_id, planted_date FROM garden_entries WHERE id = ?",
                                    (eid,),
                                ).fetchone()
                                if not entry:
                                    result = {"error": f"Entry {eid} not found — skip it and continue with others"}
                                elif entry["user_id"] != user_id:
                                    result = {"error": f"Entry {eid} belongs to your garden partner and is read-only — skip it and continue with the user's own entries"}
                                else:
                                    note_date = _parse_date_to_iso(inp.get("note_date") or today, today)
                                    planted_date = entry["planted_date"]
                                    is_fert, fert_type, fert_date = _detect_fertilization(note)
                                    if is_fert and _has_date_hint(note):
                                        fert_date = _extract_fertilization_date(note, note_date, planted_date)
                                    elif not is_fert:
                                        is_fert, fert_type, fert_date = _ai_detect_fertilization(note, note_date, planted_date)
                                    db.execute(
                                        """INSERT INTO garden_photos
                                        (entry_id, user_id, image_path, photo_date, notes, created_at, is_fertilization, fertilizer_type, fertilization_date)
                                        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                                        (eid, user_id, note_date, note,
                                         datetime.utcnow().isoformat(timespec="seconds"),
                                         is_fert, fert_type, fert_date),
                                    )
                                    db.commit()
                                    changed = True
                                    result = {"ok": True}

                        elif block.name == "update_garden_entry":
                            eid = inp.get("entry_id")
                            if not eid:
                                result = {"error": "entry_id is required"}
                            else:
                                entry = db.execute(
                                    "SELECT id, user_id FROM garden_entries WHERE id = ?",
                                    (eid,),
                                ).fetchone()
                                if not entry:
                                    result = {"error": f"Entry {eid} not found — skip it and continue with others"}
                                elif entry["user_id"] != user_id:
                                    result = {"error": f"Entry {eid} belongs to your garden partner and is read-only — skip it and continue with the user's own entries"}
                                else:
                                    fields = ["plant_name", "variety", "location_type", "location_name", "planted_date", "notes"]
                                    sets, params = [], []
                                    for f in fields:
                                        if f in inp:
                                            val = (inp[f] or "").strip() or None
                                            if f == "planted_date" and val:
                                                val = _parse_date_to_iso(val, today)
                                            sets.append(f"{f} = ?")
                                            params.append(val)
                                    if sets:
                                        sets.append("updated_at = ?")
                                        params.append(datetime.utcnow().isoformat(timespec="seconds"))
                                        params += [eid, user_id]
                                        db.execute(
                                            f"UPDATE garden_entries SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                                            params,
                                        )
                                        db.commit()
                                        changed = True
                                        result = {"ok": True, "updated": [f for f in fields if f in inp]}
                                    else:
                                        result = {"error": "No fields to update"}
                        elif block.name == "set_entry_zone":
                            eid = inp.get("entry_id")
                            if not eid:
                                result = {"error": "entry_id is required"}
                            else:
                                entry = db.execute(
                                    "SELECT id, user_id FROM garden_entries WHERE id = ?", (eid,)
                                ).fetchone()
                                if not entry:
                                    result = {"error": f"Entry {eid} not found"}
                                elif entry["user_id"] != user_id:
                                    result = {"error": f"Entry {eid} belongs to your garden partner and is read-only"}
                                else:
                                    zname = (inp.get("zone_name") or "").strip()
                                    zone_id_val = None
                                    if zname:
                                        zrow = db.execute(
                                            f"SELECT id FROM yard_zones WHERE user_id IN {ph} AND lower(name) = lower(?)",
                                            (*id_args, zname),
                                        ).fetchone()
                                        if not zrow:
                                            result = {"error": f"Zone '{zname}' not found — available zones: {', '.join(z['name'] for z in zones)}"}
                                        else:
                                            zone_id_val = zrow["id"]
                                    if zone_id_val is not None or not zname:
                                        db.execute(
                                            "UPDATE garden_entries SET zone_id = ?, updated_at = ? WHERE id = ?",
                                            (zone_id_val, datetime.utcnow().isoformat(timespec="seconds"), eid),
                                        )
                                        db.commit()
                                        changed = True
                                        result = {"ok": True, "zone_id": zone_id_val}

                        else:
                            result = {"error": f"Unknown tool: {block.name}"}
                    except Exception as exc:
                        result = {"error": str(exc)}

                    if "error" in result:
                        _log_chat_error(db, user_id, g.user["username"], message,
                                        f"tool_{block.name}", result["error"])

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
                messages.append({"role": "user", "content": tool_results})

            return jsonify(reply="Something went wrong — please try again.", changed=False)

        except _anthropic.AuthenticationError as exc:
            _log_chat_error(db, user_id, g.user["username"], message, "billing_auth", str(exc))
            return jsonify(reply="The garden assistant is currently unavailable.", changed=False), 503
        except _anthropic.RateLimitError as exc:
            _log_chat_error(db, user_id, g.user["username"], message, "rate_limit", str(exc))
            return jsonify(reply="The assistant is busy right now — please try again in a moment.", changed=False), 429
        except _anthropic.APIStatusError as exc:
            if exc.status_code == 402:
                _log_chat_error(db, user_id, g.user["username"], message, "billing_credits", str(exc))
                return jsonify(reply="The garden assistant is currently unavailable.", changed=False), 503
            if exc.status_code in (529, 503):
                _log_chat_error(db, user_id, g.user["username"], message, "overloaded", str(exc))
                return jsonify(reply="The assistant is temporarily overloaded — please try again in a moment.", changed=False), 503
            _log_chat_error(db, user_id, g.user["username"], message, f"api_{exc.status_code}", str(exc))
            return jsonify(reply="The assistant encountered an error — please try again.", changed=False), 502
        except Exception as exc:
            _log_chat_error(db, user_id, g.user["username"], message, "unknown", str(exc))
            return jsonify(reply="Something went wrong — please try again.", changed=False), 500

    # ── Garden API (token-authenticated) ────────────────────────────────────

    def token_required(view):
        @wraps(view)
        def wrapped(**kwargs):
            db = get_db()
            auth = request.headers.get("Authorization", "").strip()
            token = auth[7:] if auth.startswith("Bearer ") else auth
            if not token:
                return jsonify({"error": "missing token"}), 401
            user = db.execute("SELECT * FROM users WHERE api_token = ?", (token,)).fetchone()
            if user is None:
                return jsonify({"error": "invalid token"}), 401
            g.user = user
            return view(**kwargs)
        return wrapped

    @app.route("/api/garden/entries", methods=["GET"])
    @token_required
    def api_garden_list():
        db = get_db()
        entries = db.execute(
            "SELECT * FROM garden_entries WHERE user_id = ? ORDER BY planted_date DESC NULLS LAST, created_at DESC",
            (g.user["id"],),
        ).fetchall()
        return jsonify({"entries": [dict(e) for e in entries]})

    @app.route("/api/garden/entries", methods=["POST"])
    @token_required
    def api_garden_create():
        db = get_db()
        data = request.get_json(force=True) or {}
        plant_name = (data.get("plant_name") or "").strip()
        if not plant_name:
            return jsonify({"error": "plant_name is required"}), 400
        now = datetime.utcnow().isoformat(timespec="seconds")
        entry_id = db.execute(
            """INSERT INTO garden_entries
               (user_id, plant_name, variety, location_type, location_name, planted_date, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                g.user["id"],
                plant_name,
                (data.get("variety") or "").strip() or None,
                (data.get("location_type") or "").strip() or None,
                (data.get("location_name") or "").strip() or None,
                (data.get("planted_date") or "").strip() or None,
                (data.get("notes") or "").strip() or None,
                now, now,
            ),
        ).fetchone()["id"]
        db.commit()
        entry = db.execute("SELECT * FROM garden_entries WHERE id = ?", (entry_id,)).fetchone()
        return jsonify({"entry": dict(entry)}), 201

    @app.route("/api/garden/entries/<int:entry_id>", methods=["PATCH"])
    @token_required
    def api_garden_update(entry_id):
        db = get_db()
        entry = db.execute(
            "SELECT * FROM garden_entries WHERE id = ? AND user_id = ?",
            (entry_id, g.user["id"]),
        ).fetchone()
        if entry is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True) or {}
        allowed = {"plant_name", "variety", "location_type", "location_name", "planted_date", "notes"}
        updates = {k: v for k, v in data.items() if k in allowed and v is not None}
        if not updates:
            return jsonify({"error": "no valid fields"}), 400
        updates["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE garden_entries SET {set_clause} WHERE id = ?", [*updates.values(), entry_id])
        db.commit()
        entry = db.execute("SELECT * FROM garden_entries WHERE id = ?", (entry_id,)).fetchone()
        return jsonify({"entry": dict(entry)})

    @app.route("/api/plant-search")
    @login_required
    def api_plant_search():
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify(results=[])

        # Library matching is done client-side (data already in page).
        # This endpoint only handles external API lookup.

        # ── Perenual (if API key configured) ──
        api_key = os.environ.get("PERENUAL_API_KEY", "").strip()
        if api_key:
            try:
                resp = requests.get(
                    "https://perenual.com/api/species-list",
                    params={"key": api_key, "q": q},
                    timeout=8,
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                results = []
                for plant in data[:12]:
                    sci_list = plant.get("scientific_name") or []
                    img = plant.get("default_image") or {}
                    photo_url = (img.get("medium_url") or img.get("regular_url")
                                 or img.get("original_url") or None)
                    results.append({
                        "common_name": plant.get("common_name") or "",
                        "scientific_name": sci_list[0] if sci_list else "",
                        "rank": "species",
                        "from_library": False,
                        "photo_url": photo_url,
                    })
                try:
                    _db = get_db()
                    _db.execute(
                        "INSERT INTO perenual_log (query, result_count, logged_at, user_id) VALUES (?, ?, ?, ?)",
                        (q, len(results), datetime.utcnow().isoformat(timespec="seconds"), g.user["id"] if g.get("user") else None),
                    )
                    _db.commit()
                except Exception:
                    pass
                if results:
                    return jsonify(results=results)
            except Exception:
                pass

        # ── iNaturalist (free fallback) ──
        # Belt-and-suspenders: iconic_taxa param filters the request; this set
        # catches anything that slips through (animals, insects, birds, etc.)
        _PLANT_ICONIC_TAXA = {"Plantae", "Fungi", "Chromista", "Protozoa", ""}
        try:
            resp = requests.get(
                "https://api.inaturalist.org/v1/taxa",
                params={"q": q, "is_active": "true", "iconic_taxa": "Plantae", "per_page": 12},
                timeout=8,
            )
            resp.raise_for_status()
            taxa = [
                t for t in resp.json().get("results", [])
                if (t.get("iconic_taxon_name") or "") in _PLANT_ICONIC_TAXA
            ]
            results = []
            for t in taxa:
                photo = t.get("default_photo") or {}
                # Collect curated taxon photos (already in the response — free, no extra call)
                taxon_photos = []
                for tp in (t.get("taxon_photos") or [])[:6]:
                    p = (tp.get("photo") or {})
                    url = p.get("medium_url") or p.get("square_url") or ""
                    if url:
                        taxon_photos.append(url)
                # Fall back: use default_photo if taxon_photos is empty
                if not taxon_photos:
                    default_url = photo.get("medium_url") or photo.get("square_url")
                    if default_url:
                        taxon_photos = [default_url]
                results.append({
                    "common_name": t.get("preferred_common_name") or t.get("name") or "",
                    "scientific_name": t.get("name") or "",
                    "matched_term": t.get("matched_term") or "",
                    "photo_url": photo.get("medium_url") or photo.get("square_url"),
                    "taxon_photos": taxon_photos,
                    "taxon_id": t.get("id"),
                    "rank": t.get("rank") or "species",
                    "from_library": False,
                })
            if results:
                return jsonify(results=results)

            # iNaturalist found nothing — the query may be a regional/informal common name.
            # Ask Claude to resolve it to a scientific name and retry once.
            sci = resolve_scientific_name(q)
            if sci and sci.lower() != q.lower():
                resp2 = requests.get(
                    "https://api.inaturalist.org/v1/taxa",
                    params={"q": sci, "is_active": "true", "iconic_taxa": "Plantae", "per_page": 8},
                    timeout=8,
                )
                resp2.raise_for_status()
                for t in resp2.json().get("results", []):
                    if (t.get("iconic_taxon_name") or "") not in _PLANT_ICONIC_TAXA:
                        continue
                    photo = t.get("default_photo") or {}
                    taxon_photos = []
                    for tp in (t.get("taxon_photos") or [])[:6]:
                        p = (tp.get("photo") or {})
                        url = p.get("medium_url") or p.get("square_url") or ""
                        if url:
                            taxon_photos.append(url)
                    if not taxon_photos:
                        default_url = photo.get("medium_url") or photo.get("square_url")
                        if default_url:
                            taxon_photos = [default_url]
                    results.append({
                        "common_name": t.get("preferred_common_name") or t.get("name") or "",
                        "scientific_name": t.get("name") or "",
                        "matched_term": q,  # query was resolved via Claude to this taxon
                        "photo_url": photo.get("medium_url") or photo.get("square_url"),
                        "taxon_photos": taxon_photos,
                        "taxon_id": t.get("id"),
                        "rank": t.get("rank") or "species",
                        "from_library": False,
                    })
            return jsonify(results=results)
        except Exception:
            return jsonify(results=[])

    @app.route("/api/plant-photo")
    @login_required
    def api_plant_photo():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify(photo_url=None)
        return jsonify(photo_url=lookup_plant_image(q))

    @app.route("/api/plant-photos")
    @login_required
    def api_plant_photos():
        q = request.args.get("q", "").strip()
        count = min(int(request.args.get("count", "3")), 6)
        taxon_id_raw = request.args.get("taxon_id", "").strip()
        taxon_id = int(taxon_id_raw) if taxon_id_raw.isdigit() else None
        if not q and not taxon_id:
            return jsonify(photos=[])
        return jsonify(photos=lookup_plant_photos(q, count, taxon_id=taxon_id))

    @app.route("/api/plant-details")
    @login_required
    def api_plant_details():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify(error="No query provided"), 400
        result, error = lookup_plant_details(q, location=g.user.get("location") if g.user else None)
        if error:
            return jsonify(error=error), 200
        return jsonify(result)

    @app.route("/api/home-chat", methods=["POST"])
    @login_required
    def home_chat():
        try:
            import anthropic as _anthropic
        except ImportError:
            return jsonify(error="Chat assistant not available."), 503
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return jsonify(error="Chat assistant not configured."), 503

        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        history = (data.get("history") or [])[-14:]
        if not message:
            return jsonify(error="Message is required."), 400

        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)

        allowed, reason = _check_api_rate(db, user_id, "chat")
        if not allowed:
            return jsonify(error=reason), 429

        _log_activity(db, user_id, "home_chat", message[:200])
        db.commit()

        today = _local_today()
        user_location = (g.user.get("location") or "").strip()

        # ── Edibles ───────────────────────────────────────────────────────────
        entries = db.execute(
            f"""SELECT ge.id, ge.plant_name, ge.variety, ge.location_type, ge.location_name,
                       ge.planted_date, ge.notes, ge.user_id, ge.zone_id, yz.name AS zone_name
                FROM garden_entries ge
                LEFT JOIN yard_zones yz ON yz.id = ge.zone_id
                WHERE ge.user_id IN {ph}
                ORDER BY CASE WHEN ge.planted_date IS NULL THEN 1 ELSE 0 END, ge.planted_date DESC""",
            id_args,
        ).fetchall()
        growth_notes: dict = {}
        if entries:
            eids = [e["id"] for e in entries]
            eid_ph = "({})".format(",".join(["?"] * len(eids)))
            for row in db.execute(
                f"SELECT entry_id, photo_date, notes FROM garden_photos"
                f" WHERE entry_id IN {eid_ph} AND notes IS NOT NULL"
                f" ORDER BY photo_date ASC NULLS LAST, created_at ASC",
                eids,
            ).fetchall():
                growth_notes.setdefault(row["entry_id"], []).append((row["photo_date"], row["notes"]))

        if entries:
            edible_lines = []
            for e in entries:
                ln = f"  ID {e['id']}: {e['plant_name']}"
                if e["variety"]: ln += f" ({e['variety']})"
                if e["location_type"]: ln += f" [{e['location_type'].replace('_', ' ')}]"
                if e["location_name"]: ln += f" in {e['location_name']}"
                if e["planted_date"]: ln += f", planted {e['planted_date']}"
                if e["zone_name"]: ln += f" [zone: {e['zone_name']}]"
                if e["user_id"] != user_id: ln += " [partner's — read only]"
                if e["notes"]: ln += f"\n    Notes: {e['notes']}"
                for nd, nt in growth_notes.get(e["id"], []):
                    ln += f"\n    Log{' (' + nd + ')' if nd else ''}: {nt}"
                edible_lines.append(ln)
            edibles_text = "\n".join(edible_lines)
        else:
            edibles_text = "  (none yet)"

        # ── Zones ─────────────────────────────────────────────────────────────
        zones = db.execute(
            f"SELECT id, name FROM yard_zones WHERE user_id IN {ph} ORDER BY name ASC",
            id_args,
        ).fetchall()
        zones_text = "\n".join(f"  - {z['name']} (ID {z['id']})" for z in zones) or "  (none yet)"

        # ── Ornamentals (library + zone placements + notes) ───────────────────
        orn_plants = db.execute(
            f"SELECT * FROM plants WHERE user_id IN {ph} ORDER BY name ASC",
            id_args,
        ).fetchall()
        yard_placements = db.execute(
            f"""SELECT yp.id, yp.plant_name, z.name AS zone_name
                FROM yard_plants yp JOIN yard_zones z ON z.id = yp.zone_id
                WHERE yp.user_id IN {ph} ORDER BY yp.plant_name ASC""",
            id_args,
        ).fetchall()
        yp_notes: dict = {}
        if yard_placements:
            yp_ids = [r["id"] for r in yard_placements]
            yp_ph = "({})".format(",".join(["?"] * len(yp_ids)))
            for row in db.execute(
                f"SELECT yard_plant_id, note_date, notes FROM yard_plant_notes"
                f" WHERE yard_plant_id IN {yp_ph} ORDER BY note_date ASC NULLS LAST, created_at ASC",
                yp_ids,
            ).fetchall():
                yp_notes.setdefault(row["yard_plant_id"], []).append((row["note_date"], row["notes"]))

        placements_by_name: dict = {}
        for r in yard_placements:
            placements_by_name.setdefault(r["plant_name"].lower(), []).append(r)

        orn_lines = []
        for p in orn_plants:
            added = (p["created_at"] or "")[:10]
            ln = f"  {p['name']}"
            if added: ln += f" [added {added}]"
            if p["scientific_name"]: ln += f" ({p['scientific_name']})"
            if p["lifecycle"]: ln += f" [{p['lifecycle']}]"
            if p["sun_exposure"]: ln += f" [sun: {p['sun_exposure']}]"
            pls = placements_by_name.get(p["name"].lower(), [])
            if pls:
                for pl in pls:
                    ln += f"\n    Zone: {pl['zone_name']} (yard_plant_id={pl['id']})"
                    for nd, nt in yp_notes.get(pl["id"], []):
                        ln += f"\n      Note{' (' + nd + ')' if nd else ''}: {nt}"
            else:
                ln += " [library only — not planted in any zone]"
            orn_lines.append(ln)
        ornamentals_text = "\n".join(orn_lines) if orn_lines else "  (none yet)"

        suggestion = session.get("plant_suggestion") or {}
        suggestion_text = ""
        if suggestion.get("name"):
            suggestion_text = (
                f"\n=== SUGGESTED PLANT (shown to user this session) ===\n"
                f"  {suggestion['name']}"
                + (f" ({suggestion['scientific_name']})" if suggestion.get("scientific_name") else "")
                + (f": {suggestion['description']}" if suggestion.get("description") else "")
                + (f" Suggested because: {suggestion['why']}" if suggestion.get("why") else "")
                + "\n"
            )

        system = (
            f"You are a comprehensive garden assistant for GardenPal. Today is {today}.\n"
            + (f"The user's location: {user_location}\n" if user_location else "")
            + f"\n=== EDIBLES (tracked plants) ===\n{edibles_text}\n"
            + f"\n=== YARD ZONES ===\n{zones_text}\n"
            + f"\n=== ORNAMENTALS (library + yard placements) ===\n{ornamentals_text}\n"
            + suggestion_text + "\n"
            "You can answer questions about any plant across both edibles and ornamentals. "
            "You can also act: add notes, update edible entries, change zones, save ornamental notes. "
            "Entries marked [partner's — read only] are read-only. "
            "IMPORTANT: If the user refers to a plant by name but there are multiple entries with that name "
            "(e.g. two spinach entries in different zones), always list the options and ask which one they mean "
            "BEFORE taking any action. Never guess — ambiguity must be resolved first. "
            "For bulk changes, call the relevant tool once per item. "
            "If a tool returns an error, skip and continue — only mention if everything failed. "
            "Keep replies short. Don't mention internal IDs to the user. "
            "Always format dates as 'Month Day' (e.g. 'May 17'). "
            "Never use markdown bold (**text**) or other markdown in your replies."
        )

        tools = [
            {
                "name": "add_garden_entry",
                "description": "Add a new plant to the user's edible garden.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "plant_name": {"type": "string"},
                        "variety": {"type": "string"},
                        "location_type": {"type": "string", "enum": ["raised_bed", "in_ground", "container", "greenhouse", "grow_bag", "other"]},
                        "location_name": {"type": "string"},
                        "planted_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "notes": {"type": "string"},
                        "zone_name": {"type": "string", "description": "Name of an existing yard zone"},
                    },
                    "required": ["plant_name"],
                },
            },
            {
                "name": "add_garden_note",
                "description": "Add a text note or observation to an existing edible garden entry.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "note": {"type": "string"},
                        "note_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                    },
                    "required": ["entry_id", "note"],
                },
            },
            {
                "name": "update_garden_entry",
                "description": "Update fields of an existing edible garden entry.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "plant_name": {"type": "string"},
                        "variety": {"type": "string"},
                        "location_type": {"type": "string", "enum": ["raised_bed", "in_ground", "container", "greenhouse", "grow_bag", "other"]},
                        "location_name": {"type": "string"},
                        "planted_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "notes": {"type": "string"},
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "set_entry_zone",
                "description": "Assign or change the yard zone for an edible garden entry.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "zone_name": {"type": "string", "description": "Zone name, or omit to clear."},
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "save_ornamental_note",
                "description": "Save an observation or care note about an ornamental plant in a yard zone. Use yard_plant_id from the plant's zone placement.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "yard_plant_id": {"type": "integer", "description": "The yard_plant_id from the ornamental's zone placement"},
                        "note_text": {"type": "string"},
                        "note_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                    },
                    "required": ["yard_plant_id", "note_text"],
                },
            },
        ]

        messages = list(history) + [{"role": "user", "content": message}]
        changed = False
        client = _anthropic.Anthropic(api_key=api_key)

        try:
            for _ in range(10):
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    text = next((b.text for b in response.content if hasattr(b, "text")), "Done.")
                    _log_ai_chat(db, user_id, g.user["username"], "home", message)
                    return jsonify(reply=text, changed=changed)

                tool_results = []
                for block in response.content:
                    if not hasattr(block, "type") or block.type != "tool_use":
                        continue
                    inp = block.input
                    result = {}
                    try:
                        if block.name == "add_garden_entry":
                            pname = (inp.get("plant_name") or "").strip()
                            if not pname:
                                result = {"error": "plant_name is required"}
                            else:
                                zone_id_val = None
                                zname = (inp.get("zone_name") or "").strip()
                                if zname:
                                    zrow = db.execute(
                                        f"SELECT id FROM yard_zones WHERE user_id IN {ph} AND lower(name) = lower(?)",
                                        (*id_args, zname),
                                    ).fetchone()
                                    if zrow:
                                        zone_id_val = zrow["id"]
                                now = datetime.utcnow().isoformat(timespec="seconds")
                                row = db.execute(
                                    "INSERT INTO garden_entries (user_id, plant_name, variety, location_type, location_name, planted_date, notes, zone_id, created_at, updated_at)"
                                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                                    (
                                        user_id, pname,
                                        (inp.get("variety") or "").strip() or None,
                                        (inp.get("location_type") or "").strip() or None,
                                        (inp.get("location_name") or "").strip() or None,
                                        _parse_date_to_iso((inp.get("planted_date") or "").strip(), today),
                                        (inp.get("notes") or "").strip() or None,
                                        zone_id_val, now, now,
                                    ),
                                ).fetchone()
                                _log_activity(db, user_id, "garden_entry_added", pname)
                                db.commit()
                                changed = True
                                result = {"ok": True, "entry_id": row["id"]}

                        elif block.name == "add_garden_note":
                            eid = inp.get("entry_id")
                            note = (inp.get("note") or "").strip()
                            if not eid or not note:
                                result = {"error": "entry_id and note are required"}
                            else:
                                entry = db.execute(
                                    "SELECT id, user_id, planted_date FROM garden_entries WHERE id = ?", (eid,)
                                ).fetchone()
                                if not entry:
                                    result = {"error": f"Entry {eid} not found"}
                                elif entry["user_id"] != user_id:
                                    result = {"error": f"Entry {eid} is read-only (partner's)"}
                                else:
                                    note_date = _parse_date_to_iso(inp.get("note_date") or today, today)
                                    planted_date = entry["planted_date"]
                                    is_fert, fert_type, fert_date = _detect_fertilization(note)
                                    if is_fert and _has_date_hint(note):
                                        fert_date = _extract_fertilization_date(note, note_date, planted_date)
                                    elif not is_fert:
                                        is_fert, fert_type, fert_date = _ai_detect_fertilization(note, note_date, planted_date)
                                    db.execute(
                                        "INSERT INTO garden_photos (entry_id, user_id, photo_date, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                                        (eid, user_id, note_date, note, datetime.utcnow().isoformat(timespec="seconds")),
                                    )
                                    if is_fert and fert_date:
                                        db.execute(
                                            "UPDATE garden_entries SET last_fertilized_date = ?, last_fertilizer_type = COALESCE(?, last_fertilizer_type),"
                                            " next_fertilization_generated_at = NULL WHERE id = ? AND (last_fertilized_date IS NULL OR last_fertilized_date <= ?)",
                                            (fert_date, fert_type or None, eid, fert_date),
                                        )
                                    db.commit()
                                    changed = True
                                    result = {"ok": True}

                        elif block.name == "update_garden_entry":
                            eid = inp.get("entry_id")
                            if not eid:
                                result = {"error": "entry_id is required"}
                            else:
                                entry = db.execute(
                                    "SELECT id, user_id FROM garden_entries WHERE id = ?", (eid,)
                                ).fetchone()
                                if not entry:
                                    result = {"error": f"Entry {eid} not found"}
                                elif entry["user_id"] != user_id:
                                    result = {"error": f"Entry {eid} is read-only (partner's)"}
                                else:
                                    fields, vals = [], []
                                    for col in ("plant_name", "variety", "location_type", "location_name", "notes"):
                                        if col in inp and inp[col] is not None:
                                            fields.append(f"{col} = ?")
                                            vals.append((inp[col] or "").strip() or None)
                                    if "planted_date" in inp:
                                        fields.append("planted_date = ?")
                                        vals.append(_parse_date_to_iso((inp["planted_date"] or "").strip(), today))
                                    if fields:
                                        now = datetime.utcnow().isoformat(timespec="seconds")
                                        fields.append("updated_at = ?")
                                        vals.append(now)
                                        db.execute(f"UPDATE garden_entries SET {', '.join(fields)} WHERE id = ?", (*vals, eid))
                                        db.commit()
                                        changed = True
                                    result = {"ok": True}

                        elif block.name == "set_entry_zone":
                            eid = inp.get("entry_id")
                            if not eid:
                                result = {"error": "entry_id is required"}
                            else:
                                entry = db.execute(
                                    "SELECT id, user_id FROM garden_entries WHERE id = ?", (eid,)
                                ).fetchone()
                                if not entry:
                                    result = {"error": f"Entry {eid} not found"}
                                elif entry["user_id"] != user_id:
                                    result = {"error": f"Entry {eid} is read-only (partner's)"}
                                else:
                                    zname = (inp.get("zone_name") or "").strip()
                                    zone_id_val = None
                                    if zname:
                                        zrow = db.execute(
                                            f"SELECT id FROM yard_zones WHERE user_id IN {ph} AND lower(name) = lower(?)",
                                            (*id_args, zname),
                                        ).fetchone()
                                        if zrow:
                                            zone_id_val = zrow["id"]
                                        else:
                                            result = {"error": f"Zone '{zname}' not found"}
                                    if "error" not in result:
                                        db.execute("UPDATE garden_entries SET zone_id = ? WHERE id = ?", (zone_id_val, eid))
                                        db.commit()
                                        changed = True
                                        result = {"ok": True}

                        elif block.name == "save_ornamental_note":
                            yp_id = inp.get("yard_plant_id")
                            note_text = (inp.get("note_text") or "").strip()
                            if not yp_id or not note_text:
                                result = {"error": "yard_plant_id and note_text are required"}
                            else:
                                yp_row = db.execute(
                                    f"SELECT id FROM yard_plants WHERE id = ? AND user_id IN {ph}",
                                    (yp_id, *id_args),
                                ).fetchone()
                                if not yp_row:
                                    result = {"error": f"yard_plant_id {yp_id} not found"}
                                else:
                                    raw_date = (inp.get("note_date") or today).strip()
                                    note_date = raw_date if _ISO_DATE_RE.match(raw_date) else today
                                    db.execute(
                                        "INSERT INTO yard_plant_notes (yard_plant_id, user_id, note_date, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                                        (yp_id, user_id, note_date, note_text, datetime.utcnow().isoformat(timespec="seconds")),
                                    )
                                    db.commit()
                                    changed = True
                                    result = {"ok": True}

                        else:
                            result = {"error": "Unknown tool"}

                    except Exception as tool_exc:
                        result = {"error": str(tool_exc)[:200]}

                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)})

                messages.append({"role": "user", "content": tool_results})

            _log_ai_chat(db, user_id, g.user["username"], "home", message)
            return jsonify(reply="Done.", changed=changed)

        except Exception as exc:
            try:
                db.execute(
                    "INSERT INTO chat_error_log (user_id, error_type, error_message, logged_at) VALUES (?, ?, ?, ?)",
                    (user_id, type(exc).__name__, str(exc)[:500], datetime.utcnow().isoformat(timespec="seconds")),
                )
                db.commit()
            except Exception:
                pass
            return jsonify(error="Assistant unavailable — please try again."), 503

    @app.route("/api/plant-suggestion")
    @login_required
    def api_plant_suggestion():
        cached = session.get("plant_suggestion")
        if cached and "photo_urls" in cached:
            return jsonify(cached)
        db = get_db()
        user_id = g.user["id"]
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)

        user_row = db.execute(
            "SELECT suggestion_history, suggestion_queue FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        try:
            recent_suggestions = json.loads((user_row["suggestion_history"] if user_row else None) or "[]")
            if not isinstance(recent_suggestions, list):
                recent_suggestions = []
        except Exception:
            recent_suggestions = []
        try:
            queue = json.loads((user_row["suggestion_queue"] if user_row else None) or "[]")
            if not isinstance(queue, list):
                queue = []
        except Exception:
            queue = []

        if queue:
            # Pop the next queued suggestion and fetch its photos
            raw = queue.pop(0)
            db.execute(
                "UPDATE users SET suggestion_queue = ? WHERE id = ?",
                (json.dumps(queue), user_id),
            )
            db.commit()
            suggestion = fetch_photos_for_suggestion(raw)
        else:
            # Queue empty — generate a fresh batch of 5
            ornamental_rows = db.execute(
                f"SELECT name FROM plants WHERE user_id IN {ph} ORDER BY created_at DESC LIMIT 40",
                id_args,
            ).fetchall()
            edible_rows = db.execute(
                f"SELECT DISTINCT plant_name FROM garden_entries WHERE user_id IN {ph} ORDER BY plant_name LIMIT 30",
                id_args,
            ).fetchall()
            zone_rows = db.execute(
                f"SELECT DISTINCT plant_name FROM yard_plants WHERE user_id IN {ph}",
                id_args,
            ).fetchall()
            ornamental_names = [r["name"] for r in ornamental_rows]
            edible_names = [r["plant_name"] for r in edible_rows]
            planted_in_zone = set(r["plant_name"] for r in zone_rows)
            planted_ornamental_names = [n for n in ornamental_names if n in planted_in_zone]
            location = g.user.get("location") or ""

            batch, err = generate_plant_suggestions_batch(
                location, ornamental_names, edible_names,
                recent_suggestions=recent_suggestions,
                planted_ornamental_names=planted_ornamental_names,
                count=5,
            )
            if err or not batch:
                return jsonify(error=err or "Could not generate suggestion"), 500

            # Record all 5 names in history, store 4 in queue, use 1 now
            new_names = [s["name"] for s in batch]
            recent_suggestions = (recent_suggestions + new_names)[-10:]
            db.execute(
                "UPDATE users SET suggestion_history = ?, suggestion_queue = ? WHERE id = ?",
                (json.dumps(recent_suggestions), json.dumps(batch[1:]), user_id),
            )
            db.commit()
            suggestion = fetch_photos_for_suggestion(batch[0])

        session["plant_suggestion"] = suggestion
        session.modified = True
        return jsonify(suggestion)

    @app.route("/plant-suggestion")
    @login_required
    def plant_suggestion_preview():
        suggestion = session.get("plant_suggestion")
        if not suggestion:
            return redirect(url_for("dashboard"))
        return render_template("plant_suggestion.html", suggestion=suggestion)

    @app.route("/plant-suggestion/add", methods=["POST"])
    @login_required
    def plant_suggestion_add():
        suggestion = session.get("plant_suggestion")
        if not suggestion:
            return redirect(url_for("dashboard"))
        db = get_db()
        user_id = g.user["id"]
        sun = normalize_sun_value(suggestion.get("sun_needs") or "")
        wn = suggestion.get("watering_needs") or ""
        water = "minimal" if wn == "minimal" else ("frequent" if wn == "frequent" else wn)
        _desc = suggestion.get("description") or ""
        _why = suggestion.get("why") or ""
        _desc_combined = "\n\n".join(filter(None, [_desc, f"Suggested because: {_why}" if _why else ""])) or None
        plant_id = db.execute(
            """
            INSERT INTO plants
            (user_id, name, scientific_name, lookup_query, source_type, image_url,
             sun_exposure, lifecycle, description, water_needs, plant_form,
             size_info, flowering_schedule, lookup_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                user_id,
                suggestion["name"],
                suggestion.get("scientific_name") or None,
                suggestion.get("scientific_name") or suggestion["name"],
                "world",
                suggestion.get("photo_url") or None,
                sun or None,
                suggestion.get("lifecycle") or None,
                _desc_combined,
                water or None,
                suggestion.get("plant_form") or None,
                suggestion.get("size_info") or None,
                suggestion.get("flowering_schedule") or None,
                "draft",
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        ).fetchone()["id"]
        db.commit()
        _log_activity(db, user_id, "suggestion_added", suggestion["name"])
        db.commit()
        session.pop("plant_suggestion", None)
        session.modified = True
        flash(f"{suggestion['name']} added to your ornamentals.")
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/api/service-status")
    @login_required
    def api_service_status():
        if not g.user.get("is_admin"):
            return jsonify(error="Forbidden"), 403

        db = get_db()
        now = datetime.utcnow()
        _24h = 86400  # seconds

        def _cache_get(key):
            row = db.execute(
                "SELECT value_json, updated_at FROM service_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None, None
            try:
                age = (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
                return json.loads(row["value_json"]), age
            except Exception:
                return None, None

        def _cache_set(key, value):
            db.execute(
                "INSERT INTO service_cache (key, value_json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, json.dumps(value), now.isoformat(timespec="seconds")),
            )
            db.commit()

        services = []

        # Anthropic — no programmatic usage API on a standard key
        services.append({
            "id": "anthropic",
            "name": "Claude (Anthropic)",
            "configured": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
            "used_for": "Plant name lookup, care details, label text extraction",
            "free_tier": None,
            "limit_note": "Pay-as-you-go — ~$0.25 per million tokens (Haiku model)",
            "dashboard_url": "https://console.anthropic.com/settings/billing",
        })

        # Plant.id — usage API available; cache for 24 h
        plant_id_key = os.environ.get("PLANT_ID_API_KEY", "").strip()
        plant_id_credits = None
        plant_id_limit = None
        plant_id_checked_ago = None
        if plant_id_key:
            cached, age_secs = _cache_get("plant_id_usage")
            if cached is not None and age_secs is not None and age_secs < _24h:
                credit = cached.get("credit") or {}
                plant_id_credits = credit.get("remaining")
                plant_id_limit = credit.get("total")
                plant_id_checked_ago = int(age_secs)
            else:
                try:
                    r = requests.get(
                        "https://plant.id/api/v3/usage_info",
                        headers={"Api-Key": plant_id_key},
                        timeout=5,
                    )
                    if r.ok:
                        usage = r.json()
                        _cache_set("plant_id_usage", usage)
                        credit = usage.get("credit") or {}
                        plant_id_credits = credit.get("remaining")
                        plant_id_limit = credit.get("total")
                        plant_id_checked_ago = 0
                except Exception:
                    if cached:
                        credit = cached.get("credit") or {}
                        plant_id_credits = credit.get("remaining")
                        plant_id_limit = credit.get("total")
                        plant_id_checked_ago = int(age_secs) if age_secs else None
        services.append({
            "id": "plant_id",
            "name": "Plant.id",
            "configured": bool(plant_id_key),
            "used_for": "Photo-based plant identification",
            "free_tier": "100 identifications",
            "limit_note": "Starter plan: 100 free, then paid",
            "dashboard_url": "https://admin.kindwise.com/",
            "credits_remaining": plant_id_credits,
            "credits_total": plant_id_limit,
            "checked_ago_secs": plant_id_checked_ago,
        })

        # OCR Space — no programmatic usage API
        services.append({
            "id": "ocr_space",
            "name": "OCR Space",
            "configured": bool(os.environ.get("OCR_SPACE_API_KEY", "").strip()),
            "used_for": "Reading text from plant label photos",
            "free_tier": "25,000 pages / month",
            "limit_note": "Free plan resets monthly — check dashboard for current use",
            "dashboard_url": "https://ocr.space/ocrapi",
        })

        # iNaturalist — always available, no key
        services.append({
            "id": "inaturalist",
            "name": "iNaturalist",
            "configured": True,
            "always_free": True,
            "used_for": "Plant search autocomplete and photos",
            "free_tier": "Unlimited",
            "limit_note": "Free public API — no key required",
            "dashboard_url": "https://www.inaturalist.org/pages/api+reference",
        })

        # Perenual (optional) — no programmatic usage API
        services.append({
            "id": "perenual",
            "name": "Perenual",
            "configured": bool(os.environ.get("PERENUAL_API_KEY", "").strip()),
            "optional": True,
            "used_for": "Enhanced plant search autocomplete (optional)",
            "free_tier": "100 requests / day",
            "limit_note": "Free plan: 100 req/day, resets daily",
            "dashboard_url": "https://perenual.com/docs/api",
        })

        return jsonify(services=services)

    @app.route("/api/ocr-label", methods=["POST"])
    @login_required
    def api_ocr_label():
        text, error = extract_text_from_image(request.files.get("label_photo"))
        if error:
            return jsonify(error=error), 200
        name = extract_plant_name_from_text(text) or infer_query_from_text(text)
        if not name:
            return jsonify(error="Could not extract a plant name from that label."), 200
        return jsonify(name=name)

    @app.route("/ideas/<int:plant_id>/delete", methods=["POST"])
    @login_required
    def delete_idea(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"])
        ).fetchone()
        if plant is None:
            flash("Plant not found.")
            return redirect(url_for("ideas_index"))
        db.execute("DELETE FROM plant_tags WHERE plant_id = ?", (plant_id,))
        db.execute("DELETE FROM plant_categories WHERE plant_id = ?", (plant_id,))
        db.execute("DELETE FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"]))
        db.commit()
        flash("Plant idea deleted.")
        return redirect(url_for("ideas_index"))

    @app.route("/ideas/<int:plant_id>/tags", methods=["POST"])
    @login_required
    def add_plant_tag(plant_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        plant = db.execute(
            f"SELECT id FROM plants WHERE id = ? AND user_id IN {ph}", (plant_id, *id_args)
        ).fetchone()
        if plant is None:
            flash("Plant not found.")
            return redirect(url_for("ideas_index"))
        tag_name = request.form.get("tag_name", "").strip()[:50]
        if not tag_name:
            return redirect(url_for("idea_detail", plant_id=plant_id))
        color = tag_color_for(tag_name)
        existing_tag = db.execute(
            "SELECT id, name, color FROM tags WHERE user_id = ? AND lower(name) = lower(?)",
            (g.user["id"], tag_name),
        ).fetchone()
        if existing_tag:
            tag_id = existing_tag["id"]
            tag_name = existing_tag["name"]
            color = existing_tag["color"]
        else:
            tag_id = db.execute(
                "INSERT INTO tags (user_id, name, color) VALUES (?, ?, ?) RETURNING id",
                (g.user["id"], tag_name, color),
            ).fetchone()["id"]
        db.execute(
            "INSERT INTO plant_tags (plant_id, tag_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (plant_id, tag_id),
        )
        _log_activity(db, g.user["id"], "tag_applied", tag_name)
        db.commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"id": tag_id, "name": tag_name, "color": color})
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/ideas/<int:plant_id>/tags/<int:tag_id>/remove", methods=["POST"])
    @login_required
    def remove_plant_tag(plant_id: int, tag_id: int):
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        plant = db.execute(
            f"SELECT id FROM plants WHERE id = ? AND user_id IN {ph}", (plant_id, *id_args)
        ).fetchone()
        if plant is None:
            flash("Plant not found.")
            return redirect(url_for("ideas_index"))
        db.execute("DELETE FROM plant_tags WHERE plant_id = ? AND tag_id = ?", (plant_id, tag_id))
        db.commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": True})
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/api/tags")
    @login_required
    def api_tags():
        q = request.args.get("q", "").strip()
        db = get_db()
        if q:
            tags = db.execute(
                "SELECT id, name, color FROM tags WHERE user_id = ? AND lower(name) LIKE lower(?) ORDER BY name ASC LIMIT 10",
                (g.user["id"], f"%{q}%"),
            ).fetchall()
        else:
            tags = db.execute(
                "SELECT id, name, color FROM tags WHERE user_id = ? ORDER BY name ASC LIMIT 20",
                (g.user["id"],),
            ).fetchall()
        return jsonify(tags=[{"id": t["id"], "name": t["name"], "color": t["color"]} for t in tags])

    @app.route("/ideas/<int:plant_id>/update-photos", methods=["POST"])
    @login_required
    def update_idea_photos(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"])
        ).fetchone()
        if plant is None:
            return jsonify(error="Not found"), 404
        data = request.get_json(silent=True) or {}
        photos = [str(u) for u in (data.get("photos") or []) if u]
        db.execute(
            "UPDATE plants SET photo_urls = ? WHERE id = ?",
            (json.dumps(photos) if photos else None, plant_id),
        )
        db.commit()
        return jsonify(ok=True)

    @app.route("/ideas/<int:plant_id>/upload-photo", methods=["POST"])
    @login_required
    def upload_idea_photo(plant_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"])
        ).fetchone()
        if plant is None:
            return jsonify(error="Not found"), 404
        image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"], g.user["id"], "idea")
        if not image_path:
            return jsonify(error="Please select a photo."), 400
        if image_path.startswith("http://") or image_path.startswith("https://"):
            photo_url = image_path
        else:
            photo_url = url_for("uploads", filename=image_path)
        return jsonify(ok=True, url=photo_url)

    @app.route("/plants/new")
    def legacy_new_plant():
        return redirect(url_for("new_idea"))

    @app.route("/plants/<int:plant_id>")
    def legacy_plant_detail(plant_id: int):
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/uploads/<path:filename>")
    @login_required
    def uploads(filename: str):
        allowed_ids = _shared_user_ids(get_db(), g.user["id"])
        if not any(filename.startswith(f"{uid}_") for uid in allowed_ids):
            flash("You do not have access to that file.")
            return redirect(url_for("dashboard"))
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(404)
    def not_found(_e):
        return render_template(
            "error.html",
            code=404,
            icon="🌿",
            title="Page not found",
            message="That page doesn't exist — it may have been moved or deleted.",
            show_report=False,
            report_body="",
        ), 404

    @app.errorhandler(413)
    def payload_too_large(_e):
        return render_template(
            "error.html",
            code=413,
            icon="📷",
            title="Photo too large",
            message="The photo you uploaded is too large to process. Try a smaller image or one taken at a lower resolution.",
            show_report=False,
            report_body="",
        ), 413

    @app.errorhandler(500)
    def internal_error(e):
        import traceback
        tb = traceback.format_exc()
        report = f"Error: {e}\n\nTraceback:\n{tb}"
        return render_template(
            "error.html",
            code=500,
            icon="🪴",
            title="Something went wrong",
            message="An unexpected error occurred. Your data is safe — please go back and try again.",
            show_report=True,
            report_body=report[:800],
        ), 500

    return app


def get_db():
    if "db" not in g:
        g.db = _connect(current_app().config["DATABASE_URL"])
    return g.db


def current_app():
    from flask import current_app as flask_current_app

    return flask_current_app


def init_db():
    db = get_db()
    # Autocommit each DDL statement independently so the transaction pooler
    # can't split a SERIAL column's implicit CREATE SEQUENCE from its CREATE TABLE,
    # and so a failed statement doesn't abort the rest.
    db._conn.autocommit = True
    for stmt in _SCHEMA_STATEMENTS:
        try:
            db.execute(stmt)
        except Exception:
            pass  # table or sequence already exists — safe to continue
    db._conn.autocommit = False

    ensure_column(db, "users", "api_token", "TEXT")
    ensure_column(db, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE users SET is_admin = 1 WHERE lower(username) = lower('boatmarina')")
    db.commit()
    ensure_column(db, "plants", "user_id", "INTEGER")
    ensure_column(db, "plants", "scientific_name", "TEXT")
    ensure_column(db, "plants", "lookup_query", "TEXT")
    ensure_column(db, "plants", "label_photo_path", "TEXT")
    ensure_column(db, "plants", "lookup_status", "TEXT")
    ensure_column(db, "plants", "pnw_native", "INTEGER")
    ensure_column(db, "plants", "photo_urls", "TEXT")
    ensure_column(db, "plants", "evergreen_status", "TEXT")
    ensure_column(db, "plants", "plant_form", "TEXT")
    ensure_column(db, "plants", "height_category", "TEXT")
    ensure_column(db, "plants", "description", "TEXT")
    ensure_column(db, "plants", "water_needs", "TEXT")
    ensure_column(db, "plants", "deadheading", "TEXT")
    ensure_column(db, "plants", "deer_resistant", "TEXT")
    ensure_column(db, "plants", "last_fertilized_date", "TEXT")
    ensure_column(db, "plants", "last_fertilizer_type", "TEXT")
    ensure_column(db, "plants", "next_fertilization_date", "TEXT")
    ensure_column(db, "plants", "next_fertilization_note", "TEXT")
    ensure_column(db, "plants", "next_fertilization_generated_at", "TEXT")
    ensure_column(db, "plants", "planned_fertilization_date", "TEXT")
    ensure_column(db, "plants", "never_fertilize", "INTEGER")
    ensure_column(db, "plants", "never_water", "INTEGER")
    ensure_column(db, "users", "photo_id_provider", "TEXT")
    ensure_column(db, "users", "location", "TEXT")
    ensure_column(db, "users", "whats_new_seen", "TEXT")
    ensure_column(db, "users", "suggestion_history", "TEXT")
    ensure_column(db, "users", "suggestion_queue", "TEXT")
    ensure_column(db, "garden_shares", "confirmed", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "garden_shares", "requested_by", "INTEGER")
    ensure_column(db, "categories", "is_default", "INTEGER NOT NULL DEFAULT 0")
    # Allow note-only growth log entries (image_path may be NULL)
    try:
        db.execute("ALTER TABLE garden_photos ALTER COLUMN image_path DROP NOT NULL")
        db.commit()
    except Exception:
        pass

    ensure_column(db, "perenual_log", "user_id", "INTEGER")
    ensure_column(db, "garden_photos", "is_fertilization", "INTEGER")
    ensure_column(db, "garden_photos", "fertilizer_type", "TEXT")
    ensure_column(db, "garden_photos", "fertilization_date", "TEXT")
    ensure_column(db, "garden_entries", "last_fertilized_date", "TEXT")
    ensure_column(db, "garden_entries", "last_fertilizer_type", "TEXT")
    ensure_column(db, "garden_entries", "next_fertilization_date", "TEXT")
    ensure_column(db, "garden_entries", "next_fertilization_note", "TEXT")
    ensure_column(db, "garden_entries", "next_fertilization_generated_at", "TEXT")
    ensure_column(db, "garden_entries", "planned_fertilization_date", "TEXT")
    ensure_column(db, "garden_entries", "never_fertilize", "INTEGER")
    ensure_column(db, "garden_entries", "never_water", "INTEGER")
    ensure_column(db, "garden_entries", "zone_id",                  "INTEGER")
    ensure_column(db, "garden_entries", "last_watered_date",         "TEXT")
    ensure_column(db, "garden_entries", "watering_frequency_days",   "INTEGER")
    ensure_column(db, "garden_entries", "watering_note",             "TEXT")
    ensure_column(db, "garden_entries", "watering_generated_at",     "TEXT")
    ensure_column(db, "garden_entries", "next_watering_date",        "TEXT")
    ensure_column(db, "plants",         "last_watered_date",         "TEXT")
    ensure_column(db, "plants",         "watering_frequency_days",   "INTEGER")
    ensure_column(db, "plants",         "watering_note",             "TEXT")
    ensure_column(db, "plants",         "watering_generated_at",     "TEXT")
    ensure_column(db, "plants",         "next_watering_date",        "TEXT")

    # Backfill watering frequency for plants that have never had a suggestion.
    # Uses simple heuristics so home-screen alerts and thumbnail badges appear immediately
    # without waiting for each detail page to be visited. watering_generated_at is left
    # NULL so the AI will still produce a proper suggestion on the first detail-page load.
    try:
        today_iso = datetime.utcnow().date().isoformat()
        # Edibles: frequency based on location type
        db.execute(
            """
            UPDATE garden_entries SET
              watering_frequency_days = CASE
                WHEN location_type IN ('raised_bed', 'container', 'grow_bag') THEN 2
                WHEN location_type = 'greenhouse'                              THEN 3
                ELSE 4
              END,
              next_watering_date = ?,
              watering_generated_at = NULL
            WHERE watering_frequency_days IS NULL
            """,
            (today_iso,),
        )
        # Ornamentals: frequency based on documented water_needs
        db.execute(
            """
            UPDATE plants SET
              watering_frequency_days = CASE
                WHEN lower(water_needs) LIKE '%frequent%' OR lower(water_needs) LIKE '%high%' THEN 3
                WHEN lower(water_needs) LIKE '%drought%' OR lower(water_needs) LIKE '%low%'
                  OR lower(water_needs) LIKE '%xeric%'  OR lower(water_needs) LIKE '%dry%'  THEN 14
                ELSE 7
              END,
              next_watering_date = ?,
              watering_generated_at = NULL
            WHERE watering_frequency_days IS NULL
            """,
            (today_iso,),
        )
        # Also clear any previously-cached future dates for never-watered entries
        # (generated before the "never watered → due today" fix was deployed).
        db.execute(
            "UPDATE garden_entries SET watering_generated_at = NULL"
            " WHERE last_watered_date IS NULL AND next_watering_date > ?",
            (today_iso,),
        )
        db.execute(
            "UPDATE plants SET watering_generated_at = NULL"
            " WHERE last_watered_date IS NULL AND next_watering_date > ?",
            (today_iso,),
        )
        db.commit()
    except Exception:
        pass

    # Backfill existing growth-log notes using keyword detection (one-time, skips already-classified rows)
    unclassified = db.execute(
        "SELECT id, notes FROM garden_photos WHERE is_fertilization IS NULL AND notes IS NOT NULL"
    ).fetchall()
    for row in unclassified:
        is_fert, ftype, _ = _detect_fertilization(row["notes"])
        db.execute(
            "UPDATE garden_photos SET is_fertilization = ?, fertilizer_type = ? WHERE id = ?",
            [is_fert, ftype, row["id"]],
        )
    db.execute("UPDATE garden_photos SET is_fertilization = 0 WHERE is_fertilization IS NULL")

    # Migrate yard_plants.notes → yard_plant_notes (one-time; skips yard_plants that already have entries)
    old_notes = db.execute(
        """
        SELECT yp.id AS yard_plant_id, yp.user_id, yp.notes, yp.created_at
        FROM yard_plants yp
        WHERE yp.notes IS NOT NULL AND yp.notes != ''
          AND NOT EXISTS (SELECT 1 FROM yard_plant_notes n WHERE n.yard_plant_id = yp.id)
        """
    ).fetchall()
    for row in old_notes:
        note_date = row["created_at"][:10] if row["created_at"] else None
        db.execute(
            "INSERT INTO yard_plant_notes (yard_plant_id, user_id, note_date, notes, created_at) VALUES (?, ?, ?, ?, ?)",
            (row["yard_plant_id"], row["user_id"], note_date, row["notes"], row["created_at"] or datetime.utcnow().isoformat(timespec="seconds")),
        )

    # Backfill fertilization_date for existing fertilization notes that have date hints in their text
    needs_date = db.execute(
        "SELECT gp.id, gp.notes, gp.photo_date, ge.planted_date"
        " FROM garden_photos gp JOIN garden_entries ge ON ge.id = gp.entry_id"
        " WHERE gp.is_fertilization = 1 AND gp.fertilization_date IS NULL AND gp.notes IS NOT NULL"
    ).fetchall()
    for row in needs_date:
        if _has_date_hint(row["notes"]):
            fert_date = _extract_fertilization_date(row["notes"], row["photo_date"], row["planted_date"])
            if fert_date:
                db.execute(
                    "UPDATE garden_photos SET fertilization_date = ? WHERE id = ?",
                    [fert_date, row["id"]],
                )
    db.commit()

    user = db.execute("SELECT id FROM users WHERE lower(username) = lower('demo')").fetchone()
    if user is None:
        db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("demo", generate_password_hash("gardenpal-demo"), datetime.utcnow().isoformat(timespec="seconds")),
        )
        user = db.execute("SELECT id FROM users WHERE lower(username) = lower('demo')").fetchone()
    db.execute("UPDATE plants SET user_id = ? WHERE user_id IS NULL", (user["id"],))

    for category in DEFAULT_CATEGORIES:
        db.execute(
            "INSERT INTO categories (name, is_default) VALUES (?, 1) ON CONFLICT (name) DO NOTHING",
            (category,),
        )
    db.commit()


_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plants (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        name TEXT NOT NULL,
        scientific_name TEXT,
        lookup_query TEXT,
        source_type TEXT NOT NULL DEFAULT 'world',
        source_note TEXT,
        image_path TEXT,
        label_photo_path TEXT,
        image_url TEXT,
        size_info TEXT,
        flowering_schedule TEXT,
        sun_exposure TEXT,
        lifecycle TEXT,
        lookup_status TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        is_default INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plant_categories (
        plant_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        PRIMARY KEY (plant_id, category_id),
        FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
        FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS yard_zones (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        reference_image_path TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS yard_plants (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        zone_id INTEGER NOT NULL,
        plant_name TEXT NOT NULL,
        scientific_name TEXT,
        lookup_query TEXT,
        image_path TEXT,
        location_x REAL NOT NULL DEFAULT 50,
        location_y REAL NOT NULL DEFAULT 50,
        size_info TEXT,
        watering_needs TEXT,
        sun_needs TEXT,
        flowering_schedule TEXT,
        lifecycle TEXT,
        spreads TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        FOREIGN KEY (zone_id) REFERENCES yard_zones (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS garden_entries (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        plant_name TEXT NOT NULL,
        variety TEXT,
        location_type TEXT,
        location_name TEXT,
        planted_date TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS garden_photos (
        id SERIAL PRIMARY KEY,
        entry_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        image_path TEXT,
        photo_date TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (entry_id) REFERENCES garden_entries (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        color TEXT NOT NULL DEFAULT '#5B8A5F',
        UNIQUE (user_id, name),
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plant_tags (
        plant_id INTEGER NOT NULL,
        tag_id INTEGER NOT NULL,
        PRIMARY KEY (plant_id, tag_id),
        FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE
    )
    """,
    # Indexes — safe to re-run, all use IF NOT EXISTS
    "CREATE INDEX IF NOT EXISTS idx_plants_user_id      ON plants(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_plants_user_name    ON plants(user_id, name)",
    "CREATE INDEX IF NOT EXISTS idx_yard_zones_user_id  ON yard_zones(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_yard_plants_user_id ON yard_plants(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_yard_plants_zone_id ON yard_plants(zone_id)",
    "CREATE INDEX IF NOT EXISTS idx_garden_entries_user ON garden_entries(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_garden_photos_entry ON garden_photos(entry_id)",
    "CREATE INDEX IF NOT EXISTS idx_plant_cats_plant    ON plant_categories(plant_id)",
    "CREATE INDEX IF NOT EXISTS idx_tags_user_id        ON tags(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_plant_tags_plant_id ON plant_tags(plant_id)",
    "CREATE INDEX IF NOT EXISTS idx_plant_tags_tag_id   ON plant_tags(tag_id)",
    """
    CREATE TABLE IF NOT EXISTS service_cache (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_log (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        logged_in_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS perenual_log (
        id SERIAL PRIMARY KEY,
        query TEXT NOT NULL,
        result_count INTEGER NOT NULL DEFAULT 0,
        logged_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        item_name TEXT,
        logged_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_log_user ON activity_log(user_id)",
    """
    CREATE TABLE IF NOT EXISTS garden_shares (
        id SERIAL PRIMARY KEY,
        user_a_id INTEGER NOT NULL,
        user_b_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_a_id) REFERENCES users (id) ON DELETE CASCADE,
        FOREIGN KEY (user_b_id) REFERENCES users (id) ON DELETE CASCADE,
        UNIQUE (user_a_id, user_b_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_garden_shares_a ON garden_shares(user_a_id)",
    "CREATE INDEX IF NOT EXISTS idx_garden_shares_b ON garden_shares(user_b_id)",
    """
    CREATE TABLE IF NOT EXISTS chat_error_log (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        username TEXT,
        user_message TEXT,
        error_type TEXT NOT NULL,
        error_detail TEXT,
        logged_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_error_log_at ON chat_error_log(logged_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ai_chat_log (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        username TEXT,
        context TEXT NOT NULL,
        plant_name TEXT,
        user_message TEXT NOT NULL,
        logged_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ai_chat_log_at ON ai_chat_log(logged_at DESC)",
    # --- Indexes for common hot-path queries ---
    "CREATE INDEX IF NOT EXISTS idx_garden_entries_user_id   ON garden_entries(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_garden_entries_user_date ON garden_entries(user_id, planted_date DESC NULLS LAST)",
    "CREATE INDEX IF NOT EXISTS idx_garden_photos_entry_id   ON garden_photos(entry_id)",
    "CREATE INDEX IF NOT EXISTS idx_garden_photos_entry_date ON garden_photos(entry_id, photo_date DESC NULLS LAST)",
    "CREATE INDEX IF NOT EXISTS idx_garden_photos_entry_fert ON garden_photos(entry_id, is_fertilization)",
    # Filter columns used in ideas_index and yard search
    "CREATE INDEX IF NOT EXISTS idx_plants_sun_exposure    ON plants(user_id, sun_exposure)",
    "CREATE INDEX IF NOT EXISTS idx_plants_lifecycle       ON plants(user_id, lifecycle)",
    "CREATE INDEX IF NOT EXISTS idx_plants_evergreen       ON plants(user_id, evergreen_status)",
    "CREATE INDEX IF NOT EXISTS idx_plants_form            ON plants(user_id, plant_form)",
    "CREATE INDEX IF NOT EXISTS idx_plants_height          ON plants(user_id, height_category)",
    "CREATE INDEX IF NOT EXISTS idx_plants_created_at      ON plants(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_plant_cats_cat_plant   ON plant_categories(category_id, plant_id)",
    # Fertilization alert queries (dashboard)
    "CREATE INDEX IF NOT EXISTS idx_plants_user_next_fert  ON plants(user_id, next_fertilization_date)",
    "CREATE INDEX IF NOT EXISTS idx_garden_entries_zone_id ON garden_entries(zone_id)",
    "CREATE INDEX IF NOT EXISTS idx_garden_entries_next_fert ON garden_entries(user_id, next_fertilization_date)",
    # --- API usage tracking (rate limiting + daily spending caps) ---
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id           SERIAL PRIMARY KEY,
        user_id      INTEGER NOT NULL,
        date         TEXT NOT NULL,
        feature      TEXT NOT NULL,
        count        INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT,
        UNIQUE (user_id, date, feature),
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_usage_user_date ON api_usage(user_id, date)",
    """
    CREATE TABLE IF NOT EXISTS yard_plant_notes (
        id SERIAL PRIMARY KEY,
        yard_plant_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        note_date TEXT,
        notes TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (yard_plant_id) REFERENCES yard_plants (id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_yard_plant_notes_yp ON yard_plant_notes(yard_plant_id)",
    # Normalise any sun_exposure values that were stored as raw API strings
    # (e.g. "Part Sun", "Partial Shade", "Full Sun") instead of the canonical
    # "part-sun" / "full-sun" / "shade" values the filter UI expects.
    # The WHERE clause skips already-normalised rows so this is a safe no-op on repeat runs.
    """
    UPDATE plants
    SET sun_exposure = CASE
        WHEN LOWER(sun_exposure) LIKE '%full%' AND LOWER(sun_exposure) LIKE '%sun%' THEN 'full-sun'
        WHEN LOWER(sun_exposure) LIKE '%part%' THEN 'part-sun'
        WHEN LOWER(sun_exposure) LIKE '%shade%' THEN 'shade'
        ELSE NULL
    END
    WHERE sun_exposure IS NOT NULL
      AND sun_exposure NOT IN ('full-sun', 'part-sun', 'shade')
    """,
]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _compress_image(stream, max_px: int = 1400, quality: int = 82) -> tuple:
    """Return (compressed_bytes, content_type). Falls back to raw stream on error."""
    try:
        from PIL import Image, ExifTags
        import io
        stream.seek(0)
        img = Image.open(stream)
        # Auto-rotate based on EXIF orientation
        try:
            exif = img._getexif()
            if exif:
                orient_key = next((k for k, v in ExifTags.TAGS.items() if v == "Orientation"), None)
                if orient_key and orient_key in exif:
                    orient = exif[orient_key]
                    rotations = {3: 180, 6: 270, 8: 90}
                    if orient in rotations:
                        img = img.rotate(rotations[orient], expand=True)
        except Exception:
            pass
        # Resize if needed
        w, h = img.size
        if w > max_px or h > max_px:
            ratio = min(max_px / w, max_px / h)
            img = img.resize((round(w * ratio), round(h * ratio)), Image.LANCZOS)
        # Convert to RGB for JPEG output (handles PNG/RGBA etc.)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        stream.seek(0)
        return stream.read(), None


def save_upload(file_storage, upload_folder: str, user_id: int, kind: str):
    if file_storage is None or not file_storage.filename:
        return ""
    filename = secure_filename(file_storage.filename)
    if not filename or not allowed_file(filename):
        return ""

    data, content_type = _compress_image(file_storage.stream)
    ext = "jpg" if content_type == "image/jpeg" else filename.rsplit(".", 1)[1].lower()
    unique_name = f"{user_id}/{kind}/{uuid.uuid4().hex}.{ext}"

    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", "")

    if supabase_url and supabase_key and bucket:
        resp = requests.post(
            f"{supabase_url}/storage/v1/object/{bucket}/{unique_name}",
            headers={
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": content_type or "image/jpeg",
            },
            data=data,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return f"{supabase_url}/storage/v1/object/public/{bucket}/{unique_name}"

    # Local fallback
    unique_name_flat = unique_name.replace("/", "_")
    destination = Path(upload_folder) / unique_name_flat
    destination.write_bytes(data)
    return unique_name_flat


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.get("user") is None:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def ensure_column(db, table_name: str, column_name: str, column_spec: str):
    exists = db.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
        (table_name, column_name),
    ).fetchone()
    if exists is None:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_spec}")


# (keyword_fragment, display_type) — first match wins; None type means extract via AI or use "fertilizer"
_FERTILIZER_TERMS = [
    ("worm cast", "worm castings"),
    ("wormcast", "worm castings"),
    ("vermicast", "worm castings"),
    ("vermicompost", "vermicompost"),
    ("bone meal", "bone meal"),
    ("bonemeal", "bone meal"),
    ("blood meal", "blood meal"),
    ("bloodmeal", "blood meal"),
    ("fish emulsion", "fish emulsion"),
    ("fish meal", "fish meal"),
    ("fishmeal", "fish meal"),
    ("kelp meal", "kelp meal"),
    ("kelp extract", "kelp extract"),
    ("seaweed extract", "seaweed extract"),
    ("seaweed fertiliz", "seaweed fertilizer"),
    ("compost tea", "compost tea"),
    ("chicken pellet", "chicken pellets"),
    ("chicken manure", "chicken manure"),
    ("cow manure", "cow manure"),
    ("horse manure", "horse manure"),
    ("rabbit manure", "rabbit manure"),
    ("bat guano", "bat guano"),
    ("seabird guano", "seabird guano"),
    ("guano", "guano"),
    ("miracle-gro", "Miracle-Gro"),
    ("miracle gro", "Miracle-Gro"),
    ("growmore", "Growmore"),
    ("tomato feed", "tomato feed"),
    ("tomato fertiliz", "tomato fertilizer"),
    ("liquid seaweed", "liquid seaweed"),
    ("seaweed", "seaweed"),
    ("manure", "manure"),
    ("epsom salt", "Epsom salt"),
    ("rock dust", "rock dust"),
    ("greensand", "greensand"),
    ("slow-release", "slow-release fertilizer"),
    ("slow release", "slow-release fertilizer"),
    ("granular fertiliz", "granular fertilizer"),
    ("granular feed", "granular feed"),
    ("liquid feed", "liquid feed"),
    ("liquid fertiliz", "liquid fertilizer"),
    ("liquid fertilis", "liquid fertilizer"),
    ("plant food", "plant food"),
    ("organic feed", "organic feed"),
    ("organic fertiliz", "organic fertilizer"),
    ("organic fertilis", "organic fertilizer"),
    ("fertilis", None),
    ("fertiliz", None),
    ("npk", None),
]


import re as _re

# Matches note text that hints at a specific date other than when the note was written
_DATE_HINT_RE = _re.compile(
    r'\b(when|at|upon|during)\s+plant'
    r'|\byesterday\b'
    r'|\blast\s+(week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'
    r'|\b\d+\s+days?\s+ago\b'
    r'|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'
    r'|\b(january|february|march|april|may|june|july|august|september|october|november|december'
    r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\b'
    r'|\b\d{1,2}/\d{1,2}\b',
    _re.IGNORECASE,
)

_ISO_DATE_RE = _re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _local_today() -> str:
    """Return the user's local date as YYYY-MM-DD from the browser cookie, falling back to UTC."""
    try:
        local_date = request.cookies.get('gp_local_date', '').strip()
        if local_date and _ISO_DATE_RE.match(local_date):
            return local_date
    except RuntimeError:
        pass
    return datetime.utcnow().date().isoformat()


def _local_date_plus(days: int) -> str:
    """Return local today + `days` as YYYY-MM-DD."""
    return (date.fromisoformat(_local_today()) + timedelta(days=days)).isoformat()


# Matches growth-log phrases that indicate a replanting / reseeding event.
_REPLANT_RE = _re.compile(
    r'\b(re[-\s]?seed(?:ed|ing)?|re[-\s]?sow(?:ed|ing)?|re[-\s]?plant(?:ed|ing)?'
    r'|transplant(?:ed|ing)?|germinate[ds]?|new\s+seedling[s]?|new\s+plant[s]?'
    r'|direct\s+sow(?:ed|n)?|sow(?:ed|n)\s+seed[s]?)\b',
    _re.IGNORECASE,
)


def _has_date_hint(note_text):
    return bool(note_text and _DATE_HINT_RE.search(note_text))


def _extract_establishment_date(growth_notes):
    """Return the most recent photo_date from growth notes that mention a replanting/reseeding event.

    Used to give the fertilization advisor the effective age of the *current* plants
    when the original planted_date no longer reflects reality (e.g. crop bolted, reseeded).
    """
    for photo_date, notes in reversed(list(growth_notes)):
        if photo_date and notes and _REPLANT_RE.search(notes):
            return photo_date
    return None


def _detect_fertilization(note_text):
    """Keyword-based detection. Returns (is_fert 0/1, fert_type or None, fert_date None).
    Date is always None here — resolved separately when text contains a date hint."""
    if not note_text:
        return (0, None, None)
    lower = note_text.lower()
    for keyword, ftype in _FERTILIZER_TERMS:
        if keyword in lower:
            return (1, ftype, None)
    return (0, None, None)


def _extract_fertilization_date(note_text, photo_date, planted_date):
    """Ask Haiku to resolve the actual fertilization date from note text.
    Returns YYYY-MM-DD string or None (caller falls back to photo_date)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not note_text:
        return None
    try:
        import anthropic as _anthropic
        ctx_parts = []
        if photo_date:
            ctx_parts.append(f"note written: {photo_date}")
        if planted_date:
            ctx_parts.append(f"plant planted: {planted_date}")
        ctx = "; ".join(ctx_parts)
        client = _anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=15,
            system=(
                "Return ONLY the date fertilizer was actually applied, as YYYY-MM-DD. "
                "Use the context to resolve phrases like 'when planting' or 'yesterday'. "
                "Return null if the date cannot be determined."
            ),
            messages=[{"role": "user", "content": f"Context: {ctx}\nNote: {note_text[:400]}"}],
        )
        result = resp.content[0].text.strip().strip('"').strip("'")
        return result if _ISO_DATE_RE.match(result) else None
    except Exception:
        return None


def _ai_detect_fertilization(note_text, photo_date=None, planted_date=None):
    """Claude Haiku fallback for notes that don't match the keyword list.
    Returns (is_fert 0/1, fert_type or None, fert_date or None)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not note_text:
        return (0, None, None)
    try:
        import anthropic as _anthropic
        import json as _json
        ctx_parts = []
        if photo_date:
            ctx_parts.append(f"note written: {photo_date}")
        if planted_date:
            ctx_parts.append(f"plant planted: {planted_date}")
        ctx = "; ".join(ctx_parts)
        client = _anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=(
                "Classify garden notes. Reply ONLY with JSON, no prose. "
                "Return {\"f\":true,\"t\":\"type\",\"d\":\"YYYY-MM-DD\"} if a fertilizer/amendment "
                "was applied (worm castings, bone/blood meal, manure, compost tea, kelp, seaweed, "
                "plant food, pellets, liquid feed, etc.). Return {\"f\":false} otherwise. "
                "\"t\": short fertilizer name (1-4 words). "
                "\"d\": actual application date — use context to resolve 'when planting', 'yesterday' etc; "
                "omit if date is unclear. "
                f"Context: {ctx}"
            ),
            messages=[{"role": "user", "content": note_text[:400]}],
        )
        data = _json.loads(resp.content[0].text.strip())
        if data.get("f"):
            d = data.get("d")
            fert_date = d if (d and _ISO_DATE_RE.match(str(d))) else None
            return (1, data.get("t") or None, fert_date)
        return (0, None, None)
    except Exception:
        return (0, None, None)


def _plant_name_root(name):
    """Canonical form of a plant name for plural/singular dedup.

    Handles the most common English plural patterns so that e.g.
    'Strawberries' (user-entered) doesn't appear alongside 'Strawberry'
    (static list), and varieties from 'Strawberries' are merged into
    the 'Strawberry' bucket.
    """
    w = name.strip().lower()
    parts = w.rsplit(' ', 1)
    last = parts[-1]
    if last.endswith('ies') and len(last) > 4:
        last = last[:-3] + 'y'       # strawberries → strawberry
    elif last.endswith('oes') and len(last) > 3:
        last = last[:-2]             # tomatoes → tomato, potatoes → potato
    elif last.endswith('s') and not last.endswith('ss') and len(last) > 3:
        last = last[:-1]             # beans → bean, peppers → pepper
    return (parts[0] + ' ' + last) if len(parts) == 2 else last


def _build_plant_autocomplete_data(db, user_ids):
    """Return (plant_names, plant_varieties) merging static PLANT_SUGGESTIONS with user's own entries."""
    ph = "({})".format(",".join("?" * len(user_ids)))
    id_args = list(user_ids)
    rows = db.execute(
        f"SELECT DISTINCT plant_name, variety FROM garden_entries WHERE user_id IN {ph}",
        id_args,
    ).fetchall()

    # Collect user's own plant names and varieties
    user_data: dict = {}
    for row in rows:
        pn = (row["plant_name"] or "").strip()
        if not pn:
            continue
        variety = (row["variety"] or "").strip()
        key = pn.lower()
        if key not in user_data:
            user_data[key] = {"display": pn, "varieties": set()}
        if variety:
            user_data[key]["varieties"].add(variety)

    # Merge plant names: static list + user-only names with no matching static entry
    # (exact lowercase match OR matching root handles plural/singular variants)
    static_keys = {n.lower() for n in PLANT_SUGGESTIONS}
    static_roots = {_plant_name_root(n) for n in PLANT_SUGGESTIONS}
    extra_names = [
        v["display"] for k, v in user_data.items()
        if k not in static_keys and _plant_name_root(k) not in static_roots
    ]
    all_names = sorted(list(PLANT_SUGGESTIONS.keys()) + extra_names, key=str.lower)

    # Build varieties dict: lowercase static plant name -> list
    varieties: dict = {}
    for name, vars_list in PLANT_SUGGESTIONS.items():
        varieties[name.lower()] = list(vars_list)

    # Root → static key mapping, for merging varieties from plural/singular user entries
    static_root_to_key = {_plant_name_root(n): n.lower() for n in PLANT_SUGGESTIONS}

    # Merge user-recorded varieties, deduplicating by root match
    for key, data in user_data.items():
        if not data["varieties"]:
            continue
        if key in varieties:
            target_key = key
        else:
            target_key = static_root_to_key.get(_plant_name_root(key), key)
        if target_key in varieties:
            existing_lower = {v.lower() for v in varieties[target_key]}
            for v in sorted(data["varieties"]):
                if v.lower() not in existing_lower:
                    varieties[target_key].append(v)
                    existing_lower.add(v.lower())
        else:
            varieties[target_key] = sorted(data["varieties"])

    return all_names, varieties


def _feature_fertilization(user):
    """Feature flag: next-fertilization suggestions + due badges. Early-access only."""
    return (user or {}).get("username") in {"boatmarina", "holval@gmail.com"}


def _feature_watering(user):
    """Feature flag: watering tracker. Early-access only."""
    return (user or {}).get("username") in {"boatmarina", "holval@gmail.com"}


def _feature_home_assistant(user):
    """Home-screen garden assistant — available to all users."""
    return True


def _feature_garden_zones(user):
    """Edible garden entries can be assigned to yard zones."""
    return True


# Per-feature daily limits and minimum seconds between consecutive calls.
_API_LIMITS = {
    "chat":          {"daily": 40, "burst_secs": 3},
    "fertilization": {"daily": 100, "burst_secs": 0},  # cache already throttles; 100/day covers large gardens
    "watering":      {"daily": 100, "burst_secs": 0},
}


def _check_api_rate(db, user_id, feature):
    """Check rate + daily cap for a user/feature. Returns (allowed: bool, reason: str|None).

    Allowed → also increments the counter and updates last_used_at.
    Denied  → counter is NOT incremented so the limit isn't consumed on rejection.
    """
    cfg = _API_LIMITS.get(feature, {"daily": 20, "burst_secs": 5})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    row = db.execute(
        "SELECT count, last_used_at FROM api_usage WHERE user_id = ? AND date = ? AND feature = ?",
        (user_id, today, feature),
    ).fetchone()

    count = row["count"] if row else 0
    last_used_at = row["last_used_at"] if row else None

    # Burst check
    burst = cfg["burst_secs"]
    if burst and last_used_at:
        try:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last_used_at)).total_seconds()
            if elapsed < burst:
                return False, f"Please wait a moment before sending another message."
        except Exception:
            pass

    # Daily cap
    if count >= cfg["daily"]:
        return False, f"Daily limit reached for this feature ({cfg['daily']}/day). Try again tomorrow."

    # Allowed — upsert the counter
    db.execute(
        "INSERT INTO api_usage (user_id, date, feature, count, last_used_at) VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT (user_id, date, feature) DO UPDATE SET count = api_usage.count + 1, last_used_at = EXCLUDED.last_used_at",
        (user_id, today, feature, now_iso),
    )
    db.commit()
    return True, None


def _suggest_next_fertilization(db, entry, user_location, last_fertilized, growth_notes):
    """Call Claude Sonnet to suggest the next fertilization date. Caches result in DB."""
    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        today_str = _local_today()
        plant_name = entry["plant_name"] or ""
        variety_str = f" ({entry['variety']})" if entry.get("variety") else ""
        location_type = (entry.get("location_type") or "").replace("_", " ")
        location_name = entry.get("location_name") or ""
        planted_date = entry.get("planted_date") or "unknown"

        if last_fertilized and last_fertilized.get("date"):
            last_fert_str = last_fertilized["date"]
            last_fert_type = last_fertilized.get("type") or ""
        else:
            last_fert_str = "never"
            last_fert_type = ""

        notes_lines = []
        for photo_date, notes in growth_notes:
            if notes:
                notes_lines.append(f"- {photo_date or '?'}: {notes[:200]}")
        notes_str = "\n".join(notes_lines) if notes_lines else "None"

        # Determine effective age of current plants — if a reseeding/transplanting event
        # appears in the growth log, the original planted_date is no longer meaningful.
        effective_date = _extract_establishment_date(growth_notes)
        if effective_date and effective_date != planted_date:
            try:
                age_days = (datetime.utcnow().date() - datetime.fromisoformat(effective_date).date()).days
            except Exception:
                age_days = None
            planting_info = (
                f"Originally planted: {planted_date}\n"
                f"Current plants established (reseeded/transplanted per log): {effective_date}"
                + (f" — {age_days} days ago as of today" if age_days is not None else "")
            )
        else:
            try:
                age_days = (
                    (datetime.utcnow().date() - datetime.fromisoformat(planted_date).date()).days
                    if planted_date and planted_date != "unknown" else None
                )
            except Exception:
                age_days = None
            planting_info = (
                f"Planted: {planted_date}"
                + (f" — {age_days} days ago as of today" if age_days is not None else "")
            )

        user_msg = (
            f"Plant: {plant_name}{variety_str}\n"
            f"Location type: {location_type or 'unknown'}\n"
            f"Location name: {location_name or 'not specified'}\n"
            f"User's location: {user_location or 'unknown'}\n"
            f"{planting_info}\n"
            f"Last fertilized: {last_fert_str}"
            + (f" (fertilizer: {last_fert_type})" if last_fert_type else "")
            + f"\nGrowth log notes:\n{notes_str}\nToday is {today_str}."
        )

        client = _anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=(
                "You are a gardening advisor. Given plant details and growth history, "
                "suggest the next fertilization date.\n"
                "Pay close attention to current plant age — seedlings under 3-4 weeks old "
                "generally should not be fertilized yet; wait until true leaves are established.\n"
                "If a reseeding or transplanting date is given, use that age, not the original planting date.\n"
                "Reply with ONLY:\n"
                "Line 1: YYYY-MM-DD (the suggested date, today or in the future)\n"
                "Line 2+: 2-3 sentence explanation of your reasoning.\n"
                "No labels, no extra text."
            ),
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        lines = raw.splitlines()
        date_line = lines[0].strip() if lines else ""
        if not _ISO_DATE_RE.match(date_line):
            return None
        if date_line < today_str:
            date_line = today_str
        note_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        generated_at = datetime.utcnow().isoformat(timespec="seconds")
        db.execute(
            "UPDATE garden_entries SET next_fertilization_date = ?, next_fertilization_note = ?,"
            " next_fertilization_generated_at = ? WHERE id = ?",
            (date_line, note_text or None, generated_at, entry["id"]),
        )
        db.commit()
        return {"date": date_line, "note": note_text or None}
    except Exception:
        return None


def _suggest_next_fertilization_ornamental(db, plant, user_location, last_fert_date):
    """Call Claude Haiku to suggest the next fertilization date for an ornamental plant. Caches in DB."""
    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        today_str = _local_today()
        plant_name = plant["name"] or ""
        sci_name = plant.get("scientific_name") or ""
        plant_form = (plant.get("plant_form") or "").replace("-", " ")
        lifecycle = plant.get("lifecycle") or ""
        flowering = plant.get("flowering_schedule") or ""
        sun = plant.get("sun_exposure") or ""
        water = plant.get("water_needs") or ""
        last_fert_str = last_fert_date or "never"

        user_msg = (
            f"Plant: {plant_name}" + (f" ({sci_name})" if sci_name else "") + "\n"
            + (f"Form: {plant_form}\n" if plant_form else "")
            + (f"Lifecycle: {lifecycle}\n" if lifecycle else "")
            + (f"Flowering: {flowering}\n" if flowering else "")
            + (f"Sun: {sun}\n" if sun else "")
            + (f"Water needs: {water}\n" if water else "")
            + f"User's location: {user_location or 'unknown'}\n"
            + f"Last fertilized: {last_fert_str}\n"
            + f"Today is {today_str}."
        )

        client = _anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=(
                "You are a gardening advisor. Given ornamental plant details, "
                "suggest the next fertilization date.\n"
                "Reply with ONLY:\n"
                "Line 1: YYYY-MM-DD (the suggested date, today or in the future)\n"
                "Line 2+: 2-3 sentence explanation of your reasoning.\n"
                "No labels, no extra text."
            ),
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        lines = raw.splitlines()
        date_line = lines[0].strip() if lines else ""
        if not _ISO_DATE_RE.match(date_line):
            return None
        if date_line < today_str:
            date_line = today_str
        note_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        generated_at = datetime.utcnow().isoformat(timespec="seconds")
        db.execute(
            "UPDATE plants SET next_fertilization_date = ?, next_fertilization_note = ?,"
            " next_fertilization_generated_at = ? WHERE id = ?",
            (date_line, note_text or None, generated_at, plant["id"]),
        )
        db.commit()
        return {"date": date_line, "note": note_text or None}
    except Exception:
        return None


def _suggest_watering_frequency(db, entry, user_location, last_watered, growth_notes):
    """Call Claude Sonnet to suggest watering frequency (days between waterings) for an edible. Caches in DB."""
    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        today_str = _local_today()
        plant_name = entry["plant_name"] or ""
        variety_str = f" ({entry['variety']})" if entry.get("variety") else ""
        location_type = (entry.get("location_type") or "").replace("_", " ")
        location_name = entry.get("location_name") or ""
        planted_date = entry.get("planted_date") or "unknown"
        notes_lines = []
        for photo_date, notes in growth_notes:
            if notes:
                notes_lines.append(f"- {photo_date or '?'}: {notes[:200]}")
        notes_str = "\n".join(notes_lines) if notes_lines else "None"
        user_msg = (
            f"Plant: {plant_name}{variety_str}\n"
            f"Location type: {location_type or 'unknown'}\n"
            f"Location name: {location_name or 'not specified'}\n"
            f"User's location: {user_location or 'unknown'}\n"
            f"Planted: {planted_date}\n"
            f"Last watered: {last_watered or 'unknown'}\n"
            f"Growth log notes:\n{notes_str}\n"
            f"Today is {today_str}."
        )
        client = _anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            system=(
                "You are a gardening advisor. Given plant details, suggest how frequently to water.\n"
                "Consider: plant type, container vs in-ground, raised bed, location and climate, season.\n"
                "Reply with ONLY:\n"
                "Line 1: number of days between waterings (integer, e.g. 3)\n"
                "Line 2+: 1-2 sentence explanation.\n"
                "No labels, no extra text."
            ),
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        lines = raw.splitlines()
        days_line = lines[0].strip() if lines else ""
        if not days_line.isdigit():
            return None
        freq_days = max(1, min(30, int(days_line)))
        note_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        from datetime import timedelta as _td
        if last_watered and _ISO_DATE_RE.match(last_watered):
            next_date = (datetime.fromisoformat(last_watered).date() + _td(days=freq_days)).isoformat()
        else:
            next_date = _local_today()  # never watered → due today
        generated_at = datetime.utcnow().isoformat(timespec="seconds")
        db.execute(
            "UPDATE garden_entries SET watering_frequency_days = ?, watering_note = ?,"
            " watering_generated_at = ?, next_watering_date = ? WHERE id = ?",
            (freq_days, note_text or None, generated_at, next_date, entry["id"]),
        )
        db.commit()
        return {"days": freq_days, "note": note_text or None, "next_date": next_date}
    except Exception:
        return None


def _suggest_watering_frequency_ornamental(db, plant, user_location, last_watered):
    """Call Claude Haiku to suggest watering frequency for an ornamental plant. Caches in DB."""
    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        today_str = _local_today()
        plant_name = plant["name"] or ""
        sci_name = plant.get("scientific_name") or ""
        plant_form = (plant.get("plant_form") or "").replace("-", " ")
        lifecycle = plant.get("lifecycle") or ""
        sun = plant.get("sun_exposure") or ""
        water = plant.get("water_needs") or ""
        user_msg = (
            f"Plant: {plant_name}" + (f" ({sci_name})" if sci_name else "") + "\n"
            + (f"Form: {plant_form}\n" if plant_form else "")
            + (f"Lifecycle: {lifecycle}\n" if lifecycle else "")
            + (f"Sun: {sun}\n" if sun else "")
            + (f"Water needs: {water}\n" if water else "")
            + f"User's location: {user_location or 'unknown'}\n"
            + f"Last watered: {last_watered or 'unknown'}\n"
            + f"Today is {today_str}."
        )
        client = _anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=(
                "You are a gardening advisor. Given ornamental plant details, suggest how frequently to water.\n"
                "Reply with ONLY:\n"
                "Line 1: number of days between waterings (integer, e.g. 7)\n"
                "Line 2+: 1-2 sentence explanation.\n"
                "No labels, no extra text."
            ),
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        lines = raw.splitlines()
        days_line = lines[0].strip() if lines else ""
        if not days_line.isdigit():
            return None
        freq_days = max(1, min(30, int(days_line)))
        note_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        from datetime import timedelta as _td
        if last_watered and _ISO_DATE_RE.match(last_watered):
            next_date = (datetime.fromisoformat(last_watered).date() + _td(days=freq_days)).isoformat()
        else:
            next_date = _local_today()  # never watered → due today
        generated_at = datetime.utcnow().isoformat(timespec="seconds")
        db.execute(
            "UPDATE plants SET watering_frequency_days = ?, watering_note = ?,"
            " watering_generated_at = ?, next_watering_date = ? WHERE id = ?",
            (freq_days, note_text or None, generated_at, next_date, plant["id"]),
        )
        db.commit()
        return {"days": freq_days, "note": note_text or None, "next_date": next_date}
    except Exception:
        return None


def infer_query_from_text(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return ""
    preferred = [line for line in lines if 2 <= len(line.split()) <= 4]
    source = preferred[0] if preferred else lines[0]
    return source[:80]


def apply_lookup_to_form(form_values: dict, details: dict, use_common_name: bool):
    if use_common_name and details.get("name"):
        form_values["name"] = details["name"]
    if details.get("scientific_name"):
        form_values["scientific_name"] = details["scientific_name"]
        if not form_values.get("lookup_query"):
            form_values["lookup_query"] = details["scientific_name"]
    if details.get("sun_needs"):
        form_values["sun_exposure"] = normalize_sun_value(details["sun_needs"])
    if details.get("lifecycle"):
        form_values["lifecycle"] = normalize_lifecycle(details["lifecycle"])
    if details.get("size_info"):
        form_values["size_info"] = details["size_info"]
    if details.get("flowering_schedule"):
        form_values["flowering_schedule"] = details["flowering_schedule"]
    if details.get("photo_url") and not form_values.get("image_url"):
        form_values["image_url"] = details["photo_url"]
    if details.get("pnw_native") is not None:
        form_values["pnw_native"] = details["pnw_native"]
    if details.get("evergreen_status"):
        form_values["evergreen_status"] = details["evergreen_status"]
    _VALID_PLANT_FORMS = {"tree","shrub","perennial","annual","climber","ground-cover","grass","fern","bulb","succulent","herb","bamboo"}
    pf = (details.get("plant_form") or "").strip().lower()
    if pf in _VALID_PLANT_FORMS:
        form_values["plant_form"] = pf
    hc = (details.get("height_category") or "").strip().lower()
    if hc in {"low","medium","tall","large"}:
        form_values["height_category"] = hc
    if details.get("description"):
        form_values["description"] = details["description"]
    if details.get("watering_needs"):
        form_values["water_needs"] = details["watering_needs"]
    if details.get("deadheading"):
        form_values["deadheading"] = details["deadheading"]
    if details.get("deer_resistant"):
        form_values["deer_resistant"] = details["deer_resistant"]
    form_values["lookup_status"] = "draft"


def apply_lookup_to_yard_form(form_values: dict, details: dict):
    if details.get("name") and not form_values.get("plant_name"):
        form_values["plant_name"] = details["name"]
    if details.get("scientific_name"):
        form_values["scientific_name"] = details["scientific_name"]
        if not form_values.get("lookup_query"):
            form_values["lookup_query"] = details["scientific_name"]
    if details.get("sun_needs"):
        form_values["sun_needs"] = details["sun_needs"]
    if details.get("watering_needs"):
        form_values["watering_needs"] = details["watering_needs"]
    if details.get("flowering_schedule"):
        form_values["flowering_schedule"] = details["flowering_schedule"]
    if details.get("lifecycle"):
        form_values["lifecycle"] = details["lifecycle"]
    if details.get("size_info"):
        form_values["size_info"] = details["size_info"]
    if details.get("spreads"):
        form_values["spreads"] = details["spreads"]
    if details.get("description"):
        form_values["description"] = details["description"]
    if details.get("photo_url") and not form_values.get("image_url"):
        form_values["image_url"] = details["photo_url"]


def normalize_sun_value(value: str) -> str:
    lower = value.lower()
    if "full" in lower and "sun" in lower:
        return "full-sun"
    if "part" in lower:
        return "part-sun"
    if "shade" in lower:
        return "shade"
    return ""


def normalize_lifecycle(value: str) -> str:
    lower = value.lower()
    if "perennial" in lower:
        return "perennial"
    if "annual" in lower:
        return "annual"
    if "biennial" in lower:
        return "biennial"
    return ""


def run():
    app = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=True)
