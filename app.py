from __future__ import annotations

import csv
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import qrcode
from flask import (
    Flask,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

from config import Config


BASE_DIR = Path(__file__).resolve().parent
WHITESPACE_RE = re.compile(r"\s+")
BARCODE_PRESETS = {
    "recent": "Zuletzt hinzugefügt",
    "last7": "Letzte 7 Tage",
    "last30": "Letzte 30 Tage",
    "custom": "Benutzerdefinierter Zeitraum",
    "all": "Alle Artikel",
}

db = SQLAlchemy()
migrate = Migrate()


class Artikel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bestand = db.Column(db.Integer, nullable=False, default=0)
    mindestbestand = db.Column(db.Integer, nullable=False, default=0)
    barcode_filename = db.Column(db.String(100), nullable=False)
    lagerplatz = db.Column(db.String(100), nullable=True)
    bestelllink = db.Column(db.String(300), nullable=True)
    hinweis = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def barcode_id(self) -> str:
        return barcode_id_from_filename(self.barcode_filename)

    @property
    def status(self) -> str:
        if self.bestand < self.mindestbestand:
            return "kritisch"
        if self.bestand == self.mindestbestand:
            return "knapp"
        return "ausreichend"


def compact_whitespace(value: str | None) -> str:
    return WHITESPACE_RE.sub(" ", (value or "").strip())


def normalize_article_name(value: str | None) -> str:
    return compact_whitespace(value).lower()


def barcode_id_from_filename(filename: str | None) -> str:
    if not filename:
        return ""
    return filename[:-4] if filename.endswith(".png") else filename


def ensure_column(table: str, column: str, ddl_pg: str, ddl_sqlite: str) -> None:
    dialect = db.engine.dialect.name
    try:
        if dialect == "postgresql":
            db.session.execute(
                text(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='{table}' AND column_name='{column}'
                        ) THEN
                            ALTER TABLE {table} ADD COLUMN {ddl_pg};
                        END IF;
                    END;
                    $$;
                    """
                )
            )
        else:
            cols = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
            names = [row[1] for row in cols]
            if column not in names:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl_sqlite};"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def bootstrap_database() -> None:
    db.create_all()
    ensure_column("artikel", "lagerplatz", "lagerplatz VARCHAR(100)", "lagerplatz VARCHAR(100)")
    ensure_column("artikel", "bestelllink", "bestelllink VARCHAR(300)", "bestelllink VARCHAR(300)")
    ensure_column("artikel", "hinweis", "hinweis TEXT", "hinweis TEXT")
    ensure_column(
        "artikel",
        "created_at",
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)",
    )


def ensure_barcode_image(barcode_id: str) -> None:
    upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
    upload_folder.mkdir(parents=True, exist_ok=True)
    path = upload_folder / f"{barcode_id}.png"
    if path.exists():
        return

    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(barcode_id)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(path)


def build_article_form_data(artikel: Artikel | None = None, source: dict | None = None) -> dict[str, str]:
    if source is not None:
        return {
            "name": (source.get("name") or "").strip(),
            "bestand": (source.get("bestand") or "").strip(),
            "mindestbestand": (source.get("mindestbestand") or "").strip(),
            "lagerplatz": (source.get("lagerplatz") or "").strip(),
            "bestelllink": (source.get("bestelllink") or "").strip(),
            "hinweis": (source.get("hinweis") or "").strip(),
        }

    if artikel is None:
        return {
            "name": "",
            "bestand": "",
            "mindestbestand": "",
            "lagerplatz": "",
            "bestelllink": "",
            "hinweis": "",
        }

    return {
        "name": artikel.name or "",
        "bestand": str(artikel.bestand),
        "mindestbestand": str(artikel.mindestbestand),
        "lagerplatz": artikel.lagerplatz or "",
        "bestelllink": artikel.bestelllink or "",
        "hinweis": artikel.hinweis or "",
    }


def parse_article_payload(form_data: dict[str, str]) -> dict[str, int | str]:
    return {
        "name": form_data["name"],
        "bestand": int(form_data["bestand"]),
        "mindestbestand": int(form_data["mindestbestand"]),
        "lagerplatz": form_data["lagerplatz"],
        "bestelllink": form_data["bestelllink"],
        "hinweis": form_data["hinweis"],
    }


def get_duplicate_matches(name: str, exclude_id: int | None = None) -> list[Artikel]:
    normalized_name = normalize_article_name(name)
    if not normalized_name:
        return []

    matches: list[Artikel] = []
    kandidaten = Artikel.query.order_by(Artikel.created_at.desc(), Artikel.id.desc()).all()
    for kandidat in kandidaten:
        if exclude_id is not None and kandidat.id == exclude_id:
            continue
        if normalize_article_name(kandidat.name) == normalized_name:
            matches.append(kandidat)
    return matches


def build_duplicate_groups() -> list[dict]:
    groups: dict[str, list[Artikel]] = defaultdict(list)
    artikel = Artikel.query.order_by(Artikel.name.asc(), Artikel.id.asc()).all()
    for item in artikel:
        normalized_name = normalize_article_name(item.name)
        if normalized_name:
            groups[normalized_name].append(item)

    duplicate_groups: list[dict] = []
    for normalized_name, items in groups.items():
        if len(items) < 2:
            continue
        variant_names = sorted({compact_whitespace(item.name) for item in items})
        duplicate_groups.append(
            {
                "normalized_name": normalized_name,
                "display_name": variant_names[0] if variant_names else items[0].name,
                "variant_names": variant_names,
                "items": sorted(
                    items,
                    key=lambda item: (
                        item.created_at or datetime.min,
                        item.id,
                    ),
                    reverse=True,
                ),
                "count": len(items),
            }
        )

    duplicate_groups.sort(key=lambda group: (-group["count"], group["display_name"].lower()))
    return duplicate_groups


def parse_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if end_of_day:
        return parsed + timedelta(days=1) - timedelta(seconds=1)
    return parsed


def build_barcode_filters(args) -> dict[str, str]:
    preset = (args.get("preset") or "recent").strip().lower()
    if preset not in BARCODE_PRESETS:
        preset = "recent"

    return {
        "name": compact_whitespace(args.get("name")),
        "barcode": compact_whitespace(args.get("barcode")),
        "location": compact_whitespace(args.get("location")),
        "preset": preset,
        "date_from": (args.get("date_from") or "").strip(),
        "date_to": (args.get("date_to") or "").strip(),
    }


def query_barcodes(filters: dict[str, str]) -> tuple[list[Artikel], str]:
    query = Artikel.query

    if filters["name"]:
        query = query.filter(Artikel.name.ilike(f"%{filters['name']}%"))
    if filters["barcode"]:
        query = query.filter(Artikel.barcode_filename.ilike(f"%{filters['barcode']}%"))
    if filters["location"]:
        query = query.filter(Artikel.lagerplatz.ilike(f"%{filters['location']}%"))

    preset = filters["preset"]
    active_label = BARCODE_PRESETS[preset]

    if preset == "recent":
        artikel = query.order_by(Artikel.created_at.desc(), Artikel.id.desc()).limit(24).all()
    elif preset == "last7":
        artikel = (
            query.filter(Artikel.created_at >= datetime.utcnow() - timedelta(days=7))
            .order_by(Artikel.created_at.desc(), Artikel.id.desc())
            .all()
        )
    elif preset == "last30":
        artikel = (
            query.filter(Artikel.created_at >= datetime.utcnow() - timedelta(days=30))
            .order_by(Artikel.created_at.desc(), Artikel.id.desc())
            .all()
        )
    elif preset == "custom":
        date_from = parse_date(filters["date_from"])
        date_to = parse_date(filters["date_to"], end_of_day=True)
        if date_from:
            query = query.filter(Artikel.created_at >= date_from)
        if date_to:
            query = query.filter(Artikel.created_at <= date_to)
        artikel = query.order_by(Artikel.created_at.desc(), Artikel.id.desc()).all()
    else:
        artikel = query.order_by(Artikel.name.asc(), Artikel.id.asc()).all()

    for item in artikel:
        ensure_barcode_image(item.barcode_id)

    return artikel, active_label


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(BASE_DIR / "migrations"))

    @app.template_filter("datetime_display")
    def datetime_display(value: datetime | None) -> str:
        if not value:
            return "–"
        return value.strftime("%d.%m.%Y %H:%M")

    @app.context_processor
    def inject_layout_context() -> dict[str, str]:
        return {
            "app_title": app.config["APP_TITLE"],
            "company_name": app.config["COMPANY_NAME"],
        }

    @app.route("/")
    def index():
        artikel = Artikel.query.order_by(Artikel.name.asc(), Artikel.id.asc()).all()
        for item in artikel:
            ensure_barcode_image(item.barcode_id)

        summary = {
            "total": len(artikel),
            "critical": sum(1 for item in artikel if item.bestand < item.mindestbestand),
            "low": sum(1 for item in artikel if item.bestand == item.mindestbestand),
        }
        duplicate_groups = build_duplicate_groups()
        return render_template(
            "index.html",
            artikel=artikel,
            summary=summary,
            duplicate_group_count=len(duplicate_groups),
            duplicate_article_count=sum(group["count"] for group in duplicate_groups),
        )

    @app.route("/add", methods=["GET", "POST"])
    def add():
        form_data = build_article_form_data()
        duplicate_matches: list[Artikel] = []
        requires_duplicate_confirmation = False

        if request.method == "POST":
            form_data = build_article_form_data(source=request.form)
            duplicate_matches = get_duplicate_matches(form_data["name"])
            requires_duplicate_confirmation = (
                bool(duplicate_matches) and request.form.get("confirm_duplicate") != "1"
            )

            if requires_duplicate_confirmation:
                flash(
                    "Es gibt bereits Artikel mit demselben normalisierten Namen. "
                    "Du kannst trotzdem speichern.",
                    "warning",
                )
            else:
                try:
                    payload = parse_article_payload(form_data)
                except ValueError:
                    flash("Bitte trage Bestand und Mindestbestand als ganze Zahlen ein.", "error")
                else:
                    barcode_id = str(uuid.uuid4())[:8]
                    ensure_barcode_image(barcode_id)

                    artikel = Artikel(
                        name=payload["name"],
                        bestand=payload["bestand"],
                        mindestbestand=payload["mindestbestand"],
                        lagerplatz=payload["lagerplatz"],
                        bestelllink=payload["bestelllink"],
                        hinweis=payload["hinweis"],
                        barcode_filename=f"{barcode_id}.png",
                    )
                    db.session.add(artikel)
                    db.session.commit()
                    flash("Artikel wurde gespeichert.", "success")
                    if duplicate_matches:
                        flash("Artikel wurde trotz möglicher Dublette übernommen.", "warning")
                    return redirect(url_for("index") + f"#art-{artikel.id}")

        return render_template(
            "add.html",
            form_data=form_data,
            duplicate_matches=duplicate_matches,
            requires_duplicate_confirmation=requires_duplicate_confirmation,
            artikel=None,
        )

    @app.route("/edit/<int:id>", methods=["GET", "POST"])
    def edit(id: int):
        artikel = Artikel.query.get_or_404(id)
        form_data = build_article_form_data(artikel)
        duplicate_matches = get_duplicate_matches(artikel.name, exclude_id=artikel.id)
        requires_duplicate_confirmation = False

        if request.method == "POST":
            form_data = build_article_form_data(source=request.form)
            duplicate_matches = get_duplicate_matches(form_data["name"], exclude_id=artikel.id)
            requires_duplicate_confirmation = (
                bool(duplicate_matches) and request.form.get("confirm_duplicate") != "1"
            )

            if requires_duplicate_confirmation:
                flash(
                    "Es gibt bereits weitere Artikel mit demselben normalisierten Namen. "
                    "Du kannst die Änderung trotzdem speichern.",
                    "warning",
                )
            else:
                try:
                    payload = parse_article_payload(form_data)
                except ValueError:
                    flash("Bitte trage Bestand und Mindestbestand als ganze Zahlen ein.", "error")
                else:
                    artikel.name = payload["name"]
                    artikel.bestand = payload["bestand"]
                    artikel.mindestbestand = payload["mindestbestand"]
                    artikel.lagerplatz = payload["lagerplatz"]
                    artikel.bestelllink = payload["bestelllink"]
                    artikel.hinweis = payload["hinweis"]
                    db.session.commit()
                    flash("Artikel wurde aktualisiert.", "success")
                    if duplicate_matches:
                        flash("Die Änderung wurde trotz möglicher Dublette gespeichert.", "warning")
                    return redirect(url_for("index") + f"#art-{artikel.id}")

        return render_template(
            "edit.html",
            artikel=artikel,
            form_data=form_data,
            duplicate_matches=duplicate_matches,
            requires_duplicate_confirmation=requires_duplicate_confirmation,
        )

    @app.route("/update/<int:id>", methods=["GET", "POST"])
    def update(id: int):
        artikel = Artikel.query.get_or_404(id)
        if request.method == "POST":
            try:
                delta = int((request.form.get("delta") or "").strip())
            except ValueError:
                flash("Bitte trage eine ganze Zahl ein, z. B. 5 oder -2.", "error")
            else:
                artikel.bestand += delta
                db.session.commit()
                flash("Bestand wurde angepasst.", "success")
                return redirect(url_for("index") + f"#art-{artikel.id}")
        return render_template("update.html", artikel=artikel)

    @app.route("/delete/<int:id>", methods=["POST"])
    def delete(id: int):
        artikel = Artikel.query.get_or_404(id)
        barcode_path = Path(app.config["UPLOAD_FOLDER"]) / artikel.barcode_filename
        if barcode_path.exists():
            try:
                barcode_path.unlink()
            except OSError:
                pass

        db.session.delete(artikel)
        db.session.commit()
        flash("Artikel wurde gelöscht.", "success")
        return redirect(url_for("index"))

    @app.route("/scan")
    def scan():
        return render_template("scan.html")

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    @app.route("/adjust_barcode/<barcode_id>", methods=["GET", "POST"])
    def adjust_barcode(barcode_id: str):
        artikel = Artikel.query.filter(Artikel.barcode_filename == f"{barcode_id}.png").first()
        if not artikel:
            return "Artikel nicht gefunden", 404

        if request.method == "POST":
            try:
                menge = int((request.form.get("menge") or "").strip())
            except ValueError:
                flash("Bitte trage eine ganze Zahl ein.", "error")
                return render_template("adjust.html", artikel=artikel)

            aktion = request.form.get("aktion")
            if aktion == "hinzufügen":
                artikel.bestand += menge
            elif aktion == "entnehmen":
                artikel.bestand -= menge

            db.session.commit()
            flash("Bestand wurde über den Barcode angepasst.", "success")
            return redirect(url_for("index") + f"#art-{artikel.id}")

        return render_template("adjust.html", artikel=artikel)

    @app.route("/barcodes")
    def barcodes():
        filters = build_barcode_filters(request.args)
        artikel, active_label = query_barcodes(filters)
        return render_template(
            "barcodes.html",
            artikel=artikel,
            filters=filters,
            preset_options=BARCODE_PRESETS,
            active_label=active_label,
        )

    @app.route("/duplicates")
    @app.route("/dubletten")
    def duplicates():
        duplicate_groups = build_duplicate_groups()
        return render_template(
            "duplicates.html",
            duplicate_groups=duplicate_groups,
            duplicate_group_count=len(duplicate_groups),
            duplicate_article_count=sum(group["count"] for group in duplicate_groups),
        )

    @app.route("/export.csv")
    def export_csv():
        sep = (request.args.get("sep", "semicolon") or "semicolon").lower()
        delimiter = ";" if sep in ("semicolon", ";", "sc") else ","

        encoding = (request.args.get("encoding", "utf-8") or "utf-8").lower()
        add_bom = (request.args.get("bom", "1") in ("1", "true", "yes")) if encoding.startswith("utf") else False

        cols = [
            ("id", lambda a: a.id),
            ("name", lambda a: a.name or ""),
            ("bestand", lambda a: a.bestand),
            ("mindestbestand", lambda a: a.mindestbestand),
            ("lagerplatz", lambda a: a.lagerplatz or ""),
            ("bestelllink", lambda a: a.bestelllink or ""),
            ("hinweis", lambda a: (a.hinweis or "").replace("\n", " ").strip()),
            ("barcode_id", lambda a: a.barcode_id),
            ("barcode_filename", lambda a: a.barcode_filename or ""),
            (
                "created_at",
                lambda a: a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
            ),
        ]

        artikel = Artikel.query.order_by(Artikel.name.asc(), Artikel.id.asc()).all()

        sio = StringIO()
        writer = csv.writer(sio, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        writer.writerow([c[0] for c in cols])
        for item in artikel:
            writer.writerow([fn(item) for _, fn in cols])

        data = sio.getvalue()
        if encoding == "cp1252":
            payload = data.encode("cp1252", errors="replace")
            content_type = "text/csv; charset=windows-1252"
        else:
            if add_bom:
                data = "\ufeff" + data
            payload = data.encode("utf-8")
            content_type = "text/csv; charset=utf-8"

        headers = {
            "Content-Disposition": "attachment; filename=artikel_export.csv",
            "Content-Type": content_type,
            "Cache-Control": "no-store",
        }
        return Response(payload, headers=headers)

    with app.app_context():
        bootstrap_database()

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
