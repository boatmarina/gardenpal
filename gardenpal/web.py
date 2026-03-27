import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DEFAULT_CATEGORIES = ["Love this", "Front porch", "Backyard", "Wishlist", "Pollinator friendly"]


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    running_on_vercel = bool(os.environ.get("VERCEL"))
    data_dir = Path("/tmp/gardenpal") if running_on_vercel else Path(app.instance_path)
    app.config.from_mapping(
        SECRET_KEY="dev",
        DATABASE=str(data_dir / "gardenpal.db"),
        UPLOAD_FOLDER=str(data_dir / "uploads"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    )

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    with app.app_context():
        init_db()

    @app.route("/")
    def index():
        db = get_db()
        q = request.args.get("q", "").strip()
        sun = request.args.get("sun", "").strip()
        lifecycle = request.args.get("lifecycle", "").strip()
        category_id = request.args.get("category", "").strip()

        query = """
            SELECT DISTINCT p.*
            FROM plants p
            LEFT JOIN plant_categories pc ON p.id = pc.plant_id
            WHERE 1=1
        """
        params = []

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

        categories = db.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
        return render_template(
            "index.html",
            plants=plants,
            categories=categories,
            active_filters={"q": q, "sun": sun, "lifecycle": lifecycle, "category": category_id},
        )

    @app.route("/plants/new", methods=["GET", "POST"])
    def new_plant():
        db = get_db()
        categories = db.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            source_type = request.form.get("source_type", "world").strip()
            source_note = request.form.get("source_note", "").strip()
            image_url = request.form.get("image_url", "").strip()
            size_info = request.form.get("size_info", "").strip()
            flowering_schedule = request.form.get("flowering_schedule", "").strip()
            sun_exposure = request.form.get("sun_exposure", "").strip()
            lifecycle = request.form.get("lifecycle", "").strip()
            notes = request.form.get("notes", "").strip()
            selected_categories = request.form.getlist("categories")
            new_categories_raw = request.form.get("new_categories", "").strip()

            if not name:
                flash("Plant name is required.")
                return render_template("new_plant.html", categories=categories)

            image_path = save_upload(request.files.get("photo"), app.config["UPLOAD_FOLDER"])
            db.execute(
                """
                INSERT INTO plants
                (name, source_type, source_note, image_path, image_url, size_info, flowering_schedule,
                 sun_exposure, lifecycle, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    source_type,
                    source_note,
                    image_path,
                    image_url,
                    size_info,
                    flowering_schedule,
                    sun_exposure,
                    lifecycle,
                    notes,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            plant_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

            created_category_ids = list(selected_categories)
            if new_categories_raw:
                for category_name in [c.strip() for c in new_categories_raw.split(",") if c.strip()]:
                    existing = db.execute(
                        "SELECT id FROM categories WHERE lower(name) = lower(?)", (category_name,)
                    ).fetchone()
                    if existing:
                        created_category_ids.append(str(existing["id"]))
                    else:
                        db.execute("INSERT INTO categories (name) VALUES (?)", (category_name,))
                        new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                        created_category_ids.append(str(new_id))

            for category in set(created_category_ids):
                db.execute(
                    "INSERT OR IGNORE INTO plant_categories (plant_id, category_id) VALUES (?, ?)",
                    (plant_id, category),
                )

            db.commit()
            flash("Plant added to your diary.")
            return redirect(url_for("plant_detail", plant_id=plant_id))

        return render_template("new_plant.html", categories=categories)

    @app.route("/plants/<int:plant_id>")
    def plant_detail(plant_id: int):
        db = get_db()
        plant = db.execute("SELECT * FROM plants WHERE id = ?", (plant_id,)).fetchone()
        if plant is None:
            flash("Plant was not found.")
            return redirect(url_for("index"))

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
        return render_template("plant_detail.html", plant=plant, categories=categories)

    @app.route("/uploads/<path:filename>")
    def uploads(filename: str):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    return app


def get_db():
    if "db" not in g:
        db = sqlite3.connect(
            current_app().config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        db.row_factory = sqlite3.Row
        g.db = db
    return g.db


def current_app():
    from flask import current_app as flask_current_app

    return flask_current_app


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS plants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'world',
            source_note TEXT,
            image_path TEXT,
            image_url TEXT,
            size_info TEXT,
            flowering_schedule TEXT,
            sun_exposure TEXT,
            lifecycle TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS plant_categories (
            plant_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY (plant_id, category_id),
            FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
            FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE CASCADE
        );
        """
    )

    for category in DEFAULT_CATEGORIES:
        db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
    db.commit()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file_storage, upload_folder: str):
    if file_storage is None or not file_storage.filename:
        return ""
    filename = secure_filename(file_storage.filename)
    if not filename or not allowed_file(filename):
        return ""

    ext = filename.rsplit(".", 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    destination = Path(upload_folder) / unique_name
    file_storage.save(destination)
    return unique_name


def run():
    app = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=True)

