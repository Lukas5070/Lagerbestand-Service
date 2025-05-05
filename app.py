from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import os
import uuid
import qrcode  # ✅ QR-Code statt Code128

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///lager.db")
app.config['UPLOAD_FOLDER'] = 'static/barcodes'
db = SQLAlchemy(app)

# ✅ Neue QR-Code-Funktion
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

# Artikel-Modell
class Artikel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bestand = db.Column(db.Integer, nullable=False, default=0)
    mindestbestand = db.Column(db.Integer, nullable=False, default=0)
    barcode_filename = db.Column(db.String(100), nullable=False)

@app.route('/')
def index():
    artikel = Artikel.query.all()
    for art in artikel:
        barcode_id = art.barcode_filename[:-4]
        ensure_barcode_image(barcode_id)
    return render_template('index.html', artikel=artikel)

@app.route('/add', methods=['GET', 'POST'])
def add():
    if request.method == 'POST':
        name = request.form['name']
        bestand = int(request.form['bestand'])
        mindestbestand = int(request.form['mindestbestand'])

        barcode_id = str(uuid.uuid4())[:8]
        barcode_filename = f"{barcode_id}.png"
        ensure_barcode_image(barcode_id)

        artikel = Artikel(
            name=name,
            bestand=bestand,
            mindestbestand=mindestbestand,
            barcode_filename=barcode_filename
        )
        db.session.add(artikel)
        db.session.commit()
        return redirect(url_for('index'))
    return render_template('add.html')

@app.route('/update/<int:id>', methods=['GET', 'POST'])
def update(id):
    artikel = Artikel.query.get_or_404(id)
    if request.method == 'POST':
        try:
            delta = int(request.form['delta'])
            artikel.bestand += delta
            db.session.commit()
        except:
            return "Fehler beim Aktualisieren"
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
        return redirect(url_for('index'))
    return render_template('adjust.html', artikel=artikel)

@app.route('/barcodes')
def barcodes():
    artikel = Artikel.query.all()
    for art in artikel:
        barcode_id = art.barcode_filename[:-4]
        ensure_barcode_image(barcode_id)
    return render_template('barcodes.html', artikel=artikel)

if __name__ == '__main__':
    os.makedirs('static/barcodes', exist_ok=True)
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
