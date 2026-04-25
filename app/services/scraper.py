"""Сбор цен со сторонних сайтов.

Реальные источники:
  • Народный (market.kg) — официальная PDF-газета, парсится через pdfplumber
  • Глобус онлайн (globus-online.kg) — JSON-LD Product на страницах товаров,
    URL-ы берём из sitemap-ru-product.xml
  • Магнит (magnit.ru) — JSON-LD OfferCatalog на категорийных страницах

Оркестратор `Scraper.scrape_all`:
  1. Запускает каждый парсер, получает сырые офферы.
  2. Для каждого оффера через `extractors.extract_features` достаёт
     бренд / вес / жирность / категорию.
  3. Строит сигнатуру (SKU-ID) и находит/создаёт `Product` в БД.
  4. Пишет `Price`, привязанный к этому Product и нужному Store.

Поэтому цены в карточке товара — это всегда один и тот же SKU, а не просто
совпадение по слову «молоко».
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.models import Category, City, Country, Price, Product, Store
from app.services.extractors import (
    ProductFeatures,
    _slugify,
    build_signature,
    detect_category,
    extract_features,
    slugify_variants,
)

log = logging.getLogger("scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30


@dataclass
class ScrapedOffer:
    """Сырой оффер из магазина."""
    title: str                 # полное название
    price: float
    currency: str
    category_hint: str = ""   # категория, если парсер знает
    brand_hint: str = ""      # если магазин отдаёт бренд отдельно
    weight_hint: str = ""
    image: str = ""
    url: str = ""
    old_price: float = 0.0


@dataclass
class ParserResult:
    parser_slug: str
    parser_name: str
    country_code: str
    city_name: str
    offers: list[ScrapedOffer] = field(default_factory=list)


# ============================================================================
# Базовый класс
# ============================================================================

class BaseParser:
    slug: str = ""
    store_name: str = ""
    country_code: str = ""
    default_city: str = ""
    currency: str = ""
    base_url: str = ""

    def fetch(self, client: httpx.Client) -> ParserResult:
        raise NotImplementedError


# ============================================================================
# Глобус онлайн (КР)
# ============================================================================

class GlobusOnlineParser(BaseParser):
    slug = "globus_online"
    store_name = "Глобус"
    country_code = "KG"
    default_city = "Бишкек"
    currency = "KGS"
    base_url = "https://globus-online.kg"
    SITEMAP_URL = "https://globus-online.kg/sitemaps/sitemap-ru-product.xml"

    # In-process кэш sitemap URLов (обновляется раз в час)
    _sitemap_cache: tuple[float, list[str]] | None = None  # (timestamp, urls)
    _SITEMAP_TTL_SEC = 3600

    # Берём по N товаров из каждой категории-ключа (чтобы и обхват был, и быстро)
    CATEGORY_HINTS: list[tuple[str, tuple[str, ...], str]] = [
        # (category_key, url_keywords (all must match), max_urls)
        ("dairy",      ("moloko",),          4),
        ("dairy",      ("kefir",),           3),
        ("dairy",      ("smetana",),         3),
        ("dairy",      ("yogurt",),          4),
        ("dairy",      ("iogurt",),          3),
        ("dairy",      ("tvorog",),          3),
        ("dairy",      ("syr",),             5),
        ("dairy",      ("maslo", "sliv"),    3),
        ("dairy",      ("yayco",),           2),
        ("dairy",      ("yayc",),            2),
        ("bakery",     ("hleb",),            4),
        ("bakery",     ("baton",),           2),
        ("meat",       ("kurica",),          3),
        ("meat",       ("file", "kur"),      2),
        ("meat",       ("govyad",),          2),
        ("meat",       ("sosiski",),         3),
        ("meat",       ("kolbasa",),         3),
        ("meat",       ("farsh",),           2),
        ("vegetables", ("kartofel",),        2),
        ("vegetables", ("luk",),             2),
        ("vegetables", ("morkov",),          2),
        ("vegetables", ("kapusta",),         3),
        ("vegetables", ("pomidor",),         2),
        ("vegetables", ("ogurc",),           2),
        ("vegetables", ("chesnok",),         2),
        ("fruits",     ("yablok",),          3),
        ("fruits",     ("banany",),          1),
        ("fruits",     ("apelsin",),         2),
        ("fruits",     ("limon",),           2),
        ("grocery",    ("ris-",),            3),
        ("grocery",    ("grechka",),         2),
        ("grocery",    ("makarony",),        4),
        ("grocery",    ("muka",),            2),
        ("grocery",    ("sahar",),           2),
        ("grocery",    ("sol-",),            2),
        ("grocery",    ("maslo-podsol",),    3),
        ("grocery",    ("mayonez",),         3),
        ("drinks",     ("voda",),            3),
        ("drinks",     ("sok-",),            3),
        ("sweets",     ("chay",),            3),
        ("sweets",     ("kofe",),            3),
        ("sweets",     ("shokolad",),        3),
        ("sweets",     ("myod",),            1),
        ("sweets",     ("med-",),            1),
    ]
    MAX_URLS_TOTAL = 140

    def _load_sitemap(self, client: httpx.Client) -> list[str]:
        import time
        now = time.time()
        if self.__class__._sitemap_cache and now - self.__class__._sitemap_cache[0] < self._SITEMAP_TTL_SEC:
            return self.__class__._sitemap_cache[1]
        try:
            r = client.get(self.SITEMAP_URL)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("globus-online: sitemap fetch failed: %s", e)
            return []
        urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
        urls = [u.replace("//products/", "/products/") for u in urls if "/products/" in u]
        self.__class__._sitemap_cache = (now, urls)
        log.info("globus-online: sitemap cached %d products", len(urls))
        return urls

    def live_search(self, client: httpx.Client, query: str, limit: int = 12) -> list[ScrapedOffer]:
        """Поиск под конкретный запрос: транслитерируем, ищем в URL sitemap.

        Используем несколько вариантов транслитерации (ц→c и ц→ts, х→h и х→kh),
        чтобы надёжно находить товары под разными стандартами слагов.
        """
        urls = self._load_sitemap(client)
        if not urls or not query.strip():
            return []
        full_variants = [n for n in slugify_variants(query.strip()) if len(n) >= 3]
        if not full_variants:
            return []
        # Корни (без последних 2 букв) — ловим разные окончания:
        # «яйцо/яйца/яиц» → корень «yayc» / «yayts»
        root_needles = set()
        for v in full_variants:
            root_needles.add(v)
            if len(v) >= 5:
                root_needles.add(v[:-1])
                root_needles.add(v[:-2])
        root_needles = [n for n in root_needles if len(n) >= 4]

        def score_url(u: str) -> int:
            """URL-скор: чем ближе корень к началу slug'а продукта, тем лучше.

            /products/yayco-kurinoe-10sht  → 100 (slug начинается с корня — это то что надо)
            /products/shokoladnoe-yayco-... → 30 (в середине)
            """
            ul = u.lower()
            # Извлекаем slug после /products/
            m = re.search(r"/products/([^?/]+)", ul)
            slug = m.group(1) if m else ul
            best = 0
            for n in root_needles:
                if slug.startswith(n):
                    best = max(best, 100)
                elif "-" + n in slug:                     # корень как отдельное слово
                    best = max(best, 60)
                elif n in slug:
                    best = max(best, 20)
            return best

        scored: list[tuple[int, str]] = []
        seen: set[str] = set()
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            s = score_url(u)
            if s > 0:
                scored.append((s, u))
        scored.sort(key=lambda t: -t[0])
        matched = [u for _s, u in scored[:limit]]
        log.info("globus-online live-search %r (roots=%s) -> %d matches, top3 scores=%s",
                 query, sorted(root_needles), len(scored), [s for s,_ in scored[:3]])
        offers: list[ScrapedOffer] = []
        for u in matched:
            try:
                r = client.get(u)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            offer = self._parse_product(r.text, u, "")
            if offer:
                offers.append(offer)
        log.info("globus-online live-search %r -> parsed %d offers", query, len(offers))
        return offers

    def fetch(self, client: httpx.Client) -> ParserResult:
        urls = self._load_sitemap(client)
        if not urls:
            return ParserResult(self.slug, self.store_name, self.country_code, self.default_city)

        selected: list[tuple[str, str]] = []  # (category_hint, url)
        seen: set[str] = set()
        for cat, kws, limit in self.CATEGORY_HINTS:
            matched = [u for u in urls if all(k in u.lower() for k in kws)]
            for u in matched[:limit]:
                if u in seen:
                    continue
                seen.add(u)
                selected.append((cat, u))
                if len(selected) >= self.MAX_URLS_TOTAL:
                    break
            if len(selected) >= self.MAX_URLS_TOTAL:
                break

        log.info("globus-online: %d URLs selected for product scraping", len(selected))
        offers: list[ScrapedOffer] = []
        for cat, url in selected:
            try:
                pr = client.get(url)
            except httpx.HTTPError as e:
                log.debug("globus-online: err %s: %s", url, e)
                continue
            if pr.status_code != 200:
                continue
            offer = self._parse_product(pr.text, url, cat)
            if offer:
                offers.append(offer)
        log.info("globus-online: parsed %d offers", len(offers))
        return ParserResult(self.slug, self.store_name, self.country_code, self.default_city, offers)

    def _parse_product(self, html: str, page_url: str, category_hint: str) -> ScrapedOffer | None:
        blocks = re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.DOTALL)
        for raw in blocks:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict) or data.get("@type") != "Product":
                continue
            name = (data.get("name") or "").strip()
            if not name:
                continue
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                continue
            try:
                price = float(str(offers.get("price") or 0).replace(",", "."))
            except ValueError:
                continue
            if price <= 0:
                continue
            currency = offers.get("priceCurrency") or self.currency
            img = data.get("image")
            if isinstance(img, dict):
                img = img.get("url") or img.get("contentUrl") or ""
            elif isinstance(img, list):
                img = img[0] if img else ""
            weight_hint = ""
            w = data.get("weight")
            if isinstance(w, dict):
                val = w.get("minValue") or w.get("value")
                unit = (w.get("unitText") or w.get("unitCode") or "").lower()
                if val and unit:
                    weight_hint = f"{val} {unit}"
            return ScrapedOffer(
                title=name,
                price=round(price, 2),
                currency=currency,
                category_hint=category_hint,
                weight_hint=weight_hint,
                image=img if isinstance(img, str) else "",
                url=page_url,
            )
        return None


# ============================================================================
# Народный (КР) — PDF-газета
# ============================================================================

class NarodnyParser(BaseParser):
    slug = "narodny"
    store_name = "Народный"
    country_code = "KG"
    default_city = "Бишкек"
    currency = "KGS"
    base_url = "https://market.kg"
    PDF_URL = "https://market.kg/catalog/download/358"

    def fetch(self, client: httpx.Client) -> ParserResult:
        try:
            r = client.get(self.PDF_URL)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("narodny: pdf download failed: %s", e)
            return ParserResult(self.slug, self.store_name, self.country_code, self.default_city)

        import io as _io
        import pdfplumber
        offers: list[ScrapedOffer] = []
        with pdfplumber.open(_io.BytesIO(r.content)) as pdf:
            for page in pdf.pages:
                offers.extend(self._parse_page(page))
        log.info("narodny: extracted %d offers", len(offers))
        return ParserResult(self.slug, self.store_name, self.country_code, self.default_city, offers)

    def _parse_page(self, page) -> list[ScrapedOffer]:
        words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
        anchors = [w for w in words if re.fullmatch(r"-\d{1,2}%", w["text"])]
        result: list[ScrapedOffer] = []
        for a in anchors:
            o = self._parse_card(a, words, page.width, page.height)
            if o:
                result.append(o)
        return result

    def _parse_card(self, anchor, words, page_w, page_h) -> ScrapedOffer | None:
        card_w = page_w / 3 + 30
        card_h = page_h / 4
        x0 = anchor["x0"] - 20
        y0 = anchor["top"] - 10
        x1 = x0 + card_w
        y1 = y0 + card_h
        card = [w for w in words if w["x0"] >= x0 and w["x1"] <= x1 and w["top"] >= y0 and w["bottom"] <= y1]
        if not card:
            return None

        prices = sorted(
            (w for w in card if re.fullmatch(r"\d{3,6}", w["text"])),
            key=lambda w: (w["top"], w["x0"]),
        )
        if len(prices) < 2:
            return None
        v1 = int(prices[0]["text"]) / 100.0
        v2 = int(prices[1]["text"]) / 100.0
        old_p, new_p = (v1, v2) if v1 >= v2 else (v2, v1)
        if new_p >= old_p or new_p <= 0 or new_p > 100000:
            return None

        text_words = [w["text"] for w in card
                      if not re.fullmatch(r"-?\d+(?:[.,]\d+)?%?", w["text"])]
        raw_text = re.sub(r"\s+", " ", " ".join(text_words))

        # Строим «красивый» заголовок:
        # Бренд в «…»; добавляем русское описание после «/»
        title_bits: list[str] = []
        for m in re.finditer(r"«([^»]+)»", raw_text):
            title_bits.append(m.group(1).strip())
        # Русская часть описания — после «/»
        russian_tokens: list[str] = []
        for token in raw_text.split():
            # отбрасываем кыргызские специфичные буквы, оставляя русские слова
            if re.match(r"^[А-ЯЁа-яё][А-ЯЁа-яё\-]+$", token) and len(token) >= 3:
                low = token.lower()
                if low in {"биздикин", "сатып", "өзүбүздүн", "покупай", "местное", "карта",
                           "более", "не", "эмес"}:
                    continue
                russian_tokens.append(token)
        # Вес/жирность
        weight_m = re.search(r"(\d+(?:[.,]\d+)?)\s*(г|гр|кг|мл|л|шт)\b", raw_text, re.IGNORECASE)
        weight = weight_m.group(0) if weight_m else ""
        fat_m = re.search(r"(?<!-)\b(\d{1,2}(?:[.,]\d)?)\s*%", raw_text)
        fat = fat_m.group(0) if fat_m else ""

        # Собираем title: бренд + первое осмысленное описание + вес + жирность
        title_parts: list[str] = list(dict.fromkeys(title_bits))
        for tok in russian_tokens[:3]:
            if tok.lower() not in (p.lower() for p in title_parts):
                title_parts.append(tok)
        if fat and fat not in " ".join(title_parts):
            title_parts.append(fat)
        if weight:
            title_parts.append(weight)
        title = " ".join(title_parts)[:200] if title_parts else raw_text[:200]

        return ScrapedOffer(
            title=title,
            price=round(new_p, 2),
            currency=self.currency,
            brand_hint=title_bits[0] if title_bits else "",
            weight_hint=weight,
            old_price=round(old_p, 2),
        )


# ============================================================================
# Магнит (РФ)
# ============================================================================

class MagnitParser(BaseParser):
    slug = "magnit"
    store_name = "Магнит"
    country_code = "RU"
    default_city = "Краснодар"
    currency = "RUB"
    base_url = "https://magnit.ru"

    # (url, category_hint)
    CATEGORIES: list[tuple[str, str]] = [
        ("/catalog/65003-testmmkhleb",                  "bakery"),
        ("/catalog/63983-testmmmoloko_maslo_yaytsa",    "dairy"),
        ("/catalog/63985-testmmyogurty_i_deserty",      "dairy"),
        ("/catalog/63987-testmmkefir_smetana_tvorog",   "dairy"),
        ("/catalog/63991-testmmsyry",                   "dairy"),
        ("/catalog/64247-testmmmyaso",                  "meat"),
        ("/catalog/64245-testmmptitsa",                 "meat"),
        ("/catalog/64249-testmmkolbasy_i_sosiski",      "meat"),
        ("/catalog/63921-testmmovoshchi_griby_zelen",   "vegetables"),
        ("/catalog/63929-testmmfrukty_i_yagody",        "fruits"),
        ("/catalog/64123-testmmkrupy_i_sukhie_zavtraki","grocery"),
        ("/catalog/64125-testmmmakarony",               "grocery"),
        ("/catalog/64127-testmmrastitelnye_masla",      "grocery"),
        ("/catalog/64129-testmmsakhar_sol_i_spetsii",   "grocery"),
        ("/catalog/63793-testmmvoda",                   "drinks"),
        ("/catalog/63797-testmmsoki_i_morsy",           "drinks"),
        ("/catalog/63875-testmmchay",                   "sweets"),
        ("/catalog/63877-testmmkofe",                   "sweets"),
        ("/catalog/64713-testmmkonfety_i_shokolad",     "sweets"),
        ("/catalog/64717-testmmvarene_i_med",           "sweets"),
    ]

    def fetch(self, client: httpx.Client) -> ParserResult:
        offers: list[ScrapedOffer] = []
        city = self.default_city
        for path, category_hint in self.CATEGORIES:
            try:
                r = client.get(f"{self.base_url}{path}")
            except httpx.HTTPError as e:
                log.warning("magnit: %s err %s", path, e)
                continue
            if r.status_code != 200:
                continue
            parsed, detected_city = self._parse_ld_json(r.text, category_hint)
            if detected_city:
                city = detected_city
            offers.extend(parsed)
            log.info("magnit: %s (%s) -> %d offers", path, category_hint, len(parsed))
        # Dedup by (title, price)
        seen: set[tuple[str, float]] = set()
        uniq: list[ScrapedOffer] = []
        for o in offers:
            k = (o.title, o.price)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(o)
        return ParserResult(self.slug, self.store_name, self.country_code, city, uniq)

    def _parse_ld_json(self, html: str, category_hint: str) -> tuple[list[ScrapedOffer], str]:
        soup = BeautifulSoup(html, "lxml")
        offers: list[ScrapedOffer] = []
        city_hint = ""
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict) or data.get("@type") != "OfferCatalog":
                continue
            for item in data.get("itemListElement") or []:
                if not isinstance(item, dict) or item.get("@type") != "Offer":
                    continue
                try:
                    price = float(str(item.get("price") or "").replace(",", "."))
                except ValueError:
                    continue
                if price <= 0:
                    continue
                title = (item.get("name") or "").strip()
                if not title:
                    continue
                desc = item.get("description") or ""
                m = re.search(r"([А-ЯЁ][а-яё\-]+)\s*г[,.]", desc)
                if m:
                    city_hint = m.group(1)
                img = item.get("image")
                if isinstance(img, list):
                    img = img[0] if img else ""
                offers.append(ScrapedOffer(
                    title=title,
                    price=round(price, 2),
                    currency=self.currency,
                    category_hint=category_hint,
                    url=item.get("url") or "",
                    image=img if isinstance(img, str) else "",
                ))
        return offers, city_hint


# ============================================================================
# Playwright-based парсеры (для SPA сайтов)
# ============================================================================

class _PlaywrightHelper:
    """Запускает headless Chromium, даёт контекст page и вспомогательные методы."""

    @staticmethod
    def run(category_urls: list[str], extract_script: str, per_page_timeout: int = 30000) -> list[dict]:
        """Обойти список URL'ов, на каждом выполнить JS-извлечение товаров."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("playwright not installed; skipping")
            return []
        all_items: list[dict] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="ru-RU",
            )
            page = ctx.new_page()
            for url in category_urls:
                try:
                    page.goto(url, wait_until="networkidle", timeout=per_page_timeout)
                    page.wait_for_timeout(1500)
                    # подскроллим, чтобы подгрузились lazy-элементы
                    for _ in range(3):
                        page.mouse.wheel(0, 1500)
                        page.wait_for_timeout(500)
                    items = page.evaluate(extract_script)
                    if items:
                        for it in items:
                            it["_source_url"] = url
                        all_items.extend(items)
                except Exception as e:
                    log.info("playwright error on %s: %s", url, e)
            browser.close()
        return all_items


