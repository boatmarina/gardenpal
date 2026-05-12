import os
import ssl
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import pg8000
import requests
from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from gardenpal.plant_lookup import extract_text_from_image, identify_plant_from_image, lookup_plant_details, lookup_plant_image

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DEFAULT_CATEGORIES = ["Love this", "Front porch", "Backyard", "Wishlist", "Pollinator friendly"]


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
    conn = pg8000.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
        ssl_context=ssl_ctx,
    )
    return _PgDB(conn)


def create_app() -> Flask:
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
        if user_id is not None:
            g.user = get_db().execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()

    @app.context_processor
    def inject_auth_user():
        return {"current_user": g.get("user")}

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
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.commit()

            user = db.execute("SELECT id, username FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
            session.clear()
            session["user_id"] = user["id"]
            flash("Account created. Welcome to GardenPal.")
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
            flash("Welcome back.")
            return redirect(url_for("dashboard"))

        return render_template("login.html")

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
        idea_count = db.execute("SELECT COUNT(*) AS count FROM plants WHERE user_id = ?", (user_id,)).fetchone()["count"]
        zone_count = db.execute(
            "SELECT COUNT(*) AS count FROM yard_zones WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        yard_plant_count = db.execute(
            "SELECT COUNT(*) AS count FROM yard_plants WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        return render_template(
            "dashboard.html",
            idea_count=idea_count,
            zone_count=zone_count,
            yard_plant_count=yard_plant_count,
        )

    @app.route("/ideas")
    @login_required
    def ideas_index():
        db = get_db()
        user_id = g.user["id"]
        q = request.args.get("q", "").strip()
        sun = request.args.get("sun", "").strip()
        lifecycle = request.args.get("lifecycle", "").strip()
        category_id = request.args.get("category", "").strip()

        query = """
            SELECT DISTINCT p.*
            FROM plants p
            LEFT JOIN plant_categories pc ON p.id = pc.plant_id
            WHERE p.user_id = ?
        """
        params = [user_id]

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
        if category_id:
            query += " AND pc.category_id = ?"
            params.append(category_id)

        query += " ORDER BY p.created_at DESC"
        plants = db.execute(query, params).fetchall()

        categories = db.execute(
            """
            SELECT DISTINCT c.*
            FROM categories c
            JOIN plant_categories pc ON c.id = pc.category_id
            JOIN plants p ON p.id = pc.plant_id
            WHERE p.user_id = ?
            ORDER BY c.name ASC
            """,
            (user_id,),
        ).fetchall()
        return render_template(
            "ideas_index.html",
            plants=plants,
            categories=categories,
            active_filters={"q": q, "sun": sun, "lifecycle": lifecycle, "category": category_id},
        )

    @app.route("/ideas/new", methods=["GET", "POST"])
    @login_required
    def new_idea():
        db = get_db()
        user_id = g.user["id"]
        plant_names = [
            r["name"] for r in db.execute(
                "SELECT DISTINCT name FROM plants WHERE user_id = ? ORDER BY name ASC", (user_id,)
            ).fetchall()
        ]

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
            "active_mode": "name",
        }

        if request.method == "POST":
            form_action = request.form.get("form_action", "save")
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
                "active_mode": request.form.get("active_mode", "name").strip(),
            }

            if form_action == "autofill_name":
                details, error = lookup_plant_details(form_values["lookup_query"] or form_values["name"])
                if error:
                    flash(error)
                else:
                    apply_lookup_to_form(form_values, details, use_common_name=True)
                    flash("Plant details autofilled.")
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names)

            if form_action == "autofill_label":
                text, error = extract_text_from_image(request.files.get("label_photo"))
                if error:
                    flash(error)
                else:
                    guessed = infer_query_from_text(text)
                    form_values["lookup_query"] = guessed
                    flash(f"Extracted label text: {guessed}")
                    details, lookup_error = lookup_plant_details(guessed)
                    if not lookup_error:
                        apply_lookup_to_form(form_values, details, use_common_name=not form_values["name"])
                        flash("Used extracted text to autofill details.")
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names)

            if form_action == "autofill_photo":
                suggestion, error = identify_plant_from_image(request.files.get("photo"))
                if error:
                    flash(error)
                else:
                    if suggestion.get("common_name") and not form_values["name"]:
                        form_values["name"] = suggestion["common_name"]
                    if suggestion.get("scientific_name"):
                        form_values["scientific_name"] = suggestion["scientific_name"]
                        form_values["lookup_query"] = suggestion["scientific_name"]
                    flash(f"Top photo match confidence: {suggestion.get('confidence') or 'unknown'}")
                    details, lookup_error = lookup_plant_details(form_values["lookup_query"] or form_values["name"])
                    if not lookup_error:
                        apply_lookup_to_form(form_values, details, use_common_name=not form_values["name"])
                        flash("Used photo match to autofill details.")
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names)

            if not form_values["name"]:
                flash("Plant name is required.")
                return render_template("idea_new.html", form_values=form_values, plant_names=plant_names)

            # Auto-fill details on save if not already done via an explicit autofill action
            if form_values["lookup_status"] != "draft":
                details, _ = lookup_plant_details(form_values["lookup_query"] or form_values["name"])
                if details:
                    apply_lookup_to_form(form_values, details, use_common_name=False)

            image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"], user_id, "idea")
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
                 image_url, size_info, flowering_schedule, sun_exposure, lifecycle, lookup_status, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            ).fetchone()["id"]

            db.commit()
            flash("Plant idea added.")
            return redirect(url_for("idea_detail", plant_id=plant_id))

        return render_template("idea_new.html", form_values=form_values, plant_names=plant_names)

    @app.route("/ideas/<int:plant_id>")
    @login_required
    def idea_detail(plant_id: int):
        db = get_db()
        plant = db.execute("SELECT * FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"])).fetchone()
        if plant is None:
            flash("Plant was not found.")
            return redirect(url_for("ideas_index"))

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
        return render_template("idea_detail.html", plant=plant, categories=categories)

    @app.route("/yard")
    @login_required
    def yard_index():
        db = get_db()
        zones = db.execute(
            """
            SELECT z.*, COUNT(yp.id) AS plant_count
            FROM yard_zones z
            LEFT JOIN yard_plants yp ON yp.zone_id = z.id
            WHERE z.user_id = ?
            GROUP BY z.id
            ORDER BY z.created_at DESC
            """,
            (g.user["id"],),
        ).fetchall()
        return render_template("yard_index.html", zones=zones)

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
            db.commit()
            flash("Yard zone created.")
            return redirect(url_for("yard_index"))
        return render_template("yard_zone_new.html")

    @app.route("/yard/zones/<int:zone_id>")
    @login_required
    def yard_zone_detail(zone_id: int):
        db = get_db()
        zone = db.execute("SELECT * FROM yard_zones WHERE id = ? AND user_id = ?", (zone_id, g.user["id"])).fetchone()
        if zone is None:
            flash("Yard zone not found.")
            return redirect(url_for("yard_index"))
        plants = db.execute(
            "SELECT * FROM yard_plants WHERE zone_id = ? AND user_id = ? ORDER BY created_at DESC",
            (zone_id, g.user["id"]),
        ).fetchall()
        return render_template("yard_zone_detail.html", zone=zone, plants=plants)

    @app.route("/yard/plants/new", methods=["GET", "POST"])
    @login_required
    def yard_plant_new():
        db = get_db()
        zones = db.execute("SELECT * FROM yard_zones WHERE user_id = ? ORDER BY name ASC", (g.user["id"],)).fetchall()
        idea_names = [r["name"] for r in db.execute(
            "SELECT DISTINCT name FROM plants WHERE user_id = ? ORDER BY name ASC", (g.user["id"],)
        ).fetchall()]
        yard_names = [r["plant_name"] for r in db.execute(
            "SELECT DISTINCT plant_name FROM yard_plants WHERE user_id = ? ORDER BY plant_name ASC", (g.user["id"],)
        ).fetchall()]
        plant_names = sorted(set(idea_names + yard_names))

        form_values = {
            "zone_id": "",
            "plant_name": "",
            "scientific_name": "",
            "lookup_query": "",
            "watering_needs": "",
            "sun_needs": "",
            "flowering_schedule": "",
            "lifecycle": "",
            "size_info": "",
            "spreads": "",
            "notes": "",
            "location_x": "50",
            "location_y": "50",
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
                "notes": request.form.get("notes", "").strip(),
                "location_x": request.form.get("location_x", "").strip() or "50",
                "location_y": request.form.get("location_y", "").strip() or "50",
            }

            if form_action == "autofill_name":
                details, error = lookup_plant_details(form_values["lookup_query"] or form_values["plant_name"])
                if error:
                    flash(error)
                else:
                    apply_lookup_to_yard_form(form_values, details)
                    flash("Planted item details autofilled from name lookup.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names)

            if form_action == "autofill_photo":
                suggestion, error = identify_plant_from_image(request.files.get("photo"))
                if error:
                    flash(error)
                else:
                    if suggestion.get("common_name") and not form_values["plant_name"]:
                        form_values["plant_name"] = suggestion["common_name"]
                    if suggestion.get("scientific_name"):
                        form_values["scientific_name"] = suggestion["scientific_name"]
                        form_values["lookup_query"] = suggestion["scientific_name"]
                    details, lookup_error = lookup_plant_details(form_values["lookup_query"] or form_values["plant_name"])
                    if not lookup_error:
                        apply_lookup_to_yard_form(form_values, details)
                    flash(f"Photo-based suggestion confidence: {suggestion.get('confidence') or 'unknown'}")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names)

            if not form_values["zone_id"] or not form_values["plant_name"]:
                flash("Zone and plant name are required.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names)

            zone = db.execute(
                "SELECT id FROM yard_zones WHERE id = ? AND user_id = ?",
                (form_values["zone_id"], g.user["id"]),
            ).fetchone()
            if zone is None:
                flash("Please choose a valid zone.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names)

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
                    form_values["location_x"],
                    form_values["location_y"],
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
            db.commit()
            flash("Planted item saved.")
            return redirect(url_for("yard_zone_detail", zone_id=form_values["zone_id"]))
        return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names)

    @app.route("/api/plant-search")
    @login_required
    def api_plant_search():
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify(results=[])
        api_key = os.environ.get("PERENUAL_API_KEY", "").strip()
        if api_key:
            try:
                resp = requests.get(
                    "https://perenual.com/api/species-list",
                    params={"key": api_key, "q": q},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                results = []
                for plant in data[:12]:
                    sci = plant.get("scientific_name") or []
                    results.append({
                        "common_name": plant.get("common_name") or "",
                        "scientific_name": sci[0] if sci else "",
                    })
                if results:
                    return jsonify(results=results)
            except Exception:
                pass
        # Fall back to iNaturalist (free, no API key needed)
        try:
            resp = requests.get(
                "https://api.inaturalist.org/v1/taxa",
                params={"q": q, "is_active": "true", "iconic_taxa": "Plantae", "per_page": 12},
                timeout=10,
            )
            resp.raise_for_status()
            taxa = resp.json().get("results", [])
            results = []
            for t in taxa:
                photo = t.get("default_photo") or {}
                results.append({
                    "common_name": t.get("preferred_common_name") or t.get("name") or "",
                    "scientific_name": t.get("name") or "",
                    "photo_url": photo.get("medium_url") or photo.get("square_url"),
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
        db.execute("DELETE FROM plant_categories WHERE plant_id = ?", (plant_id,))
        db.execute("DELETE FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"]))
        db.commit()
        flash("Plant idea deleted.")
        return redirect(url_for("ideas_index"))

    @app.route("/plants/new")
    def legacy_new_plant():
        return redirect(url_for("new_idea"))

    @app.route("/plants/<int:plant_id>")
    def legacy_plant_detail(plant_id: int):
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/uploads/<path:filename>")
    @login_required
    def uploads(filename: str):
        if not filename.startswith(f"{g.user['id']}_"):
            flash("You do not have access to that file.")
            return redirect(url_for("dashboard"))
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

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

    ensure_column(db, "plants", "user_id", "INTEGER")
    ensure_column(db, "plants", "scientific_name", "TEXT")
    ensure_column(db, "plants", "lookup_query", "TEXT")
    ensure_column(db, "plants", "label_photo_path", "TEXT")
    ensure_column(db, "plants", "lookup_status", "TEXT")
    ensure_column(db, "categories", "is_default", "INTEGER NOT NULL DEFAULT 0")

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
]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file_storage, upload_folder: str, user_id: int, kind: str):
    if file_storage is None or not file_storage.filename:
        return ""
    filename = secure_filename(file_storage.filename)
    if not filename or not allowed_file(filename):
        return ""

    ext = filename.rsplit(".", 1)[1].lower()
    unique_name = f"{user_id}_{kind}_{uuid.uuid4().hex}.{ext}"
    destination = Path(upload_folder) / unique_name
    file_storage.save(destination)
    return unique_name


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.get("user") is None:
            flash("Please log in to access your plant diary.")
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
