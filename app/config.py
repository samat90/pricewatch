"""Конфигурация приложения."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'prices.db'}"

APP_NAME = "PriceWatch"
APP_DESCRIPTION = "Онлайн-платформа анализа цен на продукты питания"
APP_VERSION = "1.0.0"

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"

DEFAULT_CURRENCY = "KGS"
HISTORY_DAYS = 60
