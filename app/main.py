from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .db import SessionLocal, engine
from .errors import AppError
from .routes import api, pages
from .services import warehouses
from .templating import templates

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="CheckStock",
    description="Учёт остатков на складах фулфилмента: ФФ, FBS, FBO, перемещения.",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(pages.router)
app.include_router(api.router)


@app.get("/health", tags=["service"])
def health() -> dict[str, str]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        {
            "error": "VALIDATION_ERROR",
            "message": "Некорректные входные данные.",
            "details": {"errors": jsonable_encoder(exc.errors())},
        },
        status_code=422,
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    if request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    ):
        return JSONResponse(exc.as_dict(), status_code=exc.status_code)

    session = SessionLocal()
    try:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "error": exc,
                "action": "/",
                "lines": [],
                "warehouse_id": None,
                "from_warehouse_id": None,
                "to_warehouse_id": None,
                "external_ref": None,
                "idempotency_key": None,
                "active_warehouses": warehouses.list_warehouses(session, include_archived=False),
                "products": [],
                "place_labels": {},
            },
            status_code=exc.status_code,
        )
    finally:
        session.close()
