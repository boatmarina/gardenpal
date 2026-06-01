import csv
import hashlib
import io
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
from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from gardenpal.plant_lookup import extract_plant_name_from_text, extract_text_from_image, identify_plant_from_image, lookup_plant_details, lookup_plant_image, lookup_plant_photos, resolve_scientific_name

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

    @app.template_filter("relative_login")
    def relative_login_filter(dt_str):
        if not dt_str:
            return "never"
        try:
            dt = datetime.fromisoformat(str(dt_str)[:19])
            days = (datetime.utcnow() - dt).days
            if days < 0:
                return "today"
            if days < 7:
                return dt.strftime("%A")
            if days < 14:
                return "last week"
            weeks = days // 7
            if weeks < 5:
                return f"{weeks} weeks ago"
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
        if user_id is not None:
            g.user = get_db().execute("SELECT id, username, api_token, is_admin, photo_id_provider, location FROM users WHERE id = ?", (user_id,)).fetchone()

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
            db.execute(
                "INSERT INTO login_log (user_id, logged_in_at) VALUES (?, ?)",
                (user["id"], datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.execute("DELETE FROM activity_log WHERE user_id = ?", (user["id"],))
            db.commit()
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
        ids = _shared_user_ids(db, user_id)
        ph, id_args = _in_ids(ids)
        shared_names = _shared_user_names(db, user_id)
        q = request.args.get("q", "").strip()
        sun = request.args.get("sun", "").strip()
        lifecycle = request.args.get("lifecycle", "").strip()
        evergreen = request.args.get("evergreen", "").strip()
        plant_form = request.args.get("plant_form", "").strip()
        height_category = request.args.get("height_category", "").strip()
        category_id = request.args.get("category", "").strip()
        tag_id = request.args.get("tag", "").strip()

        query = f"""
            SELECT DISTINCT p.*
            FROM plants p
            LEFT JOIN plant_categories pc ON p.id = pc.plant_id
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
        if category_id:
            query += " AND pc.category_id = ?"
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
        return render_template(
            "ideas_index.html",
            plants=plants,
            tags_map=tags_map,
            user_tags=user_tags,
            categories=categories,
            shared_names=shared_names,
            active_filters={"q": q, "sun": sun, "lifecycle": lifecycle, "evergreen": evergreen, "plant_form": plant_form, "height_category": height_category, "category": category_id, "tag": tag_id},
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
                 image_url, size_info, flowering_schedule, sun_exposure, lifecycle, lookup_status, notes, pnw_native, photo_urls, evergreen_status, plant_form, height_category, description, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        is_owner = plant["user_id"] == g.user["id"]

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
        return render_template("idea_detail.html", plant=plant, categories=categories,
                               zone=zone, yard_plant_id=yard_plant_id,
                               plant_tags=plant_tags, user_tags=user_tags,
                               all_zones=all_zones, is_owner=is_owner,
                               shared_names=shared_names,
                               plant_zones=[
                                   {"id": r["id"], "zone_id": r["zone_id"], "zone_name": r["zone_name"], "notes": r["notes"]}
                                   for r in db.execute(
                                       f"""
                                       SELECT yp.id, yp.notes, z.id AS zone_id, z.name AS zone_name
                                       FROM yard_plants yp
                                       JOIN yard_zones z ON z.id = yp.zone_id
                                       WHERE yp.user_id IN {ph} AND lower(yp.plant_name) = lower(?)
                                       ORDER BY z.name ASC
                                       """,
                                       (*id_args, plant["name"]),
                                   ).fetchall()
                               ])

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

    @app.route("/yard")
    @login_required
    def yard_index():
        db = get_db()
        ids = _shared_user_ids(db, g.user["id"])
        ph, id_args = _in_ids(ids)
        zones = db.execute(
            f"""
            SELECT z.*, COUNT(yp.id) AS plant_count
            FROM yard_zones z
            LEFT JOIN yard_plants yp ON yp.zone_id = z.id
            WHERE z.user_id IN {ph}
            GROUP BY z.id
            ORDER BY z.created_at DESC
            """,
            id_args,
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
                      p.id          AS lib_plant_id,
                      p.photo_urls  AS lib_photo_urls,
                      p.image_path  AS lib_image_path,
                      p.image_url   AS lib_image_url
               FROM yard_plants yp
               LEFT JOIN plants p ON p.name = yp.plant_name AND p.user_id = yp.user_id
               WHERE yp.zone_id = ? AND yp.user_id IN {ph}
               ORDER BY yp.created_at DESC""",
            [zone_id] + id_args,
        ).fetchall()
        lib_plant_ids = [p["lib_plant_id"] for p in plants if p["lib_plant_id"]]
        tags_map = {}
        if lib_plant_ids:
            ph = ",".join("?" * len(lib_plant_ids))
            for row in db.execute(
                f"SELECT pt.plant_id, t.id, t.name, t.color FROM plant_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.plant_id IN ({ph}) ORDER BY t.name ASC",
                lib_plant_ids,
            ).fetchall():
                tags_map.setdefault(row["plant_id"], []).append({"id": row["id"], "name": row["name"], "color": row["color"]})
        shared_names = _shared_user_names(db, g.user["id"])
        return render_template("yard_zone_detail.html", zone=zone, plants=plants, tags_map=tags_map, shared_names=shared_names)

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
            f"SELECT id, name, scientific_name, image_path, image_url, photo_urls, sun_exposure, lifecycle, size_info, flowering_schedule FROM plants WHERE user_id IN {ph} ORDER BY name ASC",
            id_args,
        ).fetchall()
        library_plants_json = [{"name": r["name"], "sci": r["scientific_name"] or ""} for r in library_plants]

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
                db.execute(
                    """
                    INSERT INTO plants
                    (user_id, name, scientific_name, lookup_query, source_type, source_note, image_path,
                     label_photo_path, image_url, size_info, flowering_schedule, sun_exposure, lifecycle,
                     lookup_status, notes, pnw_native, photo_urls, evergreen_status, plant_form, height_category, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        form_values.get("photo_urls_json") or None,
                        None,
                        None,
                        None,
                        datetime.utcnow().isoformat(timespec="seconds"),
                    ),
                )
            else:
                updates = {k: v for k, v in [
                    ("scientific_name",    form_values["scientific_name"] or None),
                    ("lookup_query",       form_values["lookup_query"] or None),
                    ("image_url",          form_values.get("image_url") or None),
                    ("photo_urls",         form_values.get("photo_urls_json") or None),
                    ("sun_exposure",       form_values["sun_needs"] or None),
                    ("lifecycle",          form_values["lifecycle"] or None),
                    ("size_info",          form_values["size_info"] or None),
                    ("flowering_schedule", form_values["flowering_schedule"] or None),
                ] if v}
                if updates:
                    set_clause = ", ".join(
                        f"{col} = COALESCE(NULLIF({col}, ''), ?)" for col in updates
                    )
                    db.execute(
                        f"UPDATE plants SET {set_clause} WHERE id = ?",
                        [*updates.values(), existing["id"]],
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
        back = request.args.get("back", type=int)
        if back:
            return redirect(url_for("idea_detail", plant_id=back))
        return redirect(url_for("yard_zone_detail", zone_id=row["zone_id"]))

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
        activity_rows = db.execute(
            "SELECT action, item_name FROM activity_log WHERE user_id = ? ORDER BY logged_at ASC",
            (user_id,),
        ).fetchall()
        _METHOD_LABELS = {
            "name": "by name", "photo": "from photo", "label": "from label",
            "url": "by URL", "library": "from library",
        }
        plant_entries = []      # [{"name": str, "method_label": str}, ...]
        yard_entries = []
        seen_plants = set()
        seen_yard = set()
        activity: dict = {}
        for row in activity_rows:
            act = row["action"]
            name = (row["item_name"] or "").strip()
            if not name:
                continue
            if act.startswith("plant_added"):
                suffix = act[len("plant_added"):].lstrip("_")
                label = _METHOD_LABELS.get(suffix, "")
                if name not in seen_plants:
                    seen_plants.add(name)
                    plant_entries.append({"name": name, "method_label": label})
            elif act.startswith("yard_plant_added"):
                suffix = act[len("yard_plant_added"):].lstrip("_")
                label = _METHOD_LABELS.get(suffix, "")
                if name not in seen_yard:
                    seen_yard.add(name)
                    yard_entries.append({"name": name, "method_label": label})
            else:
                if act not in activity:
                    activity[act] = []
                if name not in activity[act]:
                    activity[act].append(name)
        return {
            "plants": plants, "zones": zones, "garden": garden,
            "login_count": login_count, "last_login": last_login,
            "avg_per_week": avg_per_week,
            "last_session": activity,
            "plant_entries": plant_entries,
            "yard_entries": yard_entries,
        }

    @app.route("/tools")
    @login_required
    def tools():
        users_with_stats = []
        perenual_log = []
        db = get_db()
        if g.user.get("is_admin"):
            rows = db.execute(
                "SELECT id, username, is_admin, created_at FROM users ORDER BY created_at ASC"
            ).fetchall()
            for u in rows:
                stats = _user_stats(db, u["id"], u["created_at"])
                users_with_stats.append({"user": u, "stats": stats})
            perenual_log = db.execute(
                "SELECT query, result_count, logged_at FROM perenual_log"
                " ORDER BY logged_at DESC LIMIT 200"
            ).fetchall()
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
                               garden_shares=garden_shares,
                               garden_shares_in=garden_shares_in,
                               garden_shares_out=garden_shares_out,
                               plantid_configured=bool(os.environ.get("PLANT_ID_API_KEY", "").strip()),
                               gemini_configured=bool(os.environ.get("GEMINI_API_KEY", "").strip()),
                               claude_configured=bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()))

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
        ids = _shared_user_ids(db, uid)
        ph, id_args = _in_ids(ids)
        today = date.today()
        current_year = today.year

        # Derive available years from planted dates
        all_dated = db.execute(
            f"SELECT planted_date FROM garden_entries WHERE user_id IN {ph} AND planted_date IS NOT NULL",
            id_args,
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
            f"SELECT * FROM garden_entries WHERE user_id IN {ph}"
            " AND planted_date >= ? AND planted_date < ?"
            " ORDER BY planted_date ASC, plant_name ASC",
            id_args + [year_start, year_end],
        ).fetchall()

        # Undated entries shown only on current-year view
        unscheduled = []
        if active_year == current_year:
            unscheduled = db.execute(
                f"SELECT * FROM garden_entries WHERE user_id IN {ph} AND planted_date IS NULL"
                " ORDER BY plant_name ASC",
                id_args,
            ).fetchall()

        # Group by month
        grouped = defaultdict(list)
        for entry in entries:
            pd = entry.planted_date
            month = pd.month if hasattr(pd, "month") else int(str(pd)[5:7])
            grouped[month].append(entry)

        month_counts = {m: len(lst) for m, lst in grouped.items()}
        grouped_entries = sorted(grouped.items())

        shared_names = _shared_user_names(db, uid)
        return render_template(
            "garden_index.html",
            grouped_entries=grouped_entries,
            unscheduled=unscheduled,
            month_counts=month_counts,
            years=years,
            active_year=active_year,
            current_year=current_year,
            shared_names=shared_names,
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
            _log_activity(db, g.user["id"], "garden_entry_added", plant_name)
            db.commit()
            flash("Entry added to your garden tracker.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return render_template("garden_entry_new.html", form_values={"planted_date": today})

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
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return render_template("garden_entry_detail.html", entry=entry, photos=photos, today=today)

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
        db.execute(
            "INSERT INTO garden_photos (entry_id, user_id, image_path, photo_date, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                g.user["id"],
                image_path,
                request.form.get("photo_date", "").strip() or None,
                note_text,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        if is_ajax:
            return jsonify(ok=True)
        flash("Note added.")
        return redirect(url_for("garden_detail", entry_id=entry_id))

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
        if request.method == "POST":
            plant_name = request.form.get("plant_name", "").strip()
            if not plant_name:
                flash("Plant name is required.")
                return render_template("garden_entry_edit.html", entry=entry, form_values=request.form)
            db.execute(
                f"""UPDATE garden_entries
                   SET plant_name = ?, variety = ?, location_type = ?, location_name = ?,
                       planted_date = ?, notes = ?, updated_at = ?
                   WHERE id = ? AND user_id IN {ph}""",
                [
                    plant_name,
                    request.form.get("variety", "").strip() or None,
                    request.form.get("location_type", "").strip() or None,
                    request.form.get("location_name", "").strip() or None,
                    request.form.get("planted_date", "").strip() or None,
                    request.form.get("notes", "").strip() or None,
                    datetime.utcnow().isoformat(timespec="seconds"),
                    entry_id,
                ] + id_args,
            )
            db.commit()
            flash("Entry updated.")
            return redirect(url_for("garden_detail", entry_id=entry_id))
        return render_template("garden_entry_edit.html", entry=entry, form_values=entry)

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
                        "INSERT INTO perenual_log (query, result_count, logged_at) VALUES (?, ?, ?)",
                        (q, len(results), datetime.utcnow().isoformat(timespec="seconds")),
                    )
                    _db.commit()
                except Exception:
                    pass
                if results:
                    return jsonify(results=results)
            except Exception:
                pass

        # ── iNaturalist (free fallback) ──
        try:
            resp = requests.get(
                "https://api.inaturalist.org/v1/taxa",
                params={"q": q, "is_active": "true", "iconic_taxa": "Plantae", "per_page": 8},
                timeout=8,
            )
            resp.raise_for_status()
            taxa = resp.json().get("results", [])
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
        plant = db.execute(
            "SELECT id FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"])
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
        db.commit()
        return redirect(url_for("idea_detail", plant_id=plant_id))

    @app.route("/ideas/<int:plant_id>/tags/<int:tag_id>/remove", methods=["POST"])
    @login_required
    def remove_plant_tag(plant_id: int, tag_id: int):
        db = get_db()
        plant = db.execute(
            "SELECT id FROM plants WHERE id = ? AND user_id = ?", (plant_id, g.user["id"])
        ).fetchone()
        if plant is None:
            flash("Plant not found.")
            return redirect(url_for("ideas_index"))
        db.execute("DELETE FROM plant_tags WHERE plant_id = ? AND tag_id = ?", (plant_id, tag_id))
        db.commit()
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
    ensure_column(db, "users", "photo_id_provider", "TEXT")
    ensure_column(db, "users", "location", "TEXT")
    ensure_column(db, "garden_shares", "confirmed", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "garden_shares", "requested_by", "INTEGER")
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
    _VALID_PLANT_FORMS = {"tree","shrub","perennial","annual","climber","ground-cover","grass","fern","bulb","succulent","herb","bamboo"}
    pf = (details.get("plant_form") or "").strip().lower()
    if pf in _VALID_PLANT_FORMS:
        form_values["plant_form"] = pf
    hc = (details.get("height_category") or "").strip().lower()
    if hc in {"low","medium","tall","large"}:
        form_values["height_category"] = hc
    if details.get("description"):
        form_values["description"] = details["description"]
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
