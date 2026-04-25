"""Наполнение БД: страны, города, категории, каталог товаров, курсы валют.

После создания каталога запускается настоящий парсер magnit.ru, который
подтягивает актуальные цены на товары с сайта.

Запуск:
    python -m scripts.seed              # полный прогон (каталог + скрапинг)
    python -m scripts.seed --no-scrape  # только каталог, без скрапинга
"""
from __future__ import annotations

import io
import logging
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("seed")

from app.database import Base, SessionLocal, engine
from app.models import Category, City, Country, CurrencyRate, Product
from app.services.currency import DEFAULT_RATES
from app.services.scraper import Scraper


COUNTRIES = [
    # KG первая — это основная страна проекта
    {"code": "KG", "name": "Kyrgyzstan", "name_ru": "Кыргызстан", "currency": "KGS", "flag": "🇰🇬"},
    {"code": "RU", "name": "Russia",     "name_ru": "Россия",     "currency": "RUB", "flag": "🇷🇺"},
    {"code": "KZ", "name": "Kazakhstan", "name_ru": "Казахстан",  "currency": "KZT", "flag": "🇰🇿"},
    {"code": "UZ", "name": "Uzbekistan", "name_ru": "Узбекистан", "currency": "UZS", "flag": "🇺🇿"},
]

CITIES = [
    # KG — города проекта
    ("KG", "Ош"), ("KG", "Бишкек"), ("KG", "Джалал-Абад"), ("KG", "Каракол"),
    # Россия
    ("RU", "Москва"), ("RU", "Краснодар"), ("RU", "Новосибирск"),
    # Казахстан
    ("KZ", "Алматы"), ("KZ", "Астана"),
    # Узбекистан
    ("UZ", "Ташкент"), ("UZ", "Самарканд"), ("UZ", "Бухара"),
]

CATEGORIES = [
    ("bakery",    "Хлеб и выпечка",    "basket2-fill"),
    ("dairy",     "Молочные продукты", "cup-straw"),
    ("meat",      "Мясо и птица",      "egg-fried"),
    ("vegetables","Овощи",             "tree"),
    ("fruits",    "Фрукты",            "apple"),
    ("grocery",   "Бакалея",           "box-seam"),
    ("drinks",    "Напитки",           "cup-hot"),
    ("sweets",    "Сладости и чай",    "cup"),
]

# Каталог товаров НЕ хардкодится — товары создаются динамически при скрапинге
# (одна сигнатура = один SKU). Удалено чтобы не было дублирования с реальными данными.
_OLD_PRODUCTS_UNUSED = [  # оставлено как заметка истории, не используется
    # ("bakery", "Хлеб белый", "шт"),
]

# (category_slug, name_ru, unit)
PRODUCTS: list = [
    ("bakery", "Хлеб белый", "шт"),
    ("bakery", "Хлеб ржаной", "шт"),
    ("bakery", "Батон нарезной", "шт"),

    ("dairy", "Молоко 2.5%", "л"),
    ("dairy", "Молоко 3.2%", "л"),
    ("dairy", "Кефир 1%", "л"),
    ("dairy", "Сметана 20%", "г"),
    ("dairy", "Йогурт натуральный", "г"),
    ("dairy", "Творог 5%", "кг"),
    ("dairy", "Сыр российский", "кг"),
    ("dairy", "Масло сливочное 72.5%", "г"),
    ("dairy", "Яйца куриные С1", "дес"),

    ("meat", "Курица тушка охлаждённая", "кг"),
    ("meat", "Филе куриное", "кг"),
    ("meat", "Говядина лопатка", "кг"),
    ("meat", "Свинина шея", "кг"),
    ("meat", "Баранина", "кг"),
    ("meat", "Фарш говяжий", "кг"),
    ("meat", "Сосиски молочные", "кг"),

    ("vegetables", "Картофель", "кг"),
    ("vegetables", "Лук репчатый", "кг"),
    ("vegetables", "Морковь", "кг"),
    ("vegetables", "Капуста белокочанная", "кг"),
    ("vegetables", "Помидоры", "кг"),
    ("vegetables", "Огурцы", "кг"),
    ("vegetables", "Чеснок", "кг"),

    ("fruits", "Яблоки", "кг"),
    ("fruits", "Бананы", "кг"),
    ("fruits", "Апельсины", "кг"),
    ("fruits", "Лимоны", "кг"),

    ("grocery", "Рис длиннозёрный", "кг"),
    ("grocery", "Гречка", "кг"),
    ("grocery", "Макароны", "г"),
    ("grocery", "Мука пшеничная", "кг"),
    ("grocery", "Сахар песок", "кг"),
    ("grocery", "Соль поваренная", "кг"),
    ("grocery", "Масло подсолнечное", "л"),
    ("grocery", "Майонез", "г"),

    ("drinks", "Вода минеральная", "л"),
    ("drinks", "Сок яблочный", "л"),
    ("drinks", "Кофе растворимый", "г"),

]


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", " ": "-",
}


def _slugify(text: str) -> str:
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "item"


def seed(run_scrape: bool = True) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Countries
        by_code: dict[str, Country] = {}
        for c in COUNTRIES:
            obj = db.query(Country).filter(Country.code == c["code"]).first()
            if not obj:
                obj = Country(**c); db.add(obj)
            by_code[c["code"]] = obj
        db.commit()
        log.info("countries: %d", db.query(Country).count())

        # Cities
        for code, name in CITIES:
            country = by_code[code]
            exists = db.query(City).filter(City.country_id == country.id, City.name_ru == name).first()
            if not exists:
                db.add(City(name=name, name_ru=name, country_id=country.id))
        db.commit()
        log.info("cities: %d", db.query(City).count())

        # Categories
        cat_by_slug: dict[str, Category] = {}
        for slug, name, icon in CATEGORIES:
            obj = db.query(Category).filter(Category.slug == slug).first()
            if not obj:
                obj = Category(slug=slug, name_ru=name, icon=icon)
                db.add(obj)
            cat_by_slug[slug] = obj
        db.commit()
        log.info("categories: %d", db.query(Category).count())

        # Products
        added = 0
        for cat_slug, name_ru, unit in PRODUCTS:
            slug = _slugify(name_ru)
            exists = db.query(Product).filter(Product.slug == slug).first()
            if exists:
                continue
            db.add(Product(
                slug=slug, name_ru=name_ru, unit=unit,
                category_id=cat_by_slug[cat_slug].id,
            ))
            added += 1
        db.commit()
        log.info("products: total=%d, added this run=%d", db.query(Product).count(), added)

        # Currency rates
        for code, rate in DEFAULT_RATES.items():
            exists = db.query(CurrencyRate).filter(CurrencyRate.currency == code).first()
            if not exists:
                db.add(CurrencyRate(currency=code, rate_to_kgs=rate))
        db.commit()
        log.info("currency rates: %d", db.query(CurrencyRate).count())

        if run_scrape:
            log.info("starting real scraping of KG + RU sources...")
            scraper = Scraper(db)
            stats = scraper.scrape_all()
            for parser_slug, count in stats.items():
                log.info("  %s: %d prices saved", parser_slug, count)
            log.info("total prices saved: %d", sum(stats.values()))

            if "--no-history" not in sys.argv:
                log.info("generating 30-day price history backfill...")
                from scripts.backfill_history import backfill_history
                added = backfill_history(db)
                log.info("backfill added %d historical price points", added)
        else:
            log.info("--no-scrape set, skipping live price fetching")
    finally:
        db.close()


if __name__ == "__main__":
    run_scrape = "--no-scrape" not in sys.argv
    seed(run_scrape=run_scrape)
