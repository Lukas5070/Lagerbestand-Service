from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import os
import uuid
import qrcode
import smtplib
from email.mime.text import MIMEText
from sqlalchemy import text
from datetime import datetime, timedelta

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ========= DB-Spalten sicher anlegen (PG & SQLite) =========
def ensure_column(table: str, column: str, ddl_pg: str, ddl_sqlite: str):
    """Fügt eine Spalte hinzu, falls sie fehlt. Kompatibel mit PostgreSQL & SQLite."""
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
            names = [row[1] for row in cols]  # 0:cid, 1:name, ...
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
    for art in artikel:
        ensure_barcode_image(art.barcode_filename[:-4])
    return render_template('index.html', artikel=artikel)

@app.route('/add', methods=['GET', 'POST'])
def add():
    if request.method == 'POST':
        name = request.form['name']
        bestand = int(request.form['bestand'])
        mindestbestand = int(request.form['mindestbestand'])
        lagerplatz = request.form.get('lagerplatz', '')
        bestelllink = request.form.get('bestelllink', '')

        barcode_id = str(uuid.uuid4())[:8]
        ensure_barcode_image(barcode_id)

        artikel = Artikel(
            name=name,
            bestand=bestand,
            mindestbestand=mindestbestand,
            lagerplatz=lagerplatz,
            barcode_filename=f"{barcode_id}.png",
            bestelllink=bestelllink
        )
        db.session.add(artikel)
        db.session.commit()
        return redirect(url_for('index'))
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
        db.session.commit()
        return redirect(url_for('index'))
    return render_template('edit.html', artikel=artikel)

@app.route('/update/<int:id>', methods=['GET', 'POST'])
def update(id):
    artikel = Artikel.query.get_or_404(id)
    if request.method == 'POST':
        delta = int(request.form['delta'])
        artikel.bestand += delta
        db.session.commit()
        sende_warnung(artikel)
        return redirect(url_for('index'))
    return render_template('update.html', artikel=artikel)

@app.route('/delete/<int:id>', methods=['POST'])
def delete(id):
    artikel = Artikel.query.get_or_404(id)
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
        return redirect(url_for('index'))
    return render_template('adjust.html', artikel=artikel)

# Etiketten-Seite: „neueste“ + Suche (Server liefert Daten; Interaktion passiert im Template-JS)
@app.route('/barcodes')
def barcodes():
    query = request.args.get("q", "").strip().lower()
    alle = Artikel.query.order_by(Artikel.name.asc()).all()

    # „Neueste“: letzten 7 Tage (Fallback: jüngste 15 per ID)
    eine_woche = datetime.utcnow() - timedelta(days=7)
    try:
        neue = Artikel.query.filter(Artikel.created_at >= eine_woche)\
                            .order_by(Artikel.created_at.desc()).all()
        if not neue:
            neue = Artikel.query.order_by(Artikel.id.desc()).limit(15).all()
    except Exception:
        neue = Artikel.query.order_by(Artikel.id.desc()).limit(15).all()

    # optional serverseitige Suche (nutzt dein Template aktuell nicht zwingend)
    if query:
        gefiltert = [a for a in alle if query in a.name.lower()]
    else:
        gefiltert = alle

    for art in gefiltert:
        ensure_barcode_image(art.barcode_filename[:-4])

    return render_template('barcodes.html', artikel=gefiltert, neue_artikel=neue, suchbegriff=query)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
