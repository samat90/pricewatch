"""Поиск и агрегация предложений.

Каждый Product — это один SKU (бренд + вариант + вес + жирность). Поэтому
«offers» в карточке товара — это цены разных магазинов на ОДИН И ТОТ ЖЕ SKU.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy.orm import Session, joinedload

from app.models import City, Country, Price, Product, Store
from app.services.currency import get_rates, to_kgs


FRESH_WINDOW_DAYS = 30  # «актуальными» считаем цены не старше 30 дней


def _stem(word: str) -> str:
    """Примитивный русский стемминг — отбрасываем типичные окончания."""
    w = word.lower()
    for ending in ("ами", "ями", "ов", "ев", "ей", "ий", "ия", "ую", "юю",
                   "ам", "ям", "ах", "ях", "ом", "ем", "ой", "ей",
                   "а", "я", "ы", "и", "е", "о", "у", "ю"):
        if len(w) > len(ending) + 2 and w.endswith(ending):
            return w[:-len(ending)]
    return w


def _relevance(name: str, brand: str, needle: str) -> int:
    """Скоринг релевантности с лёгким стеммингом.

    Цель: «яйца» должно находить «Яйцо куриное», но НЕ «майонез с яйцами»
    (там совпадение глубоко в середине и получит низкий балл).

    100 — бренд точно совпадает или имя начинается с корня запроса
     70 — запрос (или корень) = первое слово имени
     60 — запрос/корень среди первых 2 слов
     40 — запрос/корень = отдельное слово где-либо
     30 — префиксное совпадение слова
     10 — подстрока в середине
    """
    if not needle:
        return 0
    import re as _re

    needle_stem = _stem(needle)
    needles = {needle, needle_stem} if len(needle_stem) >= 3 else {needle}

    if brand:
        b = brand.lower()
        if needle == b or any(n == b for n in needles):
            return 100
    if name.startswith(needle) or name.startswith(needle_stem):
        return 100

    words = _re.findall(r"[а-яёa-z0-9]+", name)
    if not words:
        return 0

    stems = [_stem(w) for w in words]
    # 1-е слово: точное совпадение имени или корня
    if words[0] == needle or stems[0] == needle_stem:
        return 70
    # Среди первых 2 слов
    for i, (w, st) in enumerate(zip(words[:2], stems[:2])):
        if w == needle or st == needle_stem:
            return 65 - i * 5
    # Среди первых 4 слов — по корню
    for i in range(min(4, len(words))):
        if words[i] == needle or stems[i] == needle_stem:
            return 55 - i * 5
        if words[i].startswith(needle) or (len(needle) >= 3 and stems[i].startswith(needle_stem)):
            return 45 - i * 5
    # Корень где-либо как отдельное слово
    if needle in words or needle_stem in stems:
        return 40
    # Префиксное совпадение хоть одного слова
    for w in words:
        if w.startswith(needle) or (len(needle_stem) >= 4 and w.startswith(needle_stem)):
            return 30
    # Подстрока в середине
    if needle in name or needle_stem in name:
        return 10
    return 0


def _base_price_query(db: Session, city_id: int | None, country_id: int | None):
    q = (
        db.query(Price, Store, City, Country)
        .join(Store, Store.id == Price.store_id)
        .join(City,  City.id == Store.city_id)
        .join(Country, Country.id == City.country_id)
        .filter(Price.recorded_at >= datetime.utcnow() - timedelta(days=FRESH_WINDOW_DAYS))
    )
    if city_id:
        q = q.filter(City.id == city_id)
    if country_id:
        q = q.filter(Country.id == country_id)
    return q


def search_products(
    db: Session,
    query: str = "",
    category_id: int | None = None,
    city_id: int | None = None,
    country_id: int | None = None,
    limit: int = 120,
) -> list[dict]:
    """Вернуть Product'ы, у которых есть свежие цены с учётом фильтров."""
    rates = get_rates(db)

    # Базовый запрос по ценам с фильтрами стран/городов
    price_q = _base_price_query(db, city_id, country_id)

    # Выбираем только Product-id, которые подходят
    pid_q = price_q.with_entities(Price.product_id).distinct()
    allowed_ids = {pid for (pid,) in pid_q.all()}
    if not allowed_ids:
        return []

    # Фильтр по Product
    q = db.query(Product).options(joinedload(Product.category)).filter(Product.id.in_(allowed_ids))
    if category_id:
        q = q.filter(Product.category_id == category_id)
    products = q.all()

    # Текстовый поиск с ранжированием по релевантности.
    # Идея: слово-запрос оценивается по позиции в названии.
    # Match score: 100 — начало имени, 50 — слово целиком, 20 — внутри слова, 0 — нет.
    if query:
        needle = query.lower().strip()
        scored: list[tuple[int, Product]] = []
        for p in products:
            name = (p.name_ru or "").lower()
            brand = (p.brand or "").lower()
            score = _relevance(name, brand, needle)
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        products = [p for _s, p in scored]

    if not products:
        return []

    # Для каждого Product считаем агрегат цен (в выбранном фильтре)
    all_rows = price_q.all()
    per_product: dict[int, list[tuple[float, Store, City, Country]]] = {}
    for price, store, city, country in all_rows:
        kgs = to_kgs(price.price, price.currency, rates)
        per_product.setdefault(price.product_id, []).append((kgs, store, city, country))

    results: list[dict] = []
    for p in products:
        rows = per_product.get(p.id) or []
        if not rows:
            continue
        # Берём по 1 предложению от магазина (самая свежая — тут уже)
        prices = [r[0] for r in rows]
        best = min(rows, key=lambda r: r[0])
        results.append({
            "product": p,
            "min_price_kgs": round(min(prices), 2),
            "max_price_kgs": round(max(prices), 2),
            "avg_price_kgs": round(sum(prices) / len(prices), 2),
            "offers_count": len(rows),
            "cheapest_store": best[1].name,
            "cheapest_city":  best[2].name_ru,
            "cheapest_country": best[3].name_ru,
            "cheapest_flag": best[3].flag,
            "stores_count": len({r[1].id for r in rows}),
        })

    # Сортировка: сначала SKU с несколькими магазинами (есть настоящее сравнение), потом по цене
    results.sort(key=lambda r: (-r["stores_count"], r["min_price_kgs"]))
    return results[:limit]


