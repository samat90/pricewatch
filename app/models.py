"""ORM-модели БД."""
from datetime import datetime
from sqlalchemy import String, Integer, Float, ForeignKey, DateTime, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Country(Base):
    __tablename__ = "countries"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    name_ru: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(8))
    flag: Mapped[str] = mapped_column(String(16), default="")

    cities: Mapped[list["City"]] = relationship(back_populates="country", cascade="all, delete-orphan")


class City(Base):
    __tablename__ = "cities"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    name_ru: Mapped[str] = mapped_column(String(80), index=True)
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))

    country: Mapped[Country] = relationship(back_populates="cities")
    stores: Mapped[list["Store"]] = relationship(back_populates="city", cascade="all, delete-orphan")


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_ru: Mapped[str] = mapped_column(String(120))
    icon: Mapped[str] = mapped_column(String(64), default="basket")

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    """SKU — конкретная товарная позиция: бренд + вариант + вес/объём.

    Один Product объединяет предложения разных магазинов для одного и того же
    SKU. Сигнатура (slug) формируется из нормализованных полей (category, brand,
    weight, fat). Если два магазина продают один и тот же SKU — их цены
    попадают в один Product и мы их сравниваем корректно.
    """
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name_ru: Mapped[str] = mapped_column(String(250), index=True)       # полное название для показа
    slug: Mapped[str] = mapped_column(String(250), unique=True, index=True)  # = сигнатура
    brand: Mapped[str] = mapped_column(String(120), default="", index=True)
    variant: Mapped[str] = mapped_column(String(120), default="")
    weight_text: Mapped[str] = mapped_column(String(32), default="")     # «900 г», «1.5 л»
    weight_grams: Mapped[float] = mapped_column(Float, default=0.0)      # нормализовано в граммах/мл
    fat_text: Mapped[str] = mapped_column(String(16), default="")        # «2.5%», «82%»
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(16), default="шт")
    image_url: Mapped[str] = mapped_column(String(500), default="")
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))

    category: Mapped[Category] = relationship(back_populates="products")
    prices: Mapped[list["Price"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120), index=True)
    website: Mapped[str] = mapped_column(String(255), default="")
    logo: Mapped[str] = mapped_column(String(255), default="")
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"))

    city: Mapped[City] = relationship(back_populates="stores")
    prices: Mapped[list["Price"]] = relationship(back_populates="store", cascade="all, delete-orphan")


class Price(Base):
    __tablename__ = "prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    source_url: Mapped[str] = mapped_column(String(500), default="")

    product: Mapped[Product] = relationship(back_populates="prices")
    store: Mapped[Store] = relationship(back_populates="prices")

    __table_args__ = (
        Index("ix_prices_product_store_date", "product_id", "store_id", "recorded_at"),
    )


class CurrencyRate(Base):
    """Курс валюты относительно KGS (сколько KGS за 1 единицу валюты)."""
    __tablename__ = "currency_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), unique=True)
    rate_to_kgs: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
