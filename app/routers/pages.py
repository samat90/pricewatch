"""HTML-страницы (Jinja2)."""
from datetime import datetime
from fastapi import APIRouter, Depends, Query, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import TEMPLATES_DIR
from app.database import get_db
from app.models import Product, Category, City, Country, Store, Price
from app.services import search as search_svc
from app.services import analytics as analytics_svc
from app.services import jobs as jobs_svc
from app.services.currency import format_price, get_rates


def _int_or_none(v):
    """Парсим Query-параметр: пустая строка / некорректное значение -> None."""
    if v in (None, "", "none", "null"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["format_price"] = format_price


def _common(db: Session) -> dict:
    last_price = db.query(Price).order_by(Price.recorded_at.desc()).first()
    refresh_status = jobs_svc.get_last_status()
    return {
        "categories": db.query(Category).order_by(Category.name_ru).all(),
        "countries": db.query(Country).order_by(Country.name_ru).all(),
        "cities": db.query(City).order_by(City.name_ru).all(),
        "now": datetime.utcnow(),
        "last_update": last_price.recorded_at if last_price else None,
        "refresh_status": refresh_status,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    await jobs_svc.ensure_fresh()

    products_total = db.query(Product).count()
    stores_total = db.query(Store).count()
    prices_total = db.query(Price).count()
    cities_total = db.query(City).count()

    popular = search_svc.search_products(db, query="", limit=80)
    popular = sorted(popular, key=lambda r: r["min_price_kgs"])[:12]

    changes = analytics_svc.top_price_changes(db, days=30, limit=6)

    ctx = {
        "request": request,
        "popular": popular,
        "products_total": products_total,
        "stores_total": stores_total,
        "prices_total": prices_total,
        "cities_total": cities_total,
        "changes": changes,
        **_common(db),
    }
    return templates.TemplateResponse(request, "index.html", ctx)


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query("", min_length=0),
    category_id: str = Query(""),
    city_id: str = Query(""),
    country_id: str = Query(""),
    db: Session = Depends(get_db),
):
    # Если данные устарели — фоново тригерим обновление.
    await jobs_svc.ensure_fresh()

    cat = _int_or_none(category_id)
    cid = _int_or_none(city_id)
    coid = _int_or_none(country_id)

    # Если есть поисковый запрос — запускаем live-поиск у Глобуса в фоне.
    live_triggered = False
    if q.strip():
        live_triggered = await jobs_svc.trigger_live_search(q)

    results = search_svc.search_products(
        db, query=q, category_id=cat, city_id=cid, country_id=coid, limit=120
    )
    ctx = {
        "request": request,
        "q": q,
        "results": results,
        "selected_category_id": cat,
        "selected_city_id": cid,
        "selected_country_id": coid,
        "live_triggered": live_triggered,
        **_common(db),
    }
    return templates.TemplateResponse(request, "search.html", ctx)


@router.get("/product/{slug}", response_class=HTMLResponse)
async def product_page(request: Request, slug: str, db: Session = Depends(get_db)):
    await jobs_svc.ensure_fresh()

    product = db.query(Product).filter(Product.slug == slug).first()
    if not product:
        raise HTTPException(404, "Товар не найден")

    offers = search_svc.get_product_offers(db, product.id)
    stats = search_svc.get_product_stats(db, product.id)
    history = analytics_svc.get_price_history(db, product.id, days=60)
    cities_cmp = analytics_svc.compare_cities(db, product.id)
    similar = search_svc.find_similar_products(db, product, limit=8)

    ctx = {
        "request": request,
        "product": product,
        "offers": offers,
        "stats": stats,
        "history": history,
        "cities_cmp": cities_cmp,
        "similar": similar,
        **_common(db),
    }
    return templates.TemplateResponse(request, "product.html", ctx)


@router.get("/category/{slug}", response_class=HTMLResponse)
def category_page(request: Request, slug: str, db: Session = Depends(get_db)):
    category = db.query(Category).filter(Category.slug == slug).first()
    if not category:
        raise HTTPException(404, "Категория не найдена")
    results = search_svc.search_products(db, category_id=category.id, limit=200)
    ctx = {
        "request": request,
        "category": category,
        "results": results,
        "q": "",
        "selected_category_id": category.id,
        "selected_city_id": None,
        "selected_country_id": None,
        **_common(db),
    }
    return templates.TemplateResponse(request, "search.html", ctx)


@router.get("/stores", response_class=HTMLResponse)
def stores_page(request: Request, db: Session = Depends(get_db)):
    stores = db.query(Store).order_by(Store.name).all()
    stores_data = []
    for s in stores:
        count = db.query(Price).filter(Price.store_id == s.id).count()
        last = (
            db.query(Price)
            .filter(Price.store_id == s.id)
            .order_by(Price.recorded_at.desc())
            .first()
        )
        stores_data.append({"store": s, "prices_count": count, "last_update": last.recorded_at if last else None})
    ctx = {"request": request, "stores_data": stores_data, **_common(db)}
    return templates.TemplateResponse(request, "stores.html", ctx)


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    period: int = Query(30, ge=7, le=365),
    db: Session = Depends(get_db),
):
    """Аналитика с выбором периода (7/30/90/365 дней)."""
    if period not in (7, 30, 90, 365):
        period = 30

    changes = analytics_svc.top_price_changes(db, days=period, limit=10)
    basket = analytics_svc.basket_by_city_categories(db)
    cat_dist = analytics_svc.category_price_distribution(db)
    country_cmp = analytics_svc.country_price_comparison(db)
    stores = analytics_svc.store_stats(db)
    summary = analytics_svc.overall_summary(db)
    rates = get_rates(db)

    from app.models import CurrencyRate
    rate_rows = db.query(CurrencyRate).all()
    rates_detail = [
        {"code": r.currency, "rate": round(r.rate_to_kgs, 4), "updated_at": r.updated_at}
        for r in rate_rows if r.currency != "KGS"
    ]

    ctx = {
        "request": request,
        "period": period,
        "changes": changes,
        "basket": basket,
        "cat_dist": cat_dist,
        "country_cmp": country_cmp,
        "stores": stores,
        "summary": summary,
        "rates": rates,
        "rates_detail": rates_detail,
        **_common(db),
    }
    return templates.TemplateResponse(request, "analytics.html", ctx)
