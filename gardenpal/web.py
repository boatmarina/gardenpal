import json
import os
import secrets
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

from gardenpal.plant_lookup import extract_plant_name_from_text, extract_text_from_image, identify_plant_from_image, lookup_plant_details, lookup_plant_image, lookup_plant_photos

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
            g.user = get_db().execute("SELECT id, username, api_token FROM users WHERE id = ?", (user_id,)).fetchone()

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
        garden_count = db.execute(
            "SELECT COUNT(*) AS count FROM garden_entries WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        return render_template(
            "dashboard.html",
            idea_count=idea_count,
            zone_count=zone_count,
            yard_plant_count=yard_plant_count,
            garden_count=garden_count,
        )

    @app.route("/ideas")
    @login_required
    def ideas_index():
        db = get_db()
        user_id = g.user["id"]
        q = request.args.get("q", "").strip()
        sun = request.args.get("sun", "").strip()
        lifecycle = request.args.get("lifecycle", "").strip()
        evergreen = request.args.get("evergreen", "").strip()
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
        if evergreen:
            query += " AND p.evergreen_status = ?"
            params.append(evergreen)
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
            active_filters={"q": q, "sun": sun, "lifecycle": lifecycle, "evergreen": evergreen, "category": category_id},
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
            "pnw_native": None,
            "photo_urls": "",
            "evergreen_status": "",
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
                    if guessed:
                        if not form_values["name"]:
                            form_values["name"] = guessed
                        form_values["lookup_query"] = guessed
                        form_values["active_mode"] = "name"
                        flash(f"Label read: \"{guessed}\" — review the name then save.")
                    else:
                        flash("Could not extract a plant name from that label.")
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
                details, lookup_err = lookup_plant_details(form_values["lookup_query"] or form_values["name"])
                if details:
                    apply_lookup_to_form(form_values, details, use_common_name=False)
                elif lookup_err:
                    flash(f"Plant details could not be fetched: {lookup_err}")

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
                 image_url, size_info, flowering_schedule, sun_exposure, lifecycle, lookup_status, notes, pnw_native, photo_urls, evergreen_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    @app.route("/yard/zones/<int:zone_id>/photo", methods=["POST"])
    @login_required
    def yard_zone_update_photo(zone_id: int):
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        db = get_db()
        zone = db.execute("SELECT id FROM yard_zones WHERE id = ? AND user_id = ?", (zone_id, g.user["id"])).fetchone()
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
        db.execute("UPDATE yard_zones SET reference_image_path = ? WHERE id = ? AND user_id = ?", (image_path, zone_id, g.user["id"]))
        db.commit()
        if is_ajax:
            return jsonify(ok=True, url=image_path)
        flash("Zone photo updated.")
        return redirect(url_for("yard_zone_detail", zone_id=zone_id))

    @app.route("/yard/zones/<int:zone_id>/edit", methods=["GET", "POST"])
    @login_required
    def yard_zone_edit(zone_id: int):
        db = get_db()
        zone = db.execute("SELECT * FROM yard_zones WHERE id = ? AND user_id = ?", (zone_id, g.user["id"])).fetchone()
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
                    "UPDATE yard_zones SET name = ?, description = ? WHERE id = ? AND user_id = ?",
                    (name, description, zone_id, g.user["id"]),
                )
                db.commit()
                flash("Zone updated.")
                return redirect(url_for("yard_zone_detail", zone_id=zone_id))
        return render_template("yard_zone_edit.html", zone=zone)

    @app.route("/yard/zones/<int:zone_id>")
    @login_required
    def yard_zone_detail(zone_id: int):
        db = get_db()
        zone = db.execute("SELECT * FROM yard_zones WHERE id = ? AND user_id = ?", (zone_id, g.user["id"])).fetchone()
        if zone is None:
            flash("Yard zone not found.")
            return redirect(url_for("yard_index"))
        plants = db.execute(
            """SELECT yp.*,
                      p.id          AS lib_plant_id,
                      p.photo_urls  AS lib_photo_urls,
                      p.image_path  AS lib_image_path,
                      p.image_url   AS lib_image_url
               FROM yard_plants yp
               LEFT JOIN plants p ON p.name = yp.plant_name AND p.user_id = yp.user_id
               WHERE yp.zone_id = ? AND yp.user_id = ?
               ORDER BY yp.created_at DESC""",
            (zone_id, g.user["id"]),
        ).fetchall()
        return render_template("yard_zone_detail.html", zone=zone, plants=plants)

    @app.route("/yard/plants/new", methods=["GET", "POST"])
    @login_required
    def yard_plant_new():
        db = get_db()
        zones = db.execute("SELECT * FROM yard_zones WHERE user_id = ? ORDER BY name ASC", (g.user["id"],)).fetchall()
        plant_names = sorted(set(
            [r["name"] for r in db.execute("SELECT DISTINCT name FROM plants WHERE user_id = ? ORDER BY name ASC", (g.user["id"],)).fetchall()]
            + [r["plant_name"] for r in db.execute("SELECT DISTINCT plant_name FROM yard_plants WHERE user_id = ? ORDER BY plant_name ASC", (g.user["id"],)).fetchall()]
        ))
        library_plants = db.execute(
            "SELECT id, name, scientific_name, image_path, image_url, photo_urls, sun_exposure, lifecycle, size_info, flowering_schedule FROM plants WHERE user_id = ? ORDER BY name ASC",
            (g.user["id"],),
        ).fetchall()

        form_values = {
            "zone_id": request.args.get("zone_id", ""),
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
                "notes": request.form.get("notes", "").strip(),
                "image_url": request.form.get("image_url", "").strip(),
                "active_mode": request.form.get("active_mode", "library"),
                "yard_input_mode": request.form.get("yard_input_mode", "name"),
            }

            if form_action == "autofill_name":
                details, error = lookup_plant_details(form_values["lookup_query"] or form_values["plant_name"])
                if error:
                    flash(error)
                else:
                    apply_lookup_to_yard_form(form_values, details)
                    flash("Planted item details autofilled from name lookup.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants)

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
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants)

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
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants)

            if not form_values["zone_id"] or not form_values["plant_name"]:
                flash("Zone and plant name are required.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants)

            zone = db.execute(
                "SELECT id FROM yard_zones WHERE id = ? AND user_id = ?",
                (form_values["zone_id"], g.user["id"]),
            ).fetchone()
            if zone is None:
                flash("Please choose a valid zone.")
                return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants)

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
            # Add to library if not already there
            existing = db.execute(
                "SELECT id FROM plants WHERE user_id = ? AND name = ?",
                (g.user["id"], form_values["plant_name"]),
            ).fetchone()
            if existing is None:
                db.execute(
                    """
                    INSERT INTO plants
                    (user_id, name, scientific_name, lookup_query, source_type, source_note, image_path,
                     label_photo_path, image_url, size_info, flowering_schedule, sun_exposure, lifecycle,
                     lookup_status, notes, pnw_native, photo_urls, evergreen_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        g.user["id"],
                        form_values["plant_name"],
                        form_values["scientific_name"],
                        form_values["lookup_query"],
                        "yard",
                        None,
                        image_path,
                        None,
                        form_values.get("image_url") or None,
                        form_values["size_info"],
                        form_values["flowering_schedule"],
                        form_values["sun_needs"],
                        form_values["lifecycle"],
                        None,
                        form_values["notes"],
                        None,
                        None,
                        None,
                        datetime.utcnow().isoformat(timespec="seconds"),
                    ),
                )
            db.commit()
            flash("Planted item saved.")
            return redirect(url_for("yard_zone_detail", zone_id=form_values["zone_id"]))
        return render_template("yard_plant_new.html", zones=zones, form_values=form_values, plant_names=plant_names, library_plants=library_plants)

    # ── Settings ────────────────────────────────────────────────────────────

    @app.route("/settings")
    @login_required
    def settings():
        return render_template("settings.html", user=g.user)

    @app.route("/settings/generate-token", methods=["POST"])
    @login_required
    def generate_api_token():
        db = get_db()
        token = secrets.token_urlsafe(32)
        db.execute("UPDATE users SET api_token = ? WHERE id = ?", (token, g.user["id"]))
        db.commit()
        flash("New API token generated.")
        return redirect(url_for("settings"))

    @app.route("/settings/revoke-token", methods=["POST"])
    @login_required
    def revoke_api_token():
        db = get_db()
        db.execute("UPDATE users SET api_token = NULL WHERE id = ?", (g.user["id"],))
        db.commit()
        flash("API token revoked.")
        return redirect(url_for("settings"))

    # ── Garden tracker (UI) ─────────────────────────────────────────────────

    @app.route("/garden")
    @login_required
    def garden_index():
        from datetime import date
        from collections import defaultdict
        db = get_db()
        uid = g.user["id"]
        today = date.today()
        current_year = today.year

        # Derive available years from planted dates
        all_dated = db.execute(
            "SELECT planted_date FROM garden_entries WHERE user_id = ? AND planted_date IS NOT NULL",
            (uid,),
        ).fetchall()
        years_set = set()
        for r in all_dated:
            pd = r.planted_date
            yr = pd.year if hasattr(pd, "year") else int(str(pd)[:4])
            years_set.add(yr)
        years = sorted(years_set, reverse=True)
        if current_year not in years:
            years = [current_year] + years

        try:
            active_year = int(request.args.get("year", current_year))
        except (ValueError, TypeError):
            active_year = current_year
        if active_year not in years:
            active_year = years[0] if years else current_year

        # Entries for the active year (date range avoids EXTRACT)
        year_start = f"{active_year}-01-01"
        year_end   = f"{active_year + 1}-01-01"
        entries = db.execute(
            "SELECT * FROM garden_entries WHERE user_id = ?"
            " AND planted_date >= ? AND planted_date < ?"
            " ORDER BY planted_date ASC, plant_name ASC",
            (uid, year_start, year_end),
        ).fetchall()

        # Undated entries shown only on current-year view
        unscheduled = []
        if active_year == current_year:
            unscheduled = db.execute(
                "SELECT * FROM garden_entries WHERE user_id = ? AND planted_date IS NULL"
                " ORDER BY plant_name ASC",
                (uid,),
            ).fetchall()

        # Group by month
        grouped = defaultdict(list)
        for entry in entries:
            pd = entry.planted_date
            month = pd.month if hasattr(pd, "month") else int(str(pd)[5:7])
            grouped[month].append(entry)

        month_counts = {m: len(lst) for m, lst in grouped.items()}
        grouped_entries = sorted(grouped.items())

        return render_template(
            "garden_index.html",
            grouped_entries=grouped_entries,
            unscheduled=unscheduled,
            month_counts=month_counts,
            years=years,
            active_year=active_year,
            current_year=current_year,
        )

    @app.route("/garden/new", methods=["GET", "POST"])
    @login_required
    def garden_new():
        db = get_db()
        if request.method == "POST":
            plant_name = request.form.get("plant_name", "").strip()
            if not plant_name:
                flash("Plant name is required.")
                return render_template("garden_entry_new.html", form_values=request.form)
            now = datetime.utcnow().isoformat(timespec="seconds")
            entry_id = db.execute(
                """INSERT INTO garden_entries
                   (user_id, plant_name, variety, location_type, location_name, planted_date, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    g.user["id"],
                    plant_name,
                    request.form.get("variety", "").strip() or None,
                    request.form.get("location_type", "").strip() or None,
                    request.form.get("location_name", "").strip() or None,
                    request.form.get("planted_date", "").strip() or None,
                    request.form.get("notes", "").strip() or None,
                    now, now,
                ),
            ).fetchone()["id"]
            db.commit()
            flash("Entry added to your garden tracker.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return render_template("garden_entry_new.html", form_values={"planted_date": today})

    @app.route("/garden/<int:entry_id>")
    @login_required
    def garden_detail(entry_id):
        db = get_db()
        entry = db.execute(
            "SELECT * FROM garden_entries WHERE id = ? AND user_id = ?",
            (entry_id, g.user["id"]),
        ).fetchone()
        if entry is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        photos = db.execute(
            "SELECT * FROM garden_photos WHERE entry_id = ? ORDER BY photo_date DESC NULLS LAST, created_at DESC",
            (entry_id,),
        ).fetchall()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return render_template("garden_entry_detail.html", entry=entry, photos=photos, today=today)

    @app.route("/garden/<int:entry_id>/photos", methods=["POST"])
    @login_required
    def garden_add_photo(entry_id):
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        db = get_db()
        entry = db.execute(
            "SELECT id FROM garden_entries WHERE id = ? AND user_id = ?",
            (entry_id, g.user["id"]),
        ).fetchone()
        if entry is None:
            if is_ajax:
                return jsonify(error="Entry not found."), 404
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"], g.user["id"], "garden")
        if not image_path:
            if is_ajax:
                return jsonify(error="Please select a photo."), 400
            flash("Please select a photo.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        db.execute(
            "INSERT INTO garden_photos (entry_id, user_id, image_path, photo_date, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                g.user["id"],
                image_path,
                request.form.get("photo_date", "").strip() or None,
                request.form.get("photo_notes", "").strip() or None,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        if is_ajax:
            return jsonify(ok=True)
        flash("Photo added.")
        return redirect(url_for("garden_detail", entry_id=entry_id))

    @app.route("/garden/<int:entry_id>/edit", methods=["GET", "POST"])
    @login_required
    def garden_edit(entry_id):
        db = get_db()
        entry = db.execute(
            "SELECT * FROM garden_entries WHERE id = ? AND user_id = ?",
            (entry_id, g.user["id"]),
        ).fetchone()
        if entry is None:
            flash("Entry not found.")
            return redirect(url_for("garden_index"))
        if request.method == "POST":
            plant_name = request.form.get("plant_name", "").strip()
            if not plant_name:
                flash("Plant name is required.")
                return render_template("garden_entry_edit.html", entry=entry, form_values=request.form)
            db.execute(
                """UPDATE garden_entries
                   SET plant_name = ?, variety = ?, location_type = ?, location_name = ?,
                       planted_date = ?, notes = ?, updated_at = ?
                   WHERE id = ? AND user_id = ?""",
                (
                    plant_name,
                    request.form.get("variety", "").strip() or None,
                    request.form.get("location_type", "").strip() or None,
                    request.form.get("location_name", "").strip() or None,
                    request.form.get("planted_date", "").strip() or None,
                    request.form.get("notes", "").strip() or None,
                    datetime.utcnow().isoformat(timespec="seconds"),
                    entry_id,
                    g.user["id"],
                ),
            )
            db.commit()
            flash("Entry updated.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        return render_template("garden_entry_edit.html", entry=entry, form_values=entry)

    @app.route("/garden/<int:entry_id>/delete", methods=["POST"])
    @login_required
    def garden_delete(entry_id):
        db = get_db()
        db.execute("DELETE FROM garden_entries WHERE id = ? AND user_id = ?", (entry_id, g.user["id"]))
        db.commit()
        flash("Entry deleted.")
        return redirect(url_for("garden_index"))

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

    @app.route("/api/plant-photos")
    @login_required
    def api_plant_photos():
        q = request.args.get("q", "").strip()
        count = min(int(request.args.get("count", "3")), 6)
        if not q:
            return jsonify(photos=[])
        return jsonify(photos=lookup_plant_photos(q, count))

    @app.route("/api/plant-details")
    @login_required
    def api_plant_details():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify(error="No query provided"), 400
        result, error = lookup_plant_details(q)
        if error:
            return jsonify(error=error), 200
        return jsonify(result)

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
        db.execute("DELETE FROM plant_categories WHERE plant_id = ?", (plant_id,))
        db.execute("DELETE FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"]))
        db.commit()
        flash("Plant idea deleted.")
        return redirect(url_for("ideas_index"))

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

    ensure_column(db, "users", "api_token", "TEXT")
    ensure_column(db, "plants", "user_id", "INTEGER")
    ensure_column(db, "plants", "scientific_name", "TEXT")
    ensure_column(db, "plants", "lookup_query", "TEXT")
    ensure_column(db, "plants", "label_photo_path", "TEXT")
    ensure_column(db, "plants", "lookup_status", "TEXT")
    ensure_column(db, "plants", "pnw_native", "INTEGER")
    ensure_column(db, "plants", "photo_urls", "TEXT")
    ensure_column(db, "plants", "evergreen_status", "TEXT")
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
        image_path TEXT NOT NULL,
        photo_date TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (entry_id) REFERENCES garden_entries (id) ON DELETE CASCADE
    )
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
    if details.get("pnw_native") is not None:
        form_values["pnw_native"] = details["pnw_native"]
    if details.get("evergreen_status"):
        form_values["evergreen_status"] = details["evergreen_status"]
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