class VkusvillParser(BaseParser):
    """Парсер ВкусВилл (РФ, Москва). Требует Playwright."""
    slug = "vkusvill"
    store_name = "ВкусВилл"
    country_code = "RU"
    default_city = "Москва"
    currency = "RUB"
    base_url = "https://vkusvill.ru"

    CATEGORY_URLS: list[str] = [
        "https://vkusvill.ru/goods/moloko-kefir-yogurty-smetana/",
        "https://vkusvill.ru/goods/hleb-i-pirogi/",
        "https://vkusvill.ru/goods/syry/",
        "https://vkusvill.ru/goods/myaso-ptitsa-kolbasy/",
        "https://vkusvill.ru/goods/ovoshchi-zelen/",
        "https://vkusvill.ru/goods/frukty-orekhi-sukhofrukty/",
        "https://vkusvill.ru/goods/bakaleya-konservy/",
        "https://vkusvill.ru/goods/napitki/",
        "https://vkusvill.ru/goods/sladosti-i-deserty/",
    ]

    EXTRACT_JS = r"""
() => {
    const cards = document.querySelectorAll('.ProductCards__item');
    return Array.from(cards).slice(0, 30).map(card => {
        // Название — попытаемся из alt картинки или slug URL
        const img = card.querySelector('img');
        const link = card.querySelector('a[href*="/goods/"]');
        const priceEl = card.querySelector('[class*=Price]');
        let title = img?.alt?.trim() || '';
        if (!title && link?.href) {
            const m = link.href.match(/\/goods\/([^/]+?)(?:-\d+)?\.html?$/);
            if (m) title = m[1].replace(/-/g, ' ');
        }
        return {
            title: title,
            price_text: priceEl?.textContent?.trim() || '',
            image: img?.src || '',
            url: link?.href || '',
        };
    });
}
    """

    def fetch(self, client: httpx.Client) -> ParserResult:
        raw = _PlaywrightHelper.run(self.CATEGORY_URLS, self.EXTRACT_JS)
        log.info("vkusvill playwright: got %d raw items", len(raw))
        offers: list[ScrapedOffer] = []
        for it in raw:
            price = _extract_price_rub(it.get("price_text") or "")
            if price <= 0:
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            offers.append(ScrapedOffer(
                title=title,
                price=price,
                currency=self.currency,
                image=it.get("image") or "",
                url=it.get("url") or "",
            ))
        log.info("vkusvill: %d offers parsed", len(offers))
        return ParserResult(self.slug, self.store_name, self.country_code, self.default_city, offers)


