"""FastAPI приложение PriceWatch."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import APP_NAME, APP_DESCRIPTION, APP_VERSION, STATIC_DIR
from app.database import Base, engine
from app.routers import pages, api, admin
from app.services import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    jobs.start_scheduler()
    yield
    jobs.stop_scheduler()


app = FastAPI(
    title=APP_NAME,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(pages.router)
app.include_router(api.router, prefix="/api", tags=["api"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
