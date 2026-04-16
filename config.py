import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
BARCODE_DIR = BASE_DIR / "static" / "barcodes"


def normalize_database_url(raw_url: str | None) -> str:
    database_url = raw_url or f"sqlite:///{INSTANCE_DIR / 'lager.db'}"
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql://", 1)
    return database_url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "local-dev-secret-key")
    COMPANY_NAME = os.getenv("COMPANY_NAME", "Musterfirma")
    APP_TITLE = os.getenv("APP_TITLE", "Lagerverwaltung")
    SQLALCHEMY_DATABASE_URI = normalize_database_url(os.getenv("DATABASE_URL"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = str(BARCODE_DIR)
