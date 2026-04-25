"""Pydantic-схемы для API."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class CountryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name_ru: str
    currency: str
    flag: str


class CityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name_ru: str
    country_id: int


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slug: str
    name_ru: str
    icon: str


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name_ru: str
    slug: str
    unit: str
    image_url: str
    category_id: int


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    website: str
    city_id: int


class PriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    store_id: int
    price: float
    currency: str
    recorded_at: datetime
    source_url: str


class PriceWithContext(BaseModel):
    price: float
    currency: str
    price_kgs: float
    store_name: str
    store_id: int
    city_name: str
    city_id: int
    country_name: str
    country_code: str
    recorded_at: datetime


class ProductSearchResult(BaseModel):
    product: ProductOut
    min_price_kgs: float
    max_price_kgs: float
    avg_price_kgs: float
    offers_count: int
    cheapest_city: str
    cheapest_store: str


class PriceHistoryPoint(BaseModel):
    date: str
    avg_price_kgs: float
    min_price_kgs: float
    max_price_kgs: float
