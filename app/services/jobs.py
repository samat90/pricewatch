"""Фоновые задачи: автоматический сбор цен.

Использует APScheduler (AsyncIOScheduler), который стартует вместе с
FastAPI приложением через lifespan. Задачи:

1. `refresh_all_sources` — каждые `REFRESH_INTERVAL_MINUTES` минут проходит
   все парсеры (Народный, Магнит и т.д.) и обновляет цены в БД.
2. `ensure_fresh_for_query` — асинхронный триггер, вызываемый со страницы
   поиска: если последний сбор старше `STALE_THRESHOLD_MINUTES`, запускаем
   обновление в фоне.
"""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Price
from app.services.scraper import Scraper
from app.services.currency import update_rates_from_nbkr

log = logging.getLogger("jobs")

REFRESH_INTERVAL_MINUTES = 30      # полный прогон всех парсеров
STALE_THRESHOLD_MINUTES = 60       # считаем БД устаревшей, если нет обновлений за час
LIVE_QUERY_TTL_SEC = 120           # тот же запрос чаще раза в 2 мин не повторяем
CURRENCY_REFRESH_HOURS = 6         # курсы валют с НБКР раз в 6 часов

_scheduler: AsyncIOScheduler | None = None
_refresh_lock = asyncio.Lock()
_last_status = {
    "started_at": None,
    "finished_at": None,
    "last_stats": {},
    "running": False,
    "live_queries": {},     # {query: {started, finished, saved}}
}
_live_query_locks: dict[str, float] = {}  # normalized_query -> last_run_ts


def get_last_status() -> dict:
    return dict(_last_status)


async def refresh_all_sources() -> dict[str, int]:
    """Обходит все парсеры и сохраняет свежие цены."""
    if _refresh_lock.locked():
        log.info("refresh already running, skip")
        return {}
    async with _refresh_lock:
        _last_status["running"] = True
        _last_status["started_at"] = datetime.utcnow()
        try:
            # Scraper синхронный → выполняем в executor
            loop = asyncio.get_running_loop()
            stats = await loop.run_in_executor(None, _sync_refresh)
            _last_status["last_stats"] = stats
            total = sum(stats.values())
            log.info("refresh done: %d prices saved", total)
            return stats
        finally:
            _last_status["finished_at"] = datetime.utcnow()
            _last_status["running"] = False


def _sync_refresh() -> dict[str, int]:
    """Синхронная часть: открывает свою сессию БД и запускает Scraper."""
    db: Session = SessionLocal()
    try:
        scraper = Scraper(db)
        return scraper.scrape_all()
    finally:
        db.close()


async def trigger_live_search(query: str) -> bool:
    """Запуск live-поиска для конкретного запроса в фоне.

    Дедупликация: тот же запрос чаще чем раз в LIVE_QUERY_TTL_SEC не запускаем.
    Возвращает True если задача стартовала.
    """
    import time
    q = (query or "").strip().lower()
    if len(q) < 2:
        return False
    now = time.time()
    last = _live_query_locks.get(q, 0)
    if now - last < LIVE_QUERY_TTL_SEC:
        return False
    _live_query_locks[q] = now
    _last_status["live_queries"][q] = {"started": datetime.utcnow().isoformat(), "running": True, "saved": 0}
    log.info("triggering live search for %r", query)

    async def run():
        loop = asyncio.get_running_loop()
        try:
            saved = await loop.run_in_executor(None, _sync_live_search, query)
            _last_status["live_queries"][q].update({
                "running": False,
                "finished": datetime.utcnow().isoformat(),
                "saved": saved,
            })
        except Exception:
            log.exception("live-search %r failed", query)
            _last_status["live_queries"][q]["running"] = False

    asyncio.create_task(run())
    return True


def _sync_live_search(query: str) -> int:
    db: Session = SessionLocal()
    try:
        return Scraper(db).live_search_query(query)
    finally:
        db.close()


async def ensure_fresh(stale_minutes: int = STALE_THRESHOLD_MINUTES) -> bool:
    """Если данные устарели, асинхронно запускаем обновление (не блокируем)."""
    if _refresh_lock.locked():
        return False
    db = SessionLocal()
    try:
        last = db.query(Price).order_by(Price.recorded_at.desc()).first()
    finally:
        db.close()
    if last is None:
        log.info("DB empty, triggering refresh")
        asyncio.create_task(refresh_all_sources())
        return True
    age = datetime.utcnow() - last.recorded_at
    if age > timedelta(minutes=stale_minutes):
        log.info("data is %s old, triggering refresh", age)
        asyncio.create_task(refresh_all_sources())
        return True
    return False


async def refresh_currency_rates() -> int:
    """Обновить курсы валют с НБКР."""
    loop = asyncio.get_running_loop()
    def _sync():
        db = SessionLocal()
        try:
            return update_rates_from_nbkr(db)
        finally:
            db.close()
    n = await loop.run_in_executor(None, _sync)
    log.info("nbkr currency refresh: %d currencies updated", n)
    return n


def start_scheduler() -> None:
    """Запускается при старте приложения (lifespan)."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        refresh_all_sources,
        "interval",
        minutes=REFRESH_INTERVAL_MINUTES,
        id="refresh_all",
        next_run_time=datetime.utcnow() + timedelta(seconds=10),
        misfire_grace_time=3600, coalesce=True,
    )
    _scheduler.add_job(
        refresh_currency_rates,
        "interval",
        hours=CURRENCY_REFRESH_HOURS,
        id="refresh_rates",
        next_run_time=datetime.utcnow() + timedelta(seconds=3),
        misfire_grace_time=86400, coalesce=True,
    )
    _scheduler.start()
    log.info("scheduler started: prices every %dmin, rates every %dh",
             REFRESH_INTERVAL_MINUTES, CURRENCY_REFRESH_HOURS)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")
