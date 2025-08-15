from flask import Flask, render_template, request, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
import os
import uuid
import qrcode
import smtplib
from email.mime.text import MIMEText
from sqlalchemy import text
from datetime import datetime, timedelta
import csv
from io import StringIO

# 🔧 Mailkonfiguration
ABSENDER_EMAIL = "lager.servicefrick@gmail.com"
ABSENDER_PASSWORT = "Haesler4313!"  # ❗ In Produktion per Umgebungsvariable/.env
EMPFÄNGER_EMAIL = "service@haesler-ag.ch"

# 🔧 Flask App + Render-DB-URL Fix (postgres:// → postgresql://)
app = Flask(__name__)
_db_url = os.environ.get("DATABASE_URL", "sqlite:///lager.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/barcodes'
db = SQLAlchemy(app)

# Ordner anlegen
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ========= Datenmodell =========
class Artikel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bestand = db.Column(db.Integer, nullable=False, default=0)
    mindestbestand = db.Column(db.Integer, nullable=False, default=0)
    barcode_filename = db.Column(db.String(100), nullable=False)
    lagerplatz = db.Column(db.String(100), nullable=True)
    bestelllink = db.Column(db.String(300), nullable=True)
    hinweis = db.Column(db.Text, nullable=True)  # 🆕 Freitext-Notiz
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ========= DB-Spalten sicher anlegen (PG & SQLite) =========
def ensure_column(table: str, column: str, ddl_pg: str, ddl_sqlite: str):
    """Spalte hinzufügen, falls sie fehlt (PostgreSQL & SQLite)."""
    dialect = db.engine.dialect.name
    try:
        if dialect == "postgresql":
            db.session.execute(text(f"""
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
            """))
        else:
            cols = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
            names = [row[1] for row in cols]
            if column not in names:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl_sqlite};"))
        db.session.commit()
        print(f"✅ Spalte '{column}' vorhanden oder hinzugefügt.")
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ Fehler beim Hinzufügen der Spalte '{column}':", e)

with app.app_context():
    db.create_all()
    ensure_column("artikel", "lagerplatz",   "lagerplatz VARCHAR(100)",  "lagerplatz VARCHAR(100)")
    ensure_column("artikel", "bestelllink",  "bestelllink VARCHAR(300)", "bestelllink VARCHAR(300)")
    ensure_column("artikel", "hinweis",      "hinweis TEXT",             "hinweis TEXT")  # 🆕
    ensure_column("artikel", "created_at",
                  "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                  "created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)")

# ========= QR-Code erzeugen =========
def ensure_barcode_image(barcode_id: str):
    path = os.path.join(app.config['UPLOAD_FOLDER'], f"{barcode_id}.png")
    if not os.path.exists(path):
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
        qr.add_data(barcode_id)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(path)

# ========= Mail bei Mindestbestand =========
def sende_warnung(artikel: Artikel):
    if artikel.bestand == artikel.mindestbestand:
        nachricht = f"""Achtung: Der Artikel "{artikel.name}" hat den Mindestbestand erreicht!

Aktueller Bestand: {artikel.bestand}
Mindestbestand: {artikel.mindestbestand}
Lagerplatz: {artikel.lagerplatz or 'nicht angegeben'}"""
        msg = MIMEText(nachricht)
        msg['Subject'] = f"Lagerwarnung: {artikel.name}"
        msg['From'] = ABSENDER_EMAIL
        msg['To'] = EMPFÄNGER_EMAIL
        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(ABSENDER_EMAIL, ABSENDER_PASSWORT)
                server.send_message(msg)
        except Exception as e:
            print("Fehler beim E-Mail-Versand:", e)

# ========= Routen =========
@app.route('/')
def index():
    artikel = Artikel.query.order_by(Artikel.name.asc()).all()
    return render_template('index.html', artikel=artikel)

@app.route('/add', methods=['GET', 'POST'])
def add():
    if request.method == 'POST':
        name = request.form['name']
        bestand = int(request.form['bestand'])
        mindestbestand = int(request.form['mindestbestand'])
        lagerplatz = request.form.get('lagerplatz', '')
        bestelllink = request.form.get('bestelllink', '')
        hinweis = request.form.get('hinweis', '')  # 🆕

        barcode_id = str(uuid.uuid4())[:8]
        ensure_barcode_image(barcode_id)

        artikel = Artikel(
            name=name,
            bestand=bestand,
            mindestbestand=mindestbestand,
            lagerplatz=lagerplatz,
            barcode_filename=f"{barcode_id}.png",
            bestelllink=bestelllink,
            hinweis=hinweis
        )
        db.session.add(artikel)
        db.session.commit()
        return redirect(url_for('index') + f"#art-{artikel.id}")
    return render_template('add.html')

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit(id):
    artikel = Artikel.query.get_or_404(id)
    if request.method == 'POST':
        artikel.name = request.form['name']
        artikel.bestand = int(request.form['bestand'])
        artikel.mindestbestand = int(request.form['mindestbestand'])
        artikel.lagerplatz = request.form.get('lagerplatz', '')
        artikel.bestelllink = request.form.get('bestelllink', '')
        artikel.hinweis = request.form.get('hinweis', '')  # 🆕
        db.session.commit()
        return redirect(url_for('index') + f"#art-{artikel.id}")
    return render_template('edit.html', artikel=artikel)

