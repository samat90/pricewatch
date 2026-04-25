"""Аналитика цен: история, графики, сравнение городов, сводная статистика."""
from datetime import datetime, timedelta, date
from collections import defaultdict
from sqlalchemy import func, distinct
from sqlalchemy.orm import Session

from app.models import Category, Product, Price, Store, City, Country
from app.services.currency import get_rates, to_kgs


def get_price_history(
    db: Session,
    product_id: int,
    days: int = 60,
    city_id: int | None = None,
) -> list[dict]:
    """История цен на товар сгруппированная по датам."""
    rates = get_rates(db)
    cutoff = datetime.utcnow() - timedelta(days=days)

    q = (
        db.query(Price, Store, City)
        .join(Store, Price.store_id == Store.id)
        .join(City, Store.city_id == City.id)
        .filter(Price.product_id == product_id, Price.recorded_at >= cutoff)
    )
    if city_id:
        q = q.filter(City.id == city_id)

    buckets: dict[date, list[float]] = defaultdict(list)
    for price, _store, _city in q.all():
        kgs = to_kgs(price.price, price.currency, rates)
        buckets[price.recorded_at.date()].append(kgs)

    points = []
    for d in sorted(buckets):
        prices = buckets[d]
        points.append({
            "date": d.isoformat(),
            "avg_price_kgs": round(sum(prices) / len(prices), 2),
            "min_price_kgs": round(min(prices), 2),
            "max_price_kgs": round(max(prices), 2),
        })
    return points


def compare_cities(db: Session, product_id: int) -> list[dict]:
    """Сравнение актуальной средней цены товара по городам."""
    rates = get_rates(db)
    cutoff = datetime.utcnow() - timedelta(days=7)

    rows = (
        db.query(Price, Store, City, Country)
        .join(Store, Price.store_id == Store.id)
        .join(City, Store.city_id == City.id)
        .join(Country, City.country_id == Country.id)
        .filter(Price.product_id == product_id, Price.recorded_at >= cutoff)
        .all()
    )

    by_city: dict[int, dict] = {}
    for price, store, city, country in rows:
        kgs = to_kgs(price.price, price.currency, rates)
        if city.id not in by_city:
            by_city[city.id] = {
                "city_id": city.id,
                "city_name": city.name_ru,
                "country_name": country.name_ru,
                "country_code": country.code,
                "country_flag": country.flag,
                "prices": [],
            }
        by_city[city.id]["prices"].append(kgs)

    result = []
    for data in by_city.values():
        prices = data["prices"]
        result.append({
            "city_id": data["city_id"],
            "city_name": data["city_name"],
            "country_name": data["country_name"],
            "country_code": data["country_code"],
            "country_flag": data["country_flag"],
            "min_price_kgs": round(min(prices), 2),
            "avg_price_kgs": round(sum(prices) / len(prices), 2),
            "max_price_kgs": round(max(prices), 2),
            "offers": len(prices),
        })
    result.sort(key=lambda r: r["avg_price_kgs"])
    return result


def basket_by_city(db: Session, product_ids: list[int]) -> list[dict]:
    """DEPRECATED: строгая корзина по точным SKU. Оставлено для совместимости."""
    if not product_ids:
        return []
    rates = get_rates(db)
    cutoff = datetime.utcnow() - timedelta(days=7)

    rows = (
        db.query(Price, Store, City, Country)
        .join(Store, Price.store_id == Store.id)
        .join(City, Store.city_id == City.id)
        .join(Country, City.country_id == Country.id)
        .filter(Price.product_id.in_(product_ids), Price.recorded_at >= cutoff)
        .all()
    )

    latest: dict[tuple, tuple] = {}
    for price, store, city, country in rows:
        key = (city.id, price.product_id)
        if key not in latest or price.recorded_at > latest[key][0].recorded_at:
            latest[key] = (price, store, city, country)

    by_city: dict[int, dict] = {}
    for (city_id, product_id), (price, store, city, country) in latest.items():
        if city_id not in by_city:
            by_city[city_id] = {
                "city_id": city_id, "city_name": city.name_ru,
                "country_name": country.name_ru, "country_flag": country.flag,
                "country_code": country.code,
                "total_kgs": 0.0, "items_count": 0,
            }
        by_city[city_id]["total_kgs"] += to_kgs(price.price, price.currency, rates)
        by_city[city_id]["items_count"] += 1

    need = len(product_ids)
    full = [c for c in by_city.values() if c["items_count"] == need]
    full.sort(key=lambda c: c["total_kgs"])
    for c in full:
        c["total_kgs"] = round(c["total_kgs"], 2)
    return full


