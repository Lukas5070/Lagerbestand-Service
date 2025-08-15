from flask import Flask, render_template, request, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
import os
import uuid
import qrcode
import smtplib
from email.mime.text import MIMEText
from sqlalchemy import text
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
import mimetypes

# ▼ Neu: für Bildabruf
import requests
from bs4 import BeautifulSoup

# 🔧 Mailkonfiguration
ABSENDER_EMAIL = "lager.servicefrick@gmail.com"
ABSENDER_PASSWORT = "Haesler4313!"  # ❗ In Produktion .env verwenden
EMPFÄNGER_EMAIL = "service@haesler-ag.ch"

# 🔧 Flask App
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///lager.db")
app.config['UPLOAD_FOLDER'] = 'static/barcodes'
app.config['PRODUCT_IMG_FOLDER'] = 'static/product_images'
db = SQLAlchemy(app)

# 🗂️ Verzeichnisse anlegen
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PRODUCT_IMG_FOLDER'], exist_ok=True)

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

# 🔧 Spalte "image_filename" (lokaler Cache des Händlerbilds)
with app.app_context():
    try:
        db.session.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='artikel' AND column_name='image_filename'
                ) THEN
                    ALTER TABLE artikel ADD COLUMN image_filename VARCHAR(200);
                END IF;
            END;
            $$;
        """))
        db.session.commit()
        print("✅ Spalte 'image_filename' vorhanden oder hinzugefügt.")
    except Exception as e:
        print("⚠️ Fehler bei 'image_filename':", e)

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

# 🔧 E-Mail bei Mindestbestand senden
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
    image_filename = db.Column(db.String(200), nullable=True)  # lokal gespeichertes Bild

# ========== 🔍 Produktbild-Ermittlung & Cache ==========

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0 Safari/537.36"
}

def _guess_ext_from_mime(mime: str) -> str:
    if not mime:
        return ".jpg"
    if "jpeg" in mime:
        return ".jpg"
    if "png" in mime:
        return ".png"
    if "webp" in mime:
        return ".webp"
    if "gif" in mime:
        return ".gif"
    return mimetypes.guess_extension(mime) or ".jpg"

def _pick_image_url(html: str, page_url: str) -> str | None:
    """Sucht bevorzugt og:image / twitter:image; fallback: erstes <img>."""
    soup = BeautifulSoup(html, "html.parser")

    # Open Graph
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return urljoin(page_url, og["content"].strip())

    # Twitter
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return urljoin(page_url, tw["content"].strip())

    # link rel=image_src
    link_img = soup.find("link", rel=lambda v: v and "image_src" in v)
    if link_img and link_img.get("href"):
        return urljoin(page_url, link_img["href"].strip())

    # erstes <img> (pragmatischer Fallback, vermeidet offensichtliche Icons/Logos)
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        # kleine Heuristik um Logos/Icons zu vermeiden
        low = src.lower()
        if any(x in low for x in ["logo", "icon", "sprite", "placeholder", "spinner"]):
            continue
        return urljoin(page_url, src)

    return None

def fetch_and_cache_product_image(artikel: Artikel) -> bool:
    """Versucht, ein Produktbild aus bestelllink zu holen und lokal zu speichern."""
    if not artikel.bestelllink:
        return False

    # 1) Seite holen
    try:
        resp = requests.get(artikel.bestelllink, headers=HEADERS, timeout=7)
        if resp.status_code >= 400:
            return False
        html = resp.text
    except Exception as e:
        print("Bild: Fehler beim Laden der Bestellseite:", e)
        return False

    # 2) Bild-URL bestimmen
    img_url = _pick_image_url(html, artikel.bestelllink)
    if not img_url:
        return False

    # nur http/https erlauben (Sicherheit)
    scheme = urlparse(img_url).scheme
    if scheme not in ("http", "https"):
        return False

    # 3) Bild holen
    try:
        img_resp = requests.get(img_url, headers=HEADERS, stream=True, timeout=10)
        ctype = img_resp.headers.get("Content-Type", "")
        if img_resp.status_code >= 400 or not ctype.startswith("image/"):
            return False

        # maximale Größe ~6MB
        cl = img_resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > 6_000_000:
            return False

        ext = _guess_ext_from_mime(ctype)
        fname = f"{artikel.id}{ext}"
        out_path = os.path.join(app.config['PRODUCT_IMG_FOLDER'], fname)

        with open(out_path, "wb") as f:
            for chunk in img_resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                f.write(chunk)

        # in DB merken
        artikel.image_filename = fname
        db.session.commit()
        return True
    except Exception as e:
        print("Bild: Fehler beim Laden/Speichern:", e)
        return False

def ensure_product_image_cached(artikel: Artikel):
    """Stellt sicher, dass ein lokales Produktbild existiert – versucht einmalig zu laden."""
    if artikel.image_filename:
        # Datei existiert evtl. nicht mehr – prüfen:
        path = os.path.join(app.config['PRODUCT_IMG_FOLDER'], artikel.image_filename)
        if os.path.exists(path):
            return
    # sonst versuchen zu holen
    fetch_and_cache_product_image(artikel)

# ========== /Bild-Caching Ende ==========

# 🔧 Startseite
@app.route('/')
def index():
    artikel = Artikel.query.order_by(Artikel.name.asc()).all()

    # QR-Bilder sicherstellen
    for art in artikel:
        barcode_id = art.barcode_filename[:-4]
        ensure_barcode_image(barcode_id)

    # Optional: Für die ersten N Artikel Bildcache anstoßen (um Laden nicht zu blockieren)
    # Du kannst N erhöhen, wenn es schnell genug ist.
    for art in artikel[:20]:
        ensure_product_image_cached(art)

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
        db.session.commit()  # ID nötig für Dateiname

        # nach dem Anlegen direkt versuchen, Produktbild zu holen (non-blocking genug durch timeouts)
        try:
            fetch_and_cache_product_image(artikel)
        except Exception as e:
            print("Bild beim Anlegen nicht geladen:", e)

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

        # Wenn Bestelllink geändert wurde und noch kein Bild vorhanden: nochmal versuchen
        if artikel.bestelllink and not artikel.image_filename:
            try:
                fetch_and_cache_product_image(artikel)
            except Exception as e:
                print("Bild nach Edit nicht geladen:", e)

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
    # optional: Bilddatei löschen
    if artikel.image_filename:
        path = os.path.join(app.config['PRODUCT_IMG_FOLDER'], artikel.image_filename)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
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

    # optional: Bildcache für sichtbare Artikel anstoßen (kleines Kontingent)
    for art in alle_artikel[:20]:
        ensure_product_image_cached(art)

    if query:
        gefiltert = [a for a in alle_artikel if query in a.name.lower()]
    else:
        gefiltert = alle_artikel

    eine_woche = datetime.utcnow() - timedelta(days=7)
    neue_artikel = Artikel.query.filter(Artikel.created_at >= eine_woche).order_by(Artikel.created_at.desc()).all()

    # QR sicherstellen
    for art in gefiltert:
        barcode_id = art.barcode_filename[:-4]
        ensure_barcode_image(barcode_id)

    return render_template('barcodes.html', artikel=gefiltert, neue_artikel=neue_artikel, suchbegriff=query)

# 🔧 Starten
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
