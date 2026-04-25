"""Админ-панель: ручной ввод товаров/цен и запуск скрапера."""
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import TEMPLATES_DIR
from app.database import get_db
from app.models import Category, City, Country, Price, Product, Store
from app.services.currency import format_price
from app.services.scraper import Scraper, ALL_PARSERS

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["format_price"] = format_price


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", " ": "-",
}


def _slugify(text: str) -> str:
    import re
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "product"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_index(request: Request, db: Session = Depends(get_db), flash: str = ""):
    products = db.query(Product).order_by(Product.name_ru).all()
    stores = db.query(Store).order_by(Store.name).all()
    categories = db.query(Category).order_by(Category.name_ru).all()
    countries = db.query(Country).order_by(Country.name_ru).all()
    cities = db.query(City).order_by(City.name_ru).all()

    last_prices = db.query(Price).order_by(Price.recorded_at.desc()).limit(15).all()
    last_scrape = last_prices[0].recorded_at if last_prices else None

    parsers_info = [
        {"slug": p.slug, "name": p.store_name, "base_url": p.base_url, "country": p.country_code}
        for p in ALL_PARSERS
    ]

    ctx = {
        "request": request,
        "products": products,
        "stores": stores,
        "categories": categories,
        "countries": countries,
        "cities": cities,
        "last_prices": last_prices,
        "last_scrape": last_scrape,
        "parsers_info": parsers_info,
        "flash": flash,
        "now": datetime.utcnow(),
    }
    return templates.TemplateResponse(request, "admin.html", ctx)


@router.post("/product/add")
def admin_add_product(
    name_ru: str = Form(...),
    category_id: int = Form(...),
    unit: str = Form("шт"),
    image_url: str = Form(""),
    db: Session = Depends(get_db),
):
    slug = _slugify(name_ru)
    base = slug
    i = 1
    while db.query(Product).filter(Product.slug == slug).first():
        i += 1
        slug = f"{base}-{i}"
    product = Product(name_ru=name_ru, slug=slug, category_id=category_id, unit=unit, image_url=image_url)
    db.add(product)
    db.commit()
    return RedirectResponse(f"/admin?flash=Товар+«{name_ru}»+добавлен", status_code=303)


@router.post("/price/add")
def admin_add_price(
    product_id: int = Form(...),
    store_id: int = Form(...),
    price: float = Form(...),
    currency: str = Form("KGS"),
    source_url: str = Form(""),
    db: Session = Depends(get_db),
):
    p = Price(
        product_id=product_id, store_id=store_id, price=price,
        currency=currency, source_url=source_url, recorded_at=datetime.utcnow(),
    )
    db.add(p)
    db.commit()
    return RedirectResponse("/admin?flash=Цена+сохранена", status_code=303)


@router.post("/store/add")
def admin_add_store(
    name: str = Form(...),
    city_id: int = Form(...),
    website: str = Form(""),
    db: Session = Depends(get_db),
):
    slug = _slugify(name)
    store = Store(name=name, slug=slug, city_id=city_id, website=website)
    db.add(store)
    db.commit()
    return RedirectResponse(f"/admin?flash=Магазин+«{name}»+добавлен", status_code=303)


@router.post("/scrape/run")
def admin_run_scraper(db: Session = Depends(get_db)):
    scraper = Scraper(db)
    stats = scraper.scrape_all()
    total = sum(stats.values())
    parts = ",+".join(f"{k}:{v}" for k, v in stats.items()) or "нет+результатов"
    msg = f"Сбор+завершён:+всего+{total}+цен+({parts})"
    return RedirectResponse(f"/admin?flash={msg}", status_code=303)


@router.post("/history/backfill")
def admin_backfill_history(db: Session = Depends(get_db)):
    from scripts.backfill_history import backfill_history
    added = backfill_history(db)
    return RedirectResponse(f"/admin?flash=История+цен+достроена:+{added}+точек", status_code=303)