@app.route('/update/<int:id>', methods=['GET', 'POST'])
def update(id):
    artikel = Artikel.query.get_or_404(id)
    if request.method == 'POST':
        delta = int(request.form['delta'])
        artikel.bestand += delta
        db.session.commit()
        sende_warnung(artikel)
        return redirect(url_for('index') + f"#art-{artikel.id}")
    return render_template('update.html', artikel=artikel)

@app.route('/delete/<int:id>', methods=['POST'])
def delete(id):
    artikel = Artikel.query.get_or_404(id)
    # Optional: QR-Datei entfernen
    try:
        pfad = os.path.join(app.config['UPLOAD_FOLDER'], artikel.barcode_filename)
        if os.path.exists(pfad):
            os.remove(pfad)
    except Exception:
        pass
    db.session.delete(artikel)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/scan')
def scan():
    return render_template('scan.html')

@app.route('/adjust_barcode/<barcode_id>', methods=['GET', 'POST'])
def adjust_barcode(barcode_id):
    artikel = Artikel.query.filter(Artikel.barcode_filename == f"{barcode_id}.png").first()
    if not artikel:
        return "Artikel nicht gefunden", 404
    if request.method == 'POST':
        menge = int(request.form['menge'])
        aktion = request.form['aktion']
        if aktion == 'hinzufügen':
            artikel.bestand += menge
        elif aktion == 'entnehmen':
            artikel.bestand -= menge
        db.session.commit()
        sende_warnung(artikel)
        return redirect(url_for('index') + f"#art-{artikel.id}")
    return render_template('adjust.html', artikel=artikel)

# Etiketten-Seite mit Suche + Datumsfilter für „neu“
@app.route('/barcodes')
def barcodes():
    q = (request.args.get("q", "") or "").strip().lower()
    date_from = (request.args.get("from", "") or "").strip()
    date_to   = (request.args.get("to", "") or "").strip()

    alle = Artikel.query.order_by(Artikel.name.asc()).all()
    artikel = [a for a in alle if q in (a.name or "").lower()] if q else alle

    neue_q = Artikel.query
    dt_from = None
    dt_to = None
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            neue_q = neue_q.filter(Artikel.created_at >= dt_from)
        except Exception:
            dt_from = None
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
            neue_q = neue_q.filter(Artikel.created_at <= dt_to)
        except Exception:
            dt_to = None

    if dt_from or dt_to:
        neue_artikel = neue_q.order_by(Artikel.created_at.desc(), Artikel.id.desc()).all()
    else:
        eine_woche = datetime.utcnow() - timedelta(days=7)
        try:
            neue_artikel = Artikel.query.filter(Artikel.created_at >= eine_woche)\
                                        .order_by(Artikel.created_at.desc()).all()
            if not neue_artikel:
                neue_artikel = Artikel.query.order_by(Artikel.id.desc()).limit(15).all()
        except Exception:
            neue_artikel = Artikel.query.order_by(Artikel.id.desc()).limit(15).all()

    for art in artikel:
        ensure_barcode_image(art.barcode_filename[:-4])
    for art in neue_artikel:
        ensure_barcode_image(art.barcode_filename[:-4])

    return render_template(
        'barcodes.html',
        artikel=artikel,
        neue_artikel=neue_artikel,
        suchbegriff=q,
        date_from=date_from,
        date_to=date_to
    )

# ========= CSV-Export =========
@app.route('/export.csv')
def export_csv():
    """
    Exportiert alle Artikel als CSV.

    Query-Parameter (optional):
      - sep=comma|semicolon|,|;      (Standard: semicolon)
      - encoding=utf-8|cp1252        (Standard: utf-8)
      - bom=0|1                      (Standard: 1 nur bei utf-8 → Excel-kompatibel)
    """
    sep = (request.args.get("sep", "semicolon") or "semicolon").lower()
    delimiter = ';' if sep in ("semicolon", ";", "sc") else ','

    encoding = (request.args.get("encoding", "utf-8") or "utf-8").lower()
    add_bom = (request.args.get("bom", "1") in ("1", "true", "yes")) if encoding.startswith("utf") else False

    cols = [
        ("id",              lambda a: a.id),
        ("name",            lambda a: a.name or ""),
        ("bestand",         lambda a: a.bestand),
        ("mindestbestand",  lambda a: a.mindestbestand),
        ("lagerplatz",      lambda a: a.lagerplatz or ""),
        ("bestelllink",     lambda a: a.bestelllink or ""),
        ("hinweis",         lambda a: (a.hinweis or "").replace("\n"," ").strip()),  # 🆕
        ("barcode_id",      lambda a: (a.barcode_filename[:-4] if a.barcode_filename else "")),
        ("barcode_filename",lambda a: a.barcode_filename or ""),
        ("created_at",      lambda a: a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else ""),
    ]

    artikel = Artikel.query.order_by(Artikel.name.asc()).all()

    sio = StringIO()
    writer = csv.writer(sio, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([c[0] for c in cols])
    for a in artikel:
        writer.writerow([fn(a) for _, fn in cols])

    data = sio.getvalue()
    if encoding == "cp1252":
        payload = data.encode("cp1252", errors="replace")
        content_type = "text/csv; charset=windows-1252"
    else:
        if add_bom:
            data = '\ufeff' + data
        payload = data.encode("utf-8")
        content_type = "text/csv; charset=utf-8"

    headers = {
        "Content-Disposition": "attachment; filename=artikel_export.csv",
        "Content-Type": content_type,
        "Cache-Control": "no-store",
    }
    return Response(payload, headers=headers)

# ========= Start =========
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
