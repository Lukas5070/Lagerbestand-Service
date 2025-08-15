from flask import Flask, render_template, request, redirect, url_for, Response, send_from_directory
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

# Bildabruf
import requests
from bs4 import BeautifulSoup

# 🔧 Mailkonfiguration
ABSENDER_EMAIL = "lager.servicefrick@gmail.com"
ABSENDER_PASSWORT = "Haesler4313!"  # ❗ In Produktion .env verwenden
EMPFÄNGER_EMAIL = "service@haesler-ag.ch"

# 🔧 Flask App + Render-DB-URL Fix (postgres:// → postgresql://)
app = Flask(__name__)
_db_url = os.environ.get("DATABASE_URL", "sqlite:///lager.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/barcodes'
app.config['PRODUCT_IMG_FOLDER'] = 'static/product_images'
db = SQLAlchemy(app)

# Ordner anlegen
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PRODUCT_IMG_FOLDER'], exist_ok=True)

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
    image_filename = db.Column(db.String(200), nullable=True)  # lokal gecachtes Händlerbild

# ========= DB-Spalten sicher anlegen (PG & SQLite) =========
def ensure_column(table: str, column: str, ddl_pg: str, ddl_sqlite: str):
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
        print(f"⚠️ Fehler bei Spalte '{column}':", e)

with app.app_context():
    db.create_all()
    ensure_column("artikel", "lagerplatz", "lagerplatz VARCHAR(100)", "lagerplatz VARCHAR(100)")
    ensure_column("artikel", "bestelllink", "bestelllink VARCHAR(300)", "bestelllink VARCHAR(300)")
    ensure_column("artikel", "created_at", "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                  "created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)")
    ensure_column("artikel", "image_filename", "image_filename VARCHAR(200)", "image_filename VARCHAR(200)")

# ========= QR-Code erzeugen =========
def ensure_barcode_image(barcode_id):
    path = os.path.join(app.config['UPLOAD_FOLDER'], f"{barcode_id}.png")
    if not os.path.exists(path):
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
        qr.add_data(barcode_id)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(path)

# ========= Mail bei Mindestbestand =========
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

# ========= Bild-Scraper & Cache =========
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
}

def _guess_ext_from_mime(mime: str) -> str:
    if not mime: return ".jpg"
    if "jpeg" in mime: return ".jpg"
    if "png" in mime: return ".png"
    if "webp" in mime: return ".webp"
    if "gif" in mime: return ".gif"
    return mimetypes.guess_extension(mime) or ".jpg"

