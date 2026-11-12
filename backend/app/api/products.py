"""Store layer: GET /api/products, product pictures, and GET /api/deals (Channel3 with sample fallback)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import catalog
from app.api import Services, get_services, get_session
from app.channel3 import Channel3Error
from app.models import Product
from app.schemas import DealOfferOut, DealsOut, ProductOut, usd

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/products", response_model=list[ProductOut])
def products(q: str = Query("", max_length=80), session: Session = Depends(get_session)) -> list[Product]:
    rows = list(session.scalars(select(Product).order_by(Product.name)))
    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return rows
    return [p for p in rows if all(t in f"{p.name} {p.description} {p.category}".lower() for t in tokens)]


@router.get("/products/{product_id}/image")
def product_image(product_id: str, session: Session = Depends(get_session)) -> Response:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "unknown product")
    return Response(catalog.product_svg(product.id, product.category), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/deals", response_model=DealsOut)
def deals(product_id: str = Query(...), services: Services = Depends(get_services),
          session: Session = Depends(get_session)) -> DealsOut:
    """Top 3 offers for a product. Live Channel3 results when a key is set, otherwise (or on any failure) canned samples."""
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "unknown product")
    rows: list[dict] = []
    note: str | None = None
    client = services.channel3
    if client is not None and client.configured:
        try:
            rows = client.top_offers(product.name, limit=3)
        except Channel3Error as exc:
            logger.warning("Channel3 lookup failed, using sample deals: %s", exc)
            note = "Live deals are unavailable right now, so these are sample offers."
        if not rows and note is None:
            note = "Channel3 returned no priced offers, so these are sample offers."
    sample = not rows
    if sample:
        rows = catalog.sample_offers(product.id, product.name, product.price)
    return DealsOut(product_id=product.id, store_price=product.price, sample=sample, note=note,
                    offers=[DealOfferOut(title=r["title"], merchant=r["merchant"], price=r["price"],
                                         delta=usd(r["price"] - product.price), link=r.get("link")) for r in rows])
