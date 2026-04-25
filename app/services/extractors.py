"""Извлечение характеристик товара из произвольной строки-названия.

Используется при скрапинге: получаем строку вида
«СЕЛО ЗЕЛЕНОЕ молоко 2,5% 950 мл» и возвращаем набор:
    brand = "Село Зеленое"
    weight_text = "950 мл"
    weight_grams = 950.0   (для молока/воды 1 мл ≈ 1 г)
    fat_text = "2.5%"
    category_key = "moloko"

На основе этих полей в scraper формируется SKU-сигнатура, которая служит
уникальным идентификатором товара. Два магазина, продающие один и тот же
SKU, получают одинаковую сигнатуру и мы корректно сравниваем их цены.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Категории: ключевое слово → (slug, иконка). Используются при выборе категории
# для найденного товара. Ключи ищутся подстрокой в нижнем регистре названия.
CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    # (keyword, category_slug)
    ("молок",      "dairy"),
    ("кефир",      "dairy"),
    ("сметан",     "dairy"),
    ("йогурт",     "dairy"),
    ("биокефир",   "dairy"),
    ("биойогурт",  "dairy"),
    ("творог",     "dairy"),
    ("быштак",     "dairy"),
    ("масло слив", "dairy"),
    ("сливки",     "dairy"),
    ("сыр",        "dairy"),
    ("сырок",      "dairy"),
    ("сырогу",     "dairy"),
    ("яйц",        "dairy"),
    ("яйцо",       "dairy"),

    ("хлеб",       "bakery"),
    ("батон",      "bakery"),
    ("булк",       "bakery"),
    ("пампушк",    "bakery"),
    ("печень",     "sweets"),
    ("вафл",       "sweets"),
    ("пряник",     "sweets"),

    ("курин",      "meat"),
    ("курица",     "meat"),
    ("окорочк",    "meat"),
    ("филе кур",   "meat"),
    ("говяд",      "meat"),
    ("свинин",     "meat"),
    ("баранин",    "meat"),
    ("фарш",       "meat"),
    ("сосиск",     "meat"),
    ("колбас",     "meat"),
    ("сервелат",   "meat"),

    ("картофел",   "vegetables"),
    ("картошк",    "vegetables"),
    ("лук",        "vegetables"),
    ("морков",     "vegetables"),
    ("капуст",     "vegetables"),
    ("помидор",    "vegetables"),
    ("огурц",      "vegetables"),
    ("чеснок",     "vegetables"),
    ("перец",      "vegetables"),
    ("кабач",      "vegetables"),

    ("яблок",      "fruits"),
    ("банан",      "fruits"),
    ("апельсин",   "fruits"),
    ("лимон",      "fruits"),
    ("груш",       "fruits"),
    ("виноград",   "fruits"),

    ("рис",        "grocery"),
    ("гречк",      "grocery"),
    ("макарон",    "grocery"),
    ("вермишел",   "grocery"),
    ("лапш",       "grocery"),
    ("мук",        "grocery"),
    ("сахар",      "grocery"),
    ("соль",       "grocery"),
    ("масло подсол", "grocery"),
    ("подсолн",    "grocery"),
    ("майонез",    "grocery"),
    ("кетчуп",     "grocery"),
    ("уксус",      "grocery"),

    ("вода",       "drinks"),
    ("сок",        "drinks"),
    ("кока",       "drinks"),
    ("лимонад",    "drinks"),
    ("квас",       "drinks"),
    ("пиво",       "drinks"),
    ("чай",        "sweets"),
    ("кофе",       "sweets"),
    ("какао",      "sweets"),
    ("шокол",      "sweets"),
    ("конфет",     "sweets"),
    ("мёд",        "sweets"),
    ("мороженое",  "sweets"),
    ("балмуздаг",  "sweets"),
]

# Общие слова, которые НЕ могут быть брендом
_NON_BRAND = {
    "молоко", "кефир", "сметана", "йогурт", "творог", "сыр", "сырок",
    "хлеб", "батон", "вода", "сок", "чай", "кофе",
    "мука", "сахар", "соль", "рис", "гречка", "макароны",
    "филе", "фарш", "сосиски", "колбаса", "сервелат",
    "майонез", "кетчуп", "масло",
    "картофель", "лук", "морковь", "капуста", "огурцы", "помидоры",
    "яблоки", "бананы", "лимоны", "апельсины",
    "яйцо", "яйца",
    "в", "на", "для", "из", "с", "и", "по",
    "кг", "г", "гр", "мл", "л", "шт",
    "ассорт", "ассорти",
}


@dataclass
class ProductFeatures:
    """Извлечённые характеристики товара."""
    name: str              # полное «красивое» название для показа
    brand: str             # бренд («Чабрец», «Магнит»)
    weight_text: str       # «900 г», «1.5 л»
    weight_grams: float    # нормализованное значение в граммах/мл (условно)
    fat_text: str          # «2.5%», «82%»
    category_key: str      # slug категории («dairy», «bakery» …)


# ---- Публичные функции --------------------------------------------------

def extract_features(raw_title: str, category_hint: str = "") -> ProductFeatures:
    """Разобрать произвольное название товара на компоненты."""
    title = re.sub(r"\s+", " ", (raw_title or "").strip())
    category_key = detect_category(title) or category_hint
    brand = extract_brand(title)
    weight_text, weight_g = extract_weight(title)
    fat_text = extract_fat(title)
    # «красивое» имя — чуть подчищено
    name = _clean_display_name(title)
    return ProductFeatures(
        name=name,
        brand=brand,
        weight_text=weight_text,
        weight_grams=weight_g,
        fat_text=fat_text,
        category_key=category_key,
    )


def detect_category(title: str) -> str:
    lc = title.lower()
    for kw, cat in CATEGORY_KEYWORDS:
        if kw in lc:
            return cat
    return ""


def extract_brand(title: str) -> str:
    """Пытаемся определить бренд: сначала ищем в «…», потом CAPS-сегмент."""
    # 1) «…» кавычки
    m = re.search(r"«([^»\n]{2,60})»", title)
    if m:
        b = _clean_brand(m.group(1))
        if b:
            return b
    # 2) "…" двойные кавычки
    m = re.search(r'"([^"\n]{2,60})"', title)
    if m:
        b = _clean_brand(m.group(1))
        if b:
            return b
    # 3) CAPS или TitleCase в начале строки (2-5 слов)
    m = re.match(r"([A-ZА-ЯЁ][A-ZА-ЯЁ0-9\-\.&']{1,25}(?:\s+[A-ZА-ЯЁ][A-ZА-ЯЁ0-9\-\.&']{1,25}){0,3})\b", title)
    if m:
        b = _clean_brand(m.group(1))
        if b:
            return b
    return ""


def _clean_brand(raw: str) -> str:
    raw = raw.strip().strip(",. -")
    raw = re.sub(r"\s+", " ", raw)
    if not raw:
        return ""
    low = raw.lower()
    if low in _NON_BRAND or len(low) < 2:
        return ""
    # Title case (первая буква каждого слова большая)
    words = [w.capitalize() if w.isupper() and len(w) > 2 else w for w in raw.split(" ")]
    return " ".join(words)


# Паттерны единиц измерения и коэффициенты перевода в граммы/мл.
_WEIGHT_UNITS: list[tuple[str, float]] = [
    ("кг", 1000.0),
    ("л",  1000.0),
    ("гр", 1.0),
    ("г",  1.0),
    ("мл", 1.0),
    ("шт", 1.0),
    ("дес", 10.0),
    ("пак", 1.0),
]


def extract_weight(title: str) -> tuple[str, float]:
    """Ищем фрагмент «число + единица». Возвращаем человекочитаемый текст
    и нормализованное значение в граммах (или мл — мы их не разделяем,
    это всего лишь ключ для группировки SKU).
    """
    lc = title.lower()
    best: tuple[str, float] | None = None
    # Попробуем все юниты от длинного к короткому
    for unit, coef in _WEIGHT_UNITS:
        pattern = rf"(\d+(?:[.,]\d+)?)\s*{unit}\b"
        for m in re.finditer(pattern, lc):
            val = float(m.group(1).replace(",", "."))
            if val <= 0 or val > 100000:
                continue
            text = f"{_fmt_num(val)} {unit}"
            norm = val * coef
            if best is None or norm > best[1]:
                best = (text, norm)
        if best:
            break
    if best is None:
        return "", 0.0
    return best


def _fmt_num(val: float) -> str:
    if abs(val - round(val)) < 0.01:
        return str(int(round(val)))
    return f"{val:.2f}".rstrip("0").rstrip(".")


def extract_fat(title: str) -> str:
    """Находит фрагмент жирности/содержания: «2,5%», «72%»."""
    # Избегаем захвата скидок «-30%»
    for m in re.finditer(r"(?<!-)\b(\d{1,2}(?:[.,]\d)?)\s*%", title):
        val = float(m.group(1).replace(",", "."))
        if 0 < val <= 100:
            return f"{_fmt_num(val)}%"
    return ""


def _clean_display_name(title: str) -> str:
    """Лёгкая очистка: убрать дубли пробелов, обрезать слишком длинные."""
    t = re.sub(r"\s+", " ", title).strip(" ,.-/")
    return t[:220]


def build_signature(category_key: str, brand: str, weight_grams: float, fat_text: str, fallback: str = "") -> str:
    """Каноническая сигнатура SKU.

    Обязательно включает либо бренд, либо нормализованный слаг полного
    названия — чтобы разные товары без явного бренда не склеивались в один
    «sku-кефир-900». Одинаковые товары из разных магазинов (если у них
    совпадают и бренд, и вес/жирность, или совпадает весь title) схлопнутся
    в один Product — и мы корректно их сравним.
    """
    parts: list[str] = [category_key or "item"]
    if brand:
        parts.append(_slugify(brand))
    else:
        # без бренда — ключ из первых значимых слов названия
        parts.append(_slugify(fallback)[:50] or "anon")
    if weight_grams > 0:
        parts.append(f"w{int(round(weight_grams))}")
    if fat_text:
        parts.append("f" + fat_text.replace("%", "").replace(".", "-"))
    return "-".join(p for p in parts if p)


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Альтернативная транслитерация (ц → ts, х → kh, й → y и т.д.) —
# для совместимости с разными стандартами, используемыми сайтами.
_TRANSLIT_ALT = {
    **_TRANSLIT,
    "ц": "ts",
    "х": "kh",
    "ь": "y",
}

_TRANSLIT_Y = {
    **_TRANSLIT,
    "й": "y",   # globus-online.kg и многие другие: «яйцо» → «yayco»
}

_TRANSLIT_Y_TS = {
    **_TRANSLIT_Y,
    "ц": "ts",  # «яйцо» → «yaytso»
}


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "x"


def slugify_variants(text: str) -> list[str]:
    """Возвращает набор разных вариантов латинского слага для одного
    русского слова. Разные сайты используют разные стандарты транслитерации:
      ц → c / ts    (яйцо → yaico / yaitso)
      й → i / y     (яйцо → yaico / yayco)
      х → h / kh
    Для надёжного поиска в чужих sitemap'ах пробуем все комбинации.
    """
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text).lower()
    variants = set()
    for table in (_TRANSLIT, _TRANSLIT_ALT, _TRANSLIT_Y, _TRANSLIT_Y_TS):
        s = "".join(table.get(ch, ch) for ch in text)
        s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
        if s:
            variants.add(s)
    return list(variants)
