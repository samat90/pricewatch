"""Конвертация валют. Курсы хранятся в БД (rate_to_kgs).

Источник правды — Национальный банк КР (https://www.nbkr.kg/XML/daily.xml).
Там ежедневно публикуется официальный XML с курсами на дату. Функция
`fetch_nbkr_rates()` скачивает XML, парсит и возвращает dict.
Функция `update_rates_from_nbkr()` применяет полученные курсы к БД.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.models import CurrencyRate

log = logging.getLogger("currency")

NBKR_URL = "https://www.nbkr.kg/XML/daily.xml"
UA = "Mozilla/5.0 (PriceWatch, OshGU student exhibition)"

# Фолбек-курсы на случай, если API НБКР недоступен при первом старте.
# При живом сервере эти значения моментально перетираются данными НБКР.
DEFAULT_RATES = {
    "KGS": 1.0,
    "RUB": 1.17,
    "KZT": 0.19,
    "USD": 87.43,
    "EUR": 102.75,
    "CNY": 12.82,
    "UZS": 0.0068,  # ~1 сом = 147 сумов
}


def fetch_nbkr_rates(timeout: float = 15.0) -> dict[str, float]:
    """Тянем официальный XML НБКР, возвращаем {ISO: rate_to_kgs}.

    При ошибке логируем и возвращаем пустой dict (вызывающий код делает фолбек).
    """
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True) as c:
            r = c.get(NBKR_URL)
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("nbkr: fetch failed: %s", e)
        return {}

    try:
        # XML приходит в windows-1251 — декодируем явно
        raw = r.content.decode("windows-1251", errors="replace")
    except Exception:
        raw = r.text

    rates: dict[str, float] = {"KGS": 1.0}
    # Паттерн: <Currency ISOCode="USD"> ... <Nominal>1</Nominal> ... <Value>87,4274</Value>
    for m in re.finditer(
        r'<Currency[^>]*ISOCode="([A-Z]{3})"[^>]*>\s*<Nominal>(\d+)</Nominal>\s*<Value>([\d.,]+)</Value>',
        raw,
    ):
        iso = m.group(1)
        try:
            nominal = int(m.group(2))
            value = float(m.group(3).replace(",", "."))
            if nominal <= 0 or value <= 0:
                continue
            rates[iso] = round(value / nominal, 6)
        except ValueError:
            continue
    log.info("nbkr: parsed %d currencies", len(rates))
    return rates


def update_rates_from_nbkr(db: Session) -> int:
    """Обновить БД актуальными курсами с НБКР. Возвращает число обновлённых валют."""
    rates = fetch_nbkr_rates()
    if not rates:
        return 0
    updated = 0
    now = datetime.utcnow()
    for code, value in rates.items():
        row = db.query(CurrencyRate).filter(CurrencyRate.currency == code).first()
        if row:
            row.rate_to_kgs = value
            row.updated_at = now
        else:
            db.add(CurrencyRate(currency=code, rate_to_kgs=value, updated_at=now))
        updated += 1
    db.commit()
    return updated


def get_rates(db: Session) -> dict[str, float]:
    rows = db.query(CurrencyRate).all()
    if not rows:
        return dict(DEFAULT_RATES)
    return {r.currency: r.rate_to_kgs for r in rows}


def to_kgs(amount: float, currency: str, rates: dict[str, float]) -> float:
    if currency == "KGS":
        return amount
    rate = rates.get(currency, DEFAULT_RATES.get(currency, 1.0))
    return round(amount * rate, 2)


def from_kgs(amount_kgs: float, target: str, rates: dict[str, float]) -> float:
    if target == "KGS":
        return round(amount_kgs, 2)
    rate = rates.get(target, DEFAULT_RATES.get(target, 1.0))
    if rate == 0:
        return 0.0
    return round(amount_kgs / rate, 2)


def format_price(amount: float, currency: str) -> str:
    symbols = {"KGS": "сом", "RUB": "₽", "KZT": "₸", "USD": "$", "EUR": "€", "UZS": "сум"}
    sym = symbols.get(currency, currency)
    return f"{amount:,.2f} {sym}".replace(",", " ")
