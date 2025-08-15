from flask import Flask, render_template, request, redirect, url_for, Response
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
ABSENDER_PASSWORT = "Haesler4313!"  # ❗ In Produktion unbedingt .env nutzen
EMPFÄNGER_EMAIL = "service@haesler-ag.ch"

# 🔧 Flask App
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///lager.db")
app.config['UPLOAD_FOLDER'] = 'static/barcodes'
db = SQLAlchemy(app)

# 🔧 Spalte "lagerplatz"
with app.app_context():
    try:
        db.session.execute(text("ALTER TABLE artikel ADD COLUMN IF NOT EXISTS lagerplatz VARCHAR(100);"))
        db.session.commit()
        print("✅ Spalte 'lagerplatz' vorhanden oder hinzugefügt.")
    except Exception as e:
        print("⚠️ Fehler bei 'lagerplatz':", e)

# 🔧 Spalte "bestelllink"
with app.app_context():
    try:
        db.session.execute(text("ALTER TABLE artikel ADD COLUMN IF NOT EXISTS bestelllink VARCHAR(300);"))
        db.session.commit()
        print("✅ Spalte 'bestelllink' vorhanden oder hinzugefügt.")
    except Exception as e:
        print("⚠️ Fehler bei 'bestelllink':", e)

# 🔧 Spalte "created_at" (PostgreSQL-kompatibel)
with app.app_context():
    try:
        db.session.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='artikel' AND column_name='created_at'
                ) THEN
                    ALTER TABLE artikel ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
                END IF;
            END;
            $$;
        """))
        db.session.commit()
        print("✅ Spalte 'created_at' vorhanden oder hinzugefügt.")
    except Exception as e:
        print("⚠️ Fehler bei 'created_at':", e)

# 🔧 QR-Code erzeugen, falls nicht vorhanden
def ensure_barcode_image(barcode_id):
    path = os.path.join(app.config['UPLOAD_FOLDER'], f"{barcode_id}.png")
    if not os.path.exists(path):
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=2
        )
        qr.add_data(barcode_id)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(path)

# 🔧 Mail bei Mindestbestand senden
def sende_warnung(artikel):
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

# 🔧 Datenmodell
class Artikel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bestand = db.Column(db.Integer, nullable=False, default=0)
    mindestbestand = db.Column(db.Integer, nullable=False, default=0)
    barcode_filename = db.Column(db.String(100), nullable=False)
    lagerplatz = db.Column(db.String(100), nullable=True)
    bestelllink = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# 🔧 Startseite
@app.route('/')
def index():
    artikel = Artikel.query.order_by(Artikel.name.asc()).all()
    for art in artikel:
        barcode_id = art.barcode_filename[:-4]
        ensure_barcode_image(barcode_id)
    return render_template('index.html', artikel=artikel)

# 🔧 Artikel hinzufügen
@app.route('/add', methods=['GET', 'POST'])
def add():
    if request.method == 'POST':
        name = request.form['name']
        bestand = int(request.form['bestand'])
        mindestbestand = int(request.form['mindestbestand'])
        lagerplatz = request.form.get('lagerplatz', '')
        bestelllink = request.form.get('bestelllink', '')

        barcode_id = str(uuid.uuid4())[:8]
        barcode_filename = f"{barcode_id}.png"
        ensure_barcode_image(barcode_id)

        artikel = Artikel(
            name=name,
            bestand=bestand,
            mindestbestand=mindestbestand,
            lagerplatz=lagerplatz,
            barcode_filename=barcode_filename,
            bestelllink=bestelllink
        )
        db.session.add(artikel)
        db.session.commit()
        return redirect(url_for('index'))
    return render_template('add.html')

# 🔧 Artikel bearbeiten
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

# 🔧 Bestand anpassen
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

# 🔧 Artikel löschen
@app.route('/delete/<int:id>', methods=['POST'])
def delete(id):
    artikel = Artikel.query.get_or_404(id)
    db.session.delete(artikel)
    db.session.commit()
    return redirect(url_for('index'))

# 🔧 Scan-Seite
@app.route('/scan')
def scan():
    return render_template('scan.html')

# 🔧 Barcode-Anpassung
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

# 🔧 Etiketten anzeigen mit Filter „neu“ & Suchfunktion
@app.route('/barcodes')
def barcodes():
    query = request.args.get("q", "").strip().lower()
    alle_artikel = Artikel.query.order_by(Artikel.name.asc()).all()

    if query:
        gefiltert = [a for a in alle_artikel if query in a.name.lower()]
    else:
        gefiltert = alle_artikel

    eine_woche = datetime.utcnow() - timedelta(days=7)
    neue_artikel = Artikel.query.filter(Artikel.created_at >= eine_woche).order_by(Artikel.created_at.desc()).all()

    for art in gefiltert:
        barcode_id = art.barcode_filename[:-4]
        ensure_barcode_image(barcode_id)

    return render_template('barcodes.html', artikel=gefiltert, neue_artikel=neue_artikel, suchbegriff=query)

# 🔧 Starten
if __name__ == '__main__':
    os.makedirs('static/barcodes', exist_ok=True)
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