def basket_by_city_categories(db: Session, category_slugs: list[str] | None = None) -> list[dict]:
    """Корзина по КАТЕГОРИЯМ: берём самый дешёвый товар каждой категории
    в каждом городе. Товары РАЗНЫЕ SKU (разные бренды), но сопоставимые.

    Это реалистичная «потребительская корзина» — покупатель купит что-нибудь
    из каждой категории, не обязательно один и тот же бренд в разных городах.
    """
    rates = get_rates(db)
    cutoff = datetime.utcnow() - timedelta(days=30)

    if category_slugs:
        cats = db.query(Category).filter(Category.slug.in_(category_slugs)).all()
    else:
        cats = db.query(Category).all()
    if not cats:
        return []
    cat_ids = {c.id for c in cats}
    cat_names = {c.id: c.name_ru for c in cats}

    rows = (
        db.query(Price, Store, City, Country, Product)
        .join(Store, Price.store_id == Store.id)
        .join(City, Store.city_id == City.id)
        .join(Country, City.country_id == Country.id)
        .join(Product, Product.id == Price.product_id)
        .filter(Product.category_id.in_(cat_ids), Price.recorded_at >= cutoff)
        .all()
    )

    # Самая дешёвая позиция в каждой комбинации (city, category)
    cheapest: dict[tuple[int, int], tuple[float, Product, Store, City, Country]] = {}
    for price, store, city, country, product in rows:
        kgs = to_kgs(price.price, price.currency, rates)
        key = (city.id, product.category_id)
        if key not in cheapest or kgs < cheapest[key][0]:
            cheapest[key] = (kgs, product, store, city, country)

    # Группируем по городу
    by_city: dict[int, dict] = {}
    for (city_id, cat_id), (kgs, product, store, city, country) in cheapest.items():
        c = by_city.setdefault(city_id, {
            "city_id": city_id, "city_name": city.name_ru,
            "country_name": country.name_ru, "country_flag": country.flag,
            "country_code": country.code,
            "total_kgs": 0.0, "items_count": 0,
            "items": [],
        })
        c["total_kgs"] += kgs
        c["items_count"] += 1
        c["items"].append({
            "category": cat_names.get(cat_id, ""),
            "product": product.name_ru,
            "product_slug": product.slug,
            "price_kgs": round(kgs, 2),
            "store": store.name,
        })

    result = list(by_city.values())
    for c in result:
        c["total_kgs"] = round(c["total_kgs"], 2)
        c["items"].sort(key=lambda i: i["category"])
    # В первую очередь — города с максимальным охватом категорий
    result.sort(key=lambda c: (-c["items_count"], c["total_kgs"]))
    return result


def category_price_distribution(db: Session, country_code: str | None = None) -> list[dict]:
    """Средняя цена в каждой категории (опционально по стране)."""
    rates = get_rates(db)
    q = (
        db.query(Category.name_ru, Category.slug, Price, Country.code)
        .join(Product, Product.category_id == Category.id)
        .join(Price, Price.product_id == Product.id)
        .join(Store, Store.id == Price.store_id)
        .join(City, City.id == Store.city_id)
        .join(Country, Country.id == City.country_id)
    )
    if country_code:
        q = q.filter(Country.code == country_code)

    buckets: dict[str, list[float]] = defaultdict(list)
    slugs: dict[str, str] = {}
    for name, slug, price, _c in q.all():
        kgs = to_kgs(price.price, price.currency, rates)
        buckets[name].append(kgs)
        slugs[name] = slug
    result = []
    for name, prices in buckets.items():
        if not prices:
            continue
        result.append({
            "category": name,
            "slug": slugs[name],
            "avg_kgs": round(sum(prices) / len(prices), 2),
            "min_kgs": round(min(prices), 2),
            "max_kgs": round(max(prices), 2),
            "count": len(prices),
        })
    result.sort(key=lambda r: -r["avg_kgs"])
    return result


def country_price_comparison(db: Session) -> dict:
    """Сравнение средней цены ТОВАРА в каждой категории между странами.

    На одном графике показывает две группы столбиков: KG vs RU (vs KZ) по категориям.
    """
    rates = get_rates(db)
    q = (
        db.query(Category.name_ru, Category.slug, Price, Country.code, Country.name_ru, Country.flag)
        .join(Product, Product.category_id == Category.id)
        .join(Price, Price.product_id == Product.id)
        .join(Store, Store.id == Price.store_id)
        .join(City, City.id == Store.city_id)
        .join(Country, Country.id == City.country_id)
    )
    # (category, country_code) -> list of kgs prices
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    cat_slugs: dict[str, str] = {}
    country_meta: dict[str, dict] = {}
    for cname, cslug, price, ccode, cname_ru, cflag in q.all():
        kgs = to_kgs(price.price, price.currency, rates)
        buckets[(cname, ccode)].append(kgs)
        cat_slugs[cname] = cslug
        country_meta[ccode] = {"name": cname_ru, "flag": cflag}

    # Перестраиваем в table: categories x countries
    categories = sorted({k[0] for k in buckets})
    countries = sorted({k[1] for k in buckets})
    rows = []
    for cat in categories:
        row = {
            "category": cat,
            "slug": cat_slugs[cat],
            "by_country": {},
        }
        for country in countries:
            prices = buckets.get((cat, country), [])
            row["by_country"][country] = round(sum(prices) / len(prices), 2) if prices else 0
        rows.append(row)
    return {
        "categories": categories,
        "countries": [{"code": c, **country_meta[c]} for c in countries],
        "rows": rows,
    }


