from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import os
import uuid
import barcode
from barcode.writer import ImageWriter

app = Flask(__name__)

# ✅ Verbindet mit PostgreSQL, wenn DATABASE_URL in Render gesetzt ist
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///lager.db")

# Barcode-Bildpfad
app.config['UPLOAD_FOLDER'] = 'static/barcodes'
db = SQLAlchemy(app)

# Artikel-Modell
class Artikel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bestand = db.Column(db.Integer, nullable=False, default=0)
    mindestbestand = db.Column(db.Integer, nullable=False, default=0)
    barcode_filename = db.Column(db.String(100), nullable=False)

# Startseite
@app.route('/')
def index():
    artikel = Artikel.query.all()
    return render_template('index.html', artikel=artikel)

# Artikel hinzufügen
@app.route('/add', methods=['GET', 'POST'])
def add():
    if request.method == 'POST':
        name = request.form['name']
        bestand = int(request.form['bestand'])
        mindestbestand = int(request.form['mindestbestand'])

        barcode_id = str(uuid.uuid4())[:8]
        barcode_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{barcode_id}.png")
        ean = barcode.get('code128', barcode_id, writer=ImageWriter())
        ean.save(barcode_path[:-4])

        artikel = Artikel(
            name=name,
            bestand=bestand,
            mindestbestand=mindestbestand,
            barcode_filename=f"{barcode_id}.png"
        )
        db.session.add(artikel)
        db.session.commit()
        return redirect(url_for('index'))
    return render_template('add.html')

# Artikelbestand bearbeiten
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

# Artikel löschen
@app.route('/delete/<int:id>', methods=['POST'])
def delete(id):
    artikel = Artikel.query.get_or_404(id)
    db.session.delete(artikel)
    db.session.commit()
    return redirect(url_for('index'))

# Kamera-Scan-Seite
@app.route('/scan')
def scan():
    return render_template('scan.html')

# Barcode-basiertes Anpassen (Ein-/Ausbuchen)
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

# Barcode-Druckseite
@app.route('/barcodes')
def barcodes():
    artikel = Artikel.query.all()
    return render_template('barcodes.html', artikel=artikel)

# Hauptstart der App
if __name__ == '__main__':
    os.makedirs('static/barcodes', exist_ok=True)
    with app.app_context():
        db.create_all()  # Erstellt Tabellen beim ersten Start
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