def get_product_offers(
    db: Session,
    product_id: int,
    city_id: int | None = None,
    country_id: int | None = None,
) -> list[dict]:
    """Все актуальные предложения (по одному на магазин — последнее)."""
    rates = get_rates(db)
    q = _base_price_query(db, city_id, country_id).filter(Price.product_id == product_id)
    rows = q.all()

    latest: dict[int, tuple] = {}
    for price, store, city, country in rows:
        k = store.id
        if k not in latest or price.recorded_at > latest[k][0].recorded_at:
            latest[k] = (price, store, city, country)

    offers: list[dict] = []
    for price, store, city, country in latest.values():
        offers.append({
            "price_original": price.price,
            "currency": price.currency,
            "price_kgs": to_kgs(price.price, price.currency, rates),
            "store_id": store.id,
            "store_name": store.name,
            "store_website": store.website,
            "city_id": city.id,
            "city_name": city.name_ru,
            "country_code": country.code,
            "country_name": country.name_ru,
            "country_flag": country.flag,
            "recorded_at": price.recorded_at,
            "source_url": price.source_url,
        })
    offers.sort(key=lambda o: o["price_kgs"])
    return offers


def get_product_stats(
    db: Session,
    product_id: int,
    city_id: int | None = None,
    country_id: int | None = None,
    rates: dict | None = None,
) -> dict:
    offers = get_product_offers(db, product_id, city_id=city_id, country_id=country_id)
    if not offers:
        return {
            "offers_count": 0, "min_price_kgs": 0, "max_price_kgs": 0,
            "avg_price_kgs": 0, "cheapest_store": "", "cheapest_city": "",
        }
    prices = [o["price_kgs"] for o in offers]
    cheapest = offers[0]
    return {
        "offers_count": len(offers),
        "min_price_kgs": round(min(prices), 2),
        "max_price_kgs": round(max(prices), 2),
        "avg_price_kgs": round(sum(prices) / len(prices), 2),
        "cheapest_store": cheapest["store_name"],
        "cheapest_city":  cheapest["city_name"],
    }


def find_similar_products(db: Session, product: Product, limit: int = 6) -> list[Product]:
    """Найти похожие SKU в той же категории (другие бренды/веса)."""
    return (
        db.query(Product)
        .filter(Product.category_id == product.category_id, Product.id != product.id)
        .limit(limit)
        .all()
    )
