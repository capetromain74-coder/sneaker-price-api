"""
Flask app — API de prix sneakers multi-sources.

Endpoints :
  GET /api/prices/<sku>           → prix par taille
  GET /api/prices/<sku>?size=9.5  → prix pour une taille spécifique
  GET /api/search/<sku>           → recherche produit
  GET /health                     → health check

Déploiement Render :
  Build Command  : pip install -r requirements.txt
  Start Command  : gunicorn example_flask:app --bind 0.0.0.0:$PORT --timeout 120
"""

import os
import asyncio
import time
from functools import wraps
from flask import Flask, jsonify, request

from sneaker_prices import SneakerPriceFetcher

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────
KICKSDB_API_KEY = os.environ.get("KICKSDB_API_KEY", "")

# ── Cache simple en mémoire ─────────────────────────────────────
_cache = {}
CACHE_TTL = 600  # 10 minutes


def cached(ttl=CACHE_TTL):
    """Décorateur de cache simple pour les endpoints."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            cache_key = f"{request.path}?{request.query_string.decode()}"
            now = time.time()
            if cache_key in _cache:
                entry = _cache[cache_key]
                if now - entry["ts"] < ttl:
                    return entry["data"]
            result = f(*args, **kwargs)
            _cache[cache_key] = {"data": result, "ts": now}
            # Limiter la taille du cache
            if len(_cache) > 500:
                oldest_key = min(_cache, key=lambda k: _cache[k]["ts"])
                del _cache[oldest_key]
            return result
        return wrapper
    return decorator


def run_async(coro):
    """Exécute une coroutine async dans Flask (synchrone)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Endpoints ───────────────────────────────────────────────────

@app.route("/api/prices/<sku>")
@cached(ttl=600)
def get_prices(sku: str):
    """
    GET /api/prices/DD1391-100
    GET /api/prices/DD1391-100?size=9.5
    
    Retourne les prix par taille depuis GOAT (+ KicksDB si configuré).
    """
    async def _fetch():
        fetcher = SneakerPriceFetcher(kicksdb_api_key=KICKSDB_API_KEY)
        try:
            return await fetcher.get_prices(sku)
        finally:
            await fetcher.close()
    
    try:
        result = run_async(_fetch())
    except Exception as e:
        return jsonify({"error": str(e), "sku": sku}), 500
    
    if not result:
        return jsonify({"error": "Product not found", "sku": sku}), 404
    
    data = result.to_dict()
    
    # Si une taille spécifique est demandée
    size = request.args.get("size")
    if size:
        size_prices = data.get("prices_by_size", {}).get(size, {})
        return jsonify({
            "sku": sku,
            "name": data.get("name"),
            "size": size,
            "prices": size_prices if size_prices else None,
            "source": data.get("source"),
            "available": bool(size_prices),
        })
    
    return jsonify(data)


@app.route("/api/search/<sku>")
@cached(ttl=3600)
def search_product(sku: str):
    """
    GET /api/search/DD1391-100
    
    Recherche un produit par SKU (métadonnées, pas de prix détaillés).
    """
    async def _search():
        from sneaker_prices import GoatScraper
        scraper = GoatScraper()
        try:
            slug = await scraper.search_by_sku(sku)
            if slug:
                return {"found": True, "slug": slug, "sku": sku,
                        "goat_url": f"https://www.goat.com/sneakers/{slug}"}
            return {"found": False, "sku": sku}
        finally:
            await scraper.close()
    
    try:
        result = run_async(_search())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "cache_size": len(_cache),
        "kicksdb_configured": bool(KICKSDB_API_KEY),
        "sources": ["goat", "kicksdb" if KICKSDB_API_KEY else None, "constructor_io"],
    })


@app.route("/")
def index():
    return jsonify({
        "service": "Sneaker Price API",
        "version": "2.0",
        "endpoints": {
            "/api/prices/<sku>": "Get prices by size for a SKU",
            "/api/prices/<sku>?size=9.5": "Get price for specific size",
            "/api/search/<sku>": "Search product by SKU",
            "/health": "Health check",
        },
        "example": "/api/prices/DD1391-100",
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
