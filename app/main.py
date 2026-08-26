import logging

from fastapi import FastAPI

from app.data import models  # noqa: F401 - ensures models are registered before create_all
from app.db import Base, engine, sync_missing_columns
from app.portal.routes import router as portal_router
from app.webhooks.razorpay_webhook import router as razorpay_router
from app.webhooks.whatsapp_webhook import router as whatsapp_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Revenue Recovery")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    sync_missing_columns()
    from app.playbooks.loader import get_registry

    registry = get_registry()
    logging.getLogger("recovery.main").info("loaded %d playbooks: %s", len(registry), list(registry.keys()))


app.include_router(portal_router)
app.include_router(razorpay_router)
app.include_router(whatsapp_router)
