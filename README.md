# PriceWatch — онлайн-платформа анализа цен на продукты питания

Выставочный проект студентов группы ист2 (ОшГУ).

Веб-сайт для сравнения цен на продовольственные товары между городами Кыргызстана (Ош, Бишкек, Джалал-Абад, Каракол), России (Москва, СПб, Новосибирск, Казань) и Казахстана (Алматы, Астана).

## Возможности

- Поиск товара по названию с фильтрацией по городу и стране
- Сравнение цен на один товар между всеми магазинами
- Сравнение цен между городами/странами с конвертацией валют
- График истории цен за последние 60 дней
- Статистика: минимальная, средняя, максимальная цена; самый дешёвый магазин
- Рейтинг самых дорогих/дешёвых городов по корзине продуктов
- Админ-панель: добавление товаров, ручной запуск сбора цен

## Технологии

- **Backend:** FastAPI, SQLAlchemy 2, SQLite, Pydantic v2
- **Frontend:** Jinja2, Bootstrap 5, Chart.js, Bootstrap Icons
- **Сбор данных:** httpx + BeautifulSoup (парсер сайтов)

## Запуск

```bash
# 1. Создать виртуальное окружение
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux/Mac

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Наполнить БД демо-данными
python -m scripts.seed

# 4. Запустить сервер
python run.py
```

Открыть в браузере: http://127.0.0.1:8000

## Архитектура

```
app/
├── main.py          # Точка входа FastAPI
├── config.py        # Конфигурация
├── database.py      # SQLAlchemy engine + session
├── models.py        # ORM-модели
├── schemas.py       # Pydantic-схемы
├── routers/         # HTTP-роутеры
│   ├── pages.py     # HTML-страницы
│   ├── api.py       # JSON API
│   └── admin.py     # Админ-панель
├── services/        # Бизнес-логика
│   ├── search.py
│   ├── analytics.py
│   ├── currency.py
│   └── scraper.py
├── templates/       # Jinja2
└── static/          # CSS/JS/img
scripts/
└── seed.py          # Генератор демо-данных
data/prices.db       # SQLite БД
```

## Авторы

Студенты группы ист2, ОшГУ, 2026 г.
