"""JSON API."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Product, City, Country, Category, Store
from app.services import search as search_svc
from app.services import analytics as analytics_svc
from app.services import jobs as jobs_svc
from app.services.currency import get_rates
from app.models import Price

router = APIRouter()


@router.get("/cities")
def list_cities(db: Session = Depends(get_db)):
    cities = db.query(City).all()
    return [
        {
            "id": c.id, "name": c.name_ru,
            "country_code": c.country.code, "country_name": c.country.name_ru,
        }
        for c in cities
    ]


@router.get("/countries")
def list_countries(db: Session = Depends(get_db)):
    return [{"id": c.id, "code": c.code, "name": c.name_ru, "currency": c.currency} for c in db.query(Country).all()]


@router.get("/categories")
def list_categories(db: Session = Depends(get_db)):
    return [{"id": c.id, "slug": c.slug, "name": c.name_ru, "icon": c.icon} for c in db.query(Category).all()]


def _int_or_none(v):
    if v in (None, "", "none", "null"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@router.get("/stores")
def list_stores(city_id: str = "", db: Session = Depends(get_db)):
    cid = _int_or_none(city_id)
    q = db.query(Store)
    if cid:
        q = q.filter(Store.city_id == cid)
    return [{"id": s.id, "name": s.name, "city_id": s.city_id, "website": s.website} for s in q.all()]


@router.get("/products/{product_id}/offers")
def product_offers(product_id: int, city_id: str = "", country_id: str = "", db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404)
    return {
        "product_id": product.id,
        "name": product.name_ru,
        "offers": search_svc.get_product_offers(db, product.id,
                                                city_id=_int_or_none(city_id),
                                                country_id=_int_or_none(country_id)),
    }


@router.get("/products/{product_id}/history")
def product_history(product_id: int, days: int = 60, city_id: str = "", db: Session = Depends(get_db)):
    return {"product_id": product_id, "points": analytics_svc.get_price_history(db, product_id, days=days, city_id=_int_or_none(city_id))}


@router.get("/products/{product_id}/cities")
def product_cities(product_id: int, db: Session = Depends(get_db)):
    return {"product_id": product_id, "cities": analytics_svc.compare_cities(db, product_id)}


@router.get("/search")
async def api_search(q: str = "", category_id: str = "", city_id: str = "", country_id: str = "", limit: int = 60, db: Session = Depends(get_db)):
    # Триггерим реальный live-search (Globus sitemap), если есть запрос
    if q.strip():
        await jobs_svc.trigger_live_search(q)
    results = search_svc.search_products(
        db, query=q,
        category_id=_int_or_none(category_id),
        city_id=_int_or_none(city_id),
        country_id=_int_or_none(country_id),
        limit=limit,
    )
    return [
        {
            "id": r["product"].id,
            "name": r["product"].name_ru,
            "slug": r["product"].slug,
            "unit": r["product"].unit,
            "image_url": r["product"].image_url,
            "min_price_kgs": r["min_price_kgs"],
            "avg_price_kgs": r["avg_price_kgs"],
            "max_price_kgs": r["max_price_kgs"],
            "offers_count": r["offers_count"],
            "cheapest_store": r["cheapest_store"],
            "cheapest_city": r["cheapest_city"],
        }
        for r in results
    ]


@router.get("/rates")
def api_rates(db: Session = Depends(get_db)):
    return get_rates(db)


@router.get("/status")
def api_status(db: Session = Depends(get_db), query: str = ""):
    """Статус автообновления: последний update, идёт ли сейчас сбор.

    Если передан ?query=X — отдельно включаем статус live-поиска по этому запросу.
    """
    last = db.query(Price).order_by(Price.recorded_at.desc()).first()
    status = jobs_svc.get_last_status()
    live_for_query = None
    if query:
        lq = status.get("live_queries", {}).get(query.strip().lower())
        if lq:
            live_for_query = lq
    return {
        "last_price_at": last.recorded_at.isoformat() if last else None,
        "running": status.get("running", False),
        "started_at": status["started_at"].isoformat() if status.get("started_at") else None,
        "finished_at": status["finished_at"].isoformat() if status.get("finished_at") else None,
        "last_stats": status.get("last_stats", {}),
        "total_prices": db.query(Price).count(),
        "live_for_query": live_for_query,
    }


@router.post("/refresh")
async def api_refresh():
    """Ручной триггер сбора в фоне (для кнопки 'обновить сейчас')."""
    import asyncio
    if jobs_svc._refresh_lock.locked():
        return {"started": False, "reason": "already_running"}
    asyncio.create_task(jobs_svc.refresh_all_sources())
    return {"started": True}