def store_stats(db: Session) -> list[dict]:
    """Сколько SKU/цен у каждого магазина, средний чек."""
    rates = get_rates(db)
    rows = (
        db.query(Store.id, Store.name, Country.flag, City.name_ru, Price.price, Price.currency)
        .join(City, City.id == Store.city_id)
        .join(Country, Country.id == City.country_id)
        .join(Price, Price.store_id == Store.id)
        .all()
    )
    buckets: dict[int, dict] = {}
    for sid, sname, flag, city, p, cur in rows:
        b = buckets.setdefault(sid, {
            "store_id": sid, "store_name": sname,
            "flag": flag, "city": city,
            "prices": [], "products": 0,
        })
        b["prices"].append(to_kgs(p, cur, rates))
    result = []
    for b in buckets.values():
        prices = b.pop("prices")
        result.append({
            **b,
            "prices_count": len(prices),
            "avg_kgs": round(sum(prices) / len(prices), 2),
            "min_kgs": round(min(prices), 2),
            "max_kgs": round(max(prices), 2),
        })
    # SKU count
    sku_counts = dict(
        db.query(Price.store_id, func.count(distinct(Price.product_id))).group_by(Price.store_id).all()
    )
    for r in result:
        r["sku_count"] = sku_counts.get(r["store_id"], 0)
    result.sort(key=lambda r: -r["prices_count"])
    return result


def overall_summary(db: Session) -> dict:
    """Верхняя сводка дашборда."""
    rates = get_rates(db)
    total_prices = db.query(Price).count()
    total_products = db.query(Product).count()
    total_stores = db.query(Store).count()
    total_cities = db.query(City).count()
    total_countries = db.query(Country).count()

    # Средняя цена всех зафиксированных за последние 30 дней
    cutoff = datetime.utcnow() - timedelta(days=30)
    recent = db.query(Price).filter(Price.recorded_at >= cutoff).all()
    avg_all = 0.0
    if recent:
        avg_all = round(
            sum(to_kgs(p.price, p.currency, rates) for p in recent) / len(recent), 2
        )
    last = db.query(Price).order_by(Price.recorded_at.desc()).first()
    return {
        "total_prices": total_prices,
        "total_products": total_products,
        "total_stores": total_stores,
        "total_cities": total_cities,
        "total_countries": total_countries,
        "avg_price_kgs": avg_all,
        "last_update": last.recorded_at if last else None,
    }


def top_price_changes(db: Session, days: int = 30, limit: int = 10) -> dict[str, list]:
    """Топ товаров по изменению средней цены за период."""
    rates = get_rates(db)
    now = datetime.utcnow()
    cutoff_recent = now - timedelta(days=7)
    cutoff_past = now - timedelta(days=days)

    products = db.query(Product).all()
    rises, falls = [], []

    for product in products:
        recent = (
            db.query(Price)
            .filter(Price.product_id == product.id, Price.recorded_at >= cutoff_recent)
            .all()
        )
        past = (
            db.query(Price)
            .filter(
                Price.product_id == product.id,
                Price.recorded_at >= cutoff_past,
                Price.recorded_at < cutoff_recent,
            )
            .all()
        )
        if not recent or not past:
            continue

        recent_avg = sum(to_kgs(p.price, p.currency, rates) for p in recent) / len(recent)
        past_avg = sum(to_kgs(p.price, p.currency, rates) for p in past) / len(past)
        if past_avg == 0:
            continue

        change = (recent_avg - past_avg) / past_avg * 100
        row = {
            "product_id": product.id,
            "product_name": product.name_ru,
            "product_slug": product.slug,
            "image_url": product.image_url,
            "past_avg": round(past_avg, 2),
            "recent_avg": round(recent_avg, 2),
            "change_pct": round(change, 2),
        }
        if change >= 0:
            rises.append(row)
        else:
            falls.append(row)

    rises.sort(key=lambda r: r["change_pct"], reverse=True)
    falls.sort(key=lambda r: r["change_pct"])
    return {"rises": rises[:limit], "falls": falls[:limit]}
