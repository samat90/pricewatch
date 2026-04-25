"""Бэкфилл истории цен.

Для каждой свежей цены в БД генерируем 30 «исторических» точек —
случайные отклонения ±5-8% от текущей цены, с лёгким линейным трендом
(часть товаров дорожает, часть дешевеет).

Это делается один раз для демонстрации аналитики: без истории графики
и «подорожало/подешевело» пустые, а ждать 30 дней реальных сборов для
выставки невозможно.

Запуск:
    python -m scripts.backfill_history
"""
from __future__ import annotations

import io
import logging
import random
import sys
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill")

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import SessionLocal
from app.models import Price


DAYS = 30                      # глубина истории
POINTS_PER_DAY = 1             # одна точка в день
MAX_FLUCT = 0.07               # ±7% случайной вариации
TREND_MAX = 0.10               # до ±10% линейный тренд за весь период


def backfill_history(db: Session | None = None) -> int:
    """Для последних цен каждого (product, store) создаём 30 дневных точек."""
    close_after = False
    if db is None:
        db = SessionLocal()
        close_after = True

    try:
        # Для каждой пары (product_id, store_id) берём самую свежую запись
        subq = (
            db.query(
                Price.product_id,
                Price.store_id,
                func.max(Price.recorded_at).label("latest"),
            )
            .group_by(Price.product_id, Price.store_id)
            .subquery()
        )
        latest_prices = (
            db.query(Price)
            .join(
                subq,
                (Price.product_id == subq.c.product_id)
                & (Price.store_id == subq.c.store_id)
                & (Price.recorded_at == subq.c.latest),
            )
            .all()
        )
        log.info("seed prices for backfill: %d (product,store) pairs", len(latest_prices))

        added = 0
        rng = random.Random(42)
        now = datetime.utcnow()

        for seed_price in latest_prices:
            trend = rng.uniform(-TREND_MAX, TREND_MAX)  # тренд цены за период
            # Проверим, что ранее для этой пары нет записей за этот день
            for d in range(1, DAYS + 1):
                ts = now - timedelta(days=d)

                exists = (
                    db.query(Price.id)
                    .filter(
                        Price.product_id == seed_price.product_id,
                        Price.store_id == seed_price.store_id,
                        Price.recorded_at >= ts.replace(hour=0, minute=0, second=0, microsecond=0),
                        Price.recorded_at < (ts + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                    )
                    .first()
                )
                if exists:
                    continue

                # линейный тренд уменьшается к текущей дате (исторические цены отстают от текущей)
                linear_shift = trend * (d / DAYS)
                fluct = rng.uniform(-MAX_FLUCT, MAX_FLUCT)
                factor = 1.0 + linear_shift + fluct
                price_val = round(seed_price.price * max(0.5, factor), 2)

                db.add(Price(
                    product_id=seed_price.product_id,
                    store_id=seed_price.store_id,
                    price=price_val,
                    currency=seed_price.currency,
                    recorded_at=ts,
                    source_url=seed_price.source_url,
                ))
                added += 1
            # периодический коммит чтобы не раздуть транзакцию
            if added % 500 == 0 and added > 0:
                db.commit()

        db.commit()
        log.info("backfill complete: %d historical prices added", added)
        return added
    finally:
        if close_after:
            db.close()


if __name__ == "__main__":
    backfill_history()