def _pick_image_url(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    # OG Varianten
    for prop in ["og:image", "og:image:url", "og:image:secure_url"]:
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            return urljoin(page_url, tag["content"].strip())
    # Twitter
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return urljoin(page_url, tw["content"].strip())
    # link rel=image_src
    link_img = soup.find("link", rel=lambda v: v and "image_src" in v)
    if link_img and link_img.get("href"):
        return urljoin(page_url, link_img["href"].strip())
    # erstes sinnvolles <img>
    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src: continue
        low = src.lower()
        if any(x in low for x in ["logo", "icon", "sprite", "placeholder", "spinner"]):
            continue
        return urljoin(page_url, src)
    return None

def fetch_and_cache_product_image(artikel: Artikel) -> bool:
    if not artikel.bestelllink:
        return False
    try:
        resp = requests.get(artikel.bestelllink, headers=HEADERS, timeout=7)
        if resp.status_code >= 400:
            return False
        html = resp.text
    except Exception as e:
        print("Bild: Fehler beim Laden der Bestellseite:", e)
        return False

    img_url = _pick_image_url(html, artikel.bestelllink)
    if not img_url:
        return False

    scheme = urlparse(img_url).scheme
    if scheme not in ("http", "https"):
        return False

    try:
        # Einige Shops verlangen einen Referer
        hdr = dict(HEADERS)
        hdr["Referer"] = artikel.bestelllink
        img_resp = requests.get(img_url, headers=hdr, stream=True, timeout=10)
        ctype = img_resp.headers.get("Content-Type", "")
        if img_resp.status_code >= 400 or not ctype.startswith("image/"):
            return False

        cl = img_resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > 6_000_000:
            return False

        ext = _guess_ext_from_mime(ctype)
        fname = f"{artikel.id}{ext}"
        out_path = os.path.join(app.config['PRODUCT_IMG_FOLDER'], fname)
        with open(out_path, "wb") as f:
            for chunk in img_resp.iter_content(chunk_size=8192):
                if not chunk: continue
                f.write(chunk)

        artikel.image_filename = fname
        db.session.commit()
        return True
    except Exception as e:
        print("Bild: Fehler beim Laden/Speichern:", e)
        return False

def ensure_product_image_cached(artikel: Artikel):
    if artikel.image_filename:
        path = os.path.join(app.config['PRODUCT_IMG_FOLDER'], artikel.image_filename)
        if os.path.exists(path):
            return
    fetch_and_cache_product_image(artikel)

# ========= Routes =========
@app.route('/')
def index():
    artikel = Artikel.query.order_by(Artikel.name.asc()).all()
    for art in artikel:
        ensure_barcode_image(art.barcode_filename[:-4])
    # Nur als Komfort: ein bisschen Pre-Caching (optional)
    for art in artikel[:20]:
        ensure_product_image_cached(art)
    return render_template('index.html', artikel=artikel)

# On-Demand Produktbild (liefert Händlerbild oder QR-Fallback)
@app.route('/product_image/<int:id>')
def product_image(id):
    art = Artikel.query.get_or_404(id)

    # falls bereits gecached
    if art.image_filename:
        f = os.path.join(app.config['PRODUCT_IMG_FOLDER'], art.image_filename)
        if os.path.exists(f):
            return send_from_directory(app.config['PRODUCT_IMG_FOLDER'], art.image_filename)

    # sonst jetzt versuchen zu cachen
    try:
        if fetch_and_cache_product_image(art) and art.image_filename:
            return send_from_directory(app.config['PRODUCT_IMG_FOLDER'], art.image_filename)
    except Exception as e:
        print("On-demand Bildfehler:", e)

    # Fallback: QR
    return send_from_directory(app.config['UPLOAD_FOLDER'], art.barcode_filename)

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
            name=name, bestand=bestand, mindestbestand=mindestbestand,
            lagerplatz=lagerplatz, barcode_filename=f"{barcode_id}.png",
            bestelllink=bestelllink
        )
        db.session.add(artikel)
        db.session.commit()

        # locker versuchen zu cachen
        try:
            fetch_and_cache_product_image(artikel)
        except Exception:
            pass

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

        if artikel.bestelllink and not artikel.image_filename:
            try:
                fetch_and_cache_product_image(artikel)
            except Exception:
                pass

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
    if artikel.image_filename:
        p = os.path.join(app.config['PRODUCT_IMG_FOLDER'], artikel.image_filename)
        try:
            if os.path.exists(p):
                os.remove(p)
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
        return redirect(url_for('index'))
    return render_template('adjust.html', artikel=artikel)

@app.route('/barcodes')
def barcodes():
    query = request.args.get("q", "").strip().lower()
    alle = Artikel.query.order_by(Artikel.name.asc()).all()
    eine_woche = datetime.utcnow() - timedelta(days=7)
    try:
        neue = Artikel.query.filter(Artikel.created_at >= eine_woche)\
                            .order_by(Artikel.created_at.desc()).all()
    except Exception:
        neue = Artikel.query.order_by(Artikel.id.desc()).limit(5).all()

    gefiltert = [a for a in alle if (not query or query in a.name.lower())]

    for art in gefiltert:
        ensure_barcode_image(art.barcode_filename[:-4])

    return render_template('barcodes.html', artikel=gefiltert, neue_artikel=neue, suchbegriff=query)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