class MagnumKzParser(BaseParser):
    """Парсер Magnum Cash & Carry (Казахстан, Алматы). Playwright."""
    slug = "magnum"
    store_name = "Magnum"
    country_code = "KZ"
    default_city = "Алматы"
    currency = "KZT"
    base_url = "https://magnum.kz"

    CATEGORY_URLS: list[str] = [
        "https://magnum.kz/ru/catalog/molochnye-produkty",
        "https://magnum.kz/ru/catalog/khleb-i-vypechka",
        "https://magnum.kz/ru/catalog/myaso-i-ptitsa",
        "https://magnum.kz/ru/catalog/ovoshchi-i-frukty",
        "https://magnum.kz/ru/catalog/bakaleya",
        "https://magnum.kz/ru/catalog/napitki",
        "https://magnum.kz/ru/catalog/konditerskie-izdeliya",
    ]

    EXTRACT_JS = r"""
() => {
    // Magnum использует разные селекторы; ищем всё похожее на карточку
    const cards = document.querySelectorAll('[class*="product" i] a, [class*="Product" i] a, .product-card, .ProductCard');
    const seen = new Set();
    const items = [];
    document.querySelectorAll('a[href*="/product"], a[href*="/catalog/"]').forEach(a => {
        const card = a.closest('[class*="product" i], [class*="Product" i]') || a;
        if (seen.has(a.href) || items.length > 30) return;
        seen.add(a.href);
        const priceEl = card.querySelector('[class*=price], [class*=Price], [data-price]');
        const titleEl = card.querySelector('[class*=title], [class*=Title], [class*=name], [class*=Name], h3, h4') || card;
        const img = card.querySelector('img');
        items.push({
            title: (titleEl?.textContent || '').trim().slice(0, 200),
            price_text: (priceEl?.textContent || '').trim(),
            image: img?.src || '',
            url: a.href,
        });
    });
    return items;
}
    """

    def fetch(self, client: httpx.Client) -> ParserResult:
        raw = _PlaywrightHelper.run(self.CATEGORY_URLS, self.EXTRACT_JS)
        log.info("magnum playwright: got %d raw items", len(raw))
        offers: list[ScrapedOffer] = []
        for it in raw:
            price = _extract_price_any(it.get("price_text") or "")
            if price <= 0:
                continue
            title = (it.get("title") or "").strip()
            # отбрасываем слишком короткие / служебные
            if len(title) < 4:
                continue
            offers.append(ScrapedOffer(
                title=title,
                price=price,
                currency=self.currency,
                image=it.get("image") or "",
                url=it.get("url") or "",
            ))
        log.info("magnum: %d offers parsed", len(offers))
        return ParserResult(self.slug, self.store_name, self.country_code, self.default_city, offers)


