"""Store layer: the demo product catalog (seeded into the DB) and canned sample deals.

Products are demo items. Sample deals are deterministic fictional offers used when no Channel3 key is
configured (or Channel3 is unreachable); the API flags them with `sample: true`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.schemas import usd

D = Decimal


@dataclass(frozen=True)
class CatalogItem:
    id: str
    name: str
    description: str
    price: Decimal
    category: str
    emoji: str


CATALOG: tuple[CatalogItem, ...] = (
    CatalogItem("hp-aurora", "Aurora Wireless Headphones", "Over-ear, 30-hour battery, noise reduction.", D("89.99"), "Audio", "🎧"),
    CatalogItem("sh-stride", "Stride Running Shoes", "Lightweight cushioned trainers for daily runs.", D("74.50"), "Footwear", "👟"),
    CatalogItem("bp-trail", "Trailpack 25L Backpack", "Water-resistant daypack with a laptop sleeve.", D("49.00"), "Bags", "🎒"),
    CatalogItem("cm-brew", "BrewMate Coffee Maker", "12-cup programmable drip coffee maker.", D("59.99"), "Kitchen", "☕"),
    CatalogItem("sw-pulse", "Pulse Fitness Watch", "Heart-rate and sleep tracking with a 7-day battery.", D("129.00"), "Wearables", "⌚"),
    CatalogItem("lm-glow", "Glow LED Desk Lamp", "Dimmable lamp with a USB charging port.", D("27.90"), "Home", "💡"),
    CatalogItem("ym-flow", "Flow Yoga Mat", "6mm non-slip mat with a carry strap.", D("32.00"), "Fitness", "🧘"),
    CatalogItem("wb-hydro", "Hydro Steel Water Bottle", "Insulated 750ml bottle that keeps drinks cold for 24 hours.", D("21.50"), "Outdoors", "🧴"),
    CatalogItem("kb-click", "Click Mechanical Keyboard", "Hot-swappable switches in a compact layout.", D("68.00"), "Computing", "⌨️"),
    CatalogItem("sk-draw", "Draftline Sketchbook Set", "A5 sketchbooks, 120gsm paper, set of three.", D("18.75"), "Art", "✏️"),
)
_BY_ID = {c.id: c for c in CATALOG}
SAMPLE_MERCHANTS = ("BrightMart", "ValueHub", "Shoply", "GadgetGarden")      # fictional


def ensure_products(session: Session) -> int:
    """Insert any missing catalog products (idempotent). Returns how many were added."""
    have = set(session.scalars(select(Product.id)))
    added = 0
    for c in CATALOG:
        if c.id not in have:
            session.add(Product(id=c.id, name=c.name, description=c.description, price=c.price, category=c.category,
                                image_url=f"/api/products/{c.id}/image"))
            added += 1
    session.flush()
    return added


def product_svg(product_id: str, category: str = "") -> str:
    """A self-contained product picture (emoji on a gradient) so the demo needs no external images."""
    emoji = _BY_ID[product_id].emoji if product_id in _BY_ID else "🛍️"
    hue = int.from_bytes(hashlib.sha256(product_id.encode()).digest()[:2], "big") % 40 + 15     # warm hues
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 160"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="hsl({hue},85%,92%)"/><stop offset="1" stop-color="hsl({hue + 12},80%,82%)"/></linearGradient></defs>'
            f'<rect width="240" height="160" fill="url(#g)"/><text x="120" y="102" font-size="72" text-anchor="middle">{emoji}</text></svg>')


def sample_offers(product_id: str, name: str, store_price: Decimal) -> list[dict]:
    """Three canned offers around the store price (deterministic per product)."""
    rows = []
    for merchant in SAMPLE_MERCHANTS:
        h = int.from_bytes(hashlib.sha256(f"{product_id}:{merchant}".encode()).digest()[:4], "big")
        factor = D("0.86") + D(h % 21) / D(100)                       # 0.86x .. 1.06x
        rows.append({"title": name, "merchant": merchant, "price": usd(store_price * factor), "link": None})
    return sorted(rows, key=lambda r: (r["price"], r["merchant"]))[:3]