def _extract_price_rub(text: str) -> float:
    """Извлечь цену в рублях из строки вроде '640 руб/шт' или '200,00 ₽'."""
    m = re.search(r"(\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.)", text)
    if not m:
        return 0.0
    raw = m.group(1).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return 0.0


def _extract_price_any(text: str) -> float:
    """Общий экстрактор цены: берём первое число, похожее на цену."""
    text = text.replace("\u00a0", " ").replace(" ", " ")
    m = re.search(r"(\d{1,3}(?:[\s,]\d{3})*(?:[.,]\d{1,2})?)", text)
    if not m:
        return 0.0
    raw = m.group(1).replace(" ", "").replace(",", ".")
    try:
        v = float(raw)
        return round(v, 2) if 0 < v < 1_000_000 else 0.0
    except ValueError:
        return 0.0


# Основной список. Playwright-парсеры в конце — они медленнее.
# MagnumKzParser временно отключён: magnum.kz требует
# длительного ожидания и геопривязки.
ALL_PARSERS: list[BaseParser] = [
    GlobusOnlineParser(),
    NarodnyParser(),
    MagnitParser(),
    VkusvillParser(),
]


# ============================================================================
# Оркестратор
# ============================================================================

class Scraper:
    def __init__(self, db: Session, parsers: list[BaseParser] | None = None):
        self.db = db
        self.parsers = parsers or ALL_PARSERS

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers={"User-Agent": USER_AGENT,
                     "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml"},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    # ---- Справочники ----------------------------------------------------

    def _ensure_city_and_store(self, parser: BaseParser, city_name: str) -> Store:
        country = self.db.query(Country).filter(Country.code == parser.country_code).first()
        if not country:
            raise RuntimeError(f"Country {parser.country_code} missing")
        city = (
            self.db.query(City)
            .filter(City.country_id == country.id, City.name_ru == city_name)
            .first()
        )
        if not city:
            city = City(name=city_name, name_ru=city_name, country_id=country.id)
            self.db.add(city)
            self.db.flush()
        slug = f"{parser.slug}-{city.id}"
        store = self.db.query(Store).filter(Store.slug == slug).first()
        if not store:
            store = Store(
                slug=slug,
                name=f"{parser.store_name} ({city_name})",
                website=parser.base_url,
                city_id=city.id,
            )
            self.db.add(store)
            self.db.flush()
        return store

    def _ensure_category(self, key: str) -> Category:
        cat = self.db.query(Category).filter(Category.slug == key).first()
        if cat:
            return cat
        # fallback категория
        names = {
            "dairy":      ("Молочные продукты", "cup-straw"),
            "bakery":     ("Хлеб и выпечка",    "basket2-fill"),
            "meat":       ("Мясо и птица",      "egg-fried"),
            "vegetables": ("Овощи",             "tree"),
            "fruits":     ("Фрукты",            "apple"),
            "grocery":    ("Бакалея",           "box-seam"),
            "drinks":     ("Напитки",           "cup-hot"),
            "sweets":     ("Сладости и чай",    "cup"),
            "other":      ("Прочее",            "basket"),
        }
        name, icon = names.get(key, ("Прочее", "basket"))
        cat = Category(slug=key or "other", name_ru=name, icon=icon)
        self.db.add(cat)
        self.db.flush()
        return cat

    # ---- Ядро -----------------------------------------------------------

    def _upsert_product(self, features: ProductFeatures, fallback_title: str, image: str) -> Product:
        sig = build_signature(features.category_key, features.brand, features.weight_grams, features.fat_text, fallback=fallback_title)
        product = self.db.query(Product).filter(Product.slug == sig).first()
        if product:
            # Обновляем поля, если пусты
            if not product.image_url and image:
                product.image_url = image
            return product

        category = self._ensure_category(features.category_key or "other")
        # Если бренд распознан — формируем аккуратное имя из полей.
        # Иначе используем полное оригинальное название (оно точнее).
        if features.brand:
            nice_bits = [features.brand]
            type_word = _leading_type_word(fallback_title, features.brand)
            if type_word:
                nice_bits.append(type_word)
            if features.fat_text:
                nice_bits.append(features.fat_text)
            if features.weight_text:
                nice_bits.append(features.weight_text)
            display = " ".join(nice_bits)
        else:
            display = fallback_title
        display = display[:230].strip()

        product = Product(
            slug=sig,
            name_ru=display,
            brand=features.brand,
            weight_text=features.weight_text,
            weight_grams=features.weight_grams,
            fat_text=features.fat_text,
            image_url=image,
            category_id=category.id,
        )
        self.db.add(product)
        self.db.flush()
        return product

    def live_search_query(self, query: str) -> int:
        """Ищет конкретный запрос в реальном времени у Глобуса.

        Для Магнита и Народного live-поиск невозможен технически
        (Магнит рендерит поиск на клиенте, Народный публикует только PDF).
        Поэтому live-search использует sitemap Глобуса.
        """
        q = (query or "").strip()
        if len(q) < 2:
            return 0
        now = datetime.utcnow()
        saved = 0
        with self._client() as client:
            # используем только Globus для live-поиска
            globus = next((p for p in self.parsers if isinstance(p, GlobusOnlineParser)), None)
            if not globus:
                return 0
            offers = globus.live_search(client, q)
            if not offers:
                return 0
            store = self._ensure_city_and_store(globus, globus.default_city)
            for offer in offers:
                features = extract_features(offer.title, offer.category_hint)
                if not features.brand and offer.brand_hint:
                    features = ProductFeatures(
                        name=features.name,
                        brand=offer.brand_hint.strip(),
                        weight_text=features.weight_text or offer.weight_hint,
                        weight_grams=features.weight_grams,
                        fat_text=features.fat_text,
                        category_key=features.category_key or offer.category_hint,
                    )
                if not features.category_key and not features.brand and not features.weight_text:
                    continue
                product = self._upsert_product(features, offer.title, offer.image)
                self.db.add(Price(
                    product_id=product.id,
                    store_id=store.id,
                    price=offer.price,
                    currency=offer.currency,
                    recorded_at=now,
                    source_url=offer.url,
                ))
                saved += 1
        self.db.commit()
        log.info("live-search %r: %d prices saved", query, saved)
        return saved

    def scrape_all(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        now = datetime.utcnow()

        with self._client() as client:
            for parser in self.parsers:
                try:
                    result = parser.fetch(client)
                except Exception:
                    log.exception("%s: fetch failed", parser.slug)
                    stats[parser.slug] = 0
                    continue

                store = self._ensure_city_and_store(parser, result.city_name)
                saved = 0
                for offer in result.offers:
                    features = extract_features(offer.title, offer.category_hint)
                    # если парсер подсказал бренд — предпочитаем его
                    if not features.brand and offer.brand_hint:
                        features = ProductFeatures(
                            name=features.name,
                            brand=offer.brand_hint.strip(),
                            weight_text=features.weight_text or offer.weight_hint,
                            weight_grams=features.weight_grams,
                            fat_text=features.fat_text,
                            category_key=features.category_key or offer.category_hint,
                        )
                    # Пропускаем откровенный мусор (нет ни бренда, ни веса, ни жирности)
                    if not features.category_key and not features.brand and not features.weight_text:
                        continue
                    product = self._upsert_product(features, offer.title, offer.image)
                    self.db.add(Price(
                        product_id=product.id,
                        store_id=store.id,
                        price=offer.price,
                        currency=offer.currency,
                        recorded_at=now,
                        source_url=offer.url,
                    ))
                    saved += 1
                stats[parser.slug] = saved
                log.info("%s: %d offers saved (out of %d)", parser.slug, saved, len(result.offers))
        self.db.commit()
        return stats

    # совместимость
    scrape_catalog = scrape_all


def _leading_type_word(title: str, brand: str) -> str:
    """Находим ведущее родовое слово: 'молоко', 'хлеб', 'творог' и т.д."""
    cleaned = re.sub(r"«[^»]+»", " ", title)
    if brand:
        cleaned = cleaned.replace(brand, " ")
    words = re.findall(r"[А-Яа-яЁё]{3,}", cleaned)
    skip = {"ассорт", "ассорти", "салм", "ашык", "эмес", "биздикин", "сатып",
            "местное", "покупай", "гост", "тоңдурулган"}
    for w in words:
        low = w.lower()
        if low in skip:
            continue
        return low
    return ""
