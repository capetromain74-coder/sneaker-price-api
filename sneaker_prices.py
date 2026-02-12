"""
Sneaker Price Fetcher — Solution avec curl_cffi pour contourner les protections anti-bot.

Utilise curl_cffi avec impersonate="chrome" pour imiter parfaitement le TLS fingerprint
de Chrome, ce qui permet de passer les protections de GOAT/StockX depuis un serveur.
"""

import json
import re
import logging
from typing import Optional
from dataclasses import dataclass, field, asdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SneakerPriceResult:
    name: str
    sku: str
    brand: str = ""
    colorway: str = ""
    retail_price: Optional[float] = None
    image_url: str = ""
    source: str = "goat"
    slug: str = ""
    prices_by_size: dict = field(default_factory=dict)
    lowest_ask: Optional[float] = None
    
    def to_dict(self):
        return asdict(self)


class GoatScraper:
    """
    Scrape GOAT avec curl_cffi pour contourner les protections anti-bot.
    Utilise l'impersonation Chrome pour le TLS fingerprint.
    """
    
    ALGOLIA_URL = "https://2fwotdvm2o-dsn.algolia.net/1/indexes/*/queries"
    ALGOLIA_APP_ID = "2FWOTDVM2O"
    ALGOLIA_API_KEY = "ac96de6fef0e02bb95d433d8d5c7038a"
    PRODUCT_API_URL = "https://www.goat.com/web-api/v1/product_templates"
    
    def __init__(self):
        self._session = None
        try:
            from curl_cffi.requests import Session
            self._session = Session(impersonate="chrome")
            logger.info("[GOAT] Using curl_cffi with Chrome TLS impersonation")
        except ImportError:
            logger.warning("[GOAT] curl_cffi not available!")
    
    def _get(self, url: str, headers: dict = None) -> Optional[str]:
        """GET request with Chrome TLS fingerprint."""
        if not self._session:
            logger.error("[GOAT] No session available")
            return None
        
        try:
            resp = self._session.get(url, headers=headers, timeout=30)
            logger.info(f"[GOAT] GET {url[:60]}... -> {resp.status_code}")
            if resp.status_code == 200:
                return resp.text
            return None
        except Exception as e:
            logger.error(f"[GOAT] GET error: {e}")
            return None
    
    def _post(self, url: str, data: dict, headers: dict = None) -> Optional[str]:
        """POST request with Chrome TLS fingerprint."""
        if not self._session:
            logger.error("[GOAT] No session available")
            return None
        
        try:
            resp = self._session.post(url, json=data, headers=headers, timeout=30)
            logger.info(f"[GOAT] POST {url[:60]}... -> {resp.status_code}")
            if resp.status_code == 200:
                return resp.text
            return None
        except Exception as e:
            logger.error(f"[GOAT] POST error: {e}")
            return None
    
    def search_by_sku(self, sku: str) -> Optional[dict]:
        """Search product by SKU using Algolia."""
        logger.info(f"[GOAT] Searching for SKU: {sku}")
        
        headers = {
            "x-algolia-application-id": self.ALGOLIA_APP_ID,
            "x-algolia-api-key": self.ALGOLIA_API_KEY,
            "Content-Type": "application/json",
        }
        
        payload = {
            "requests": [{
                "indexName": "product_variants_v2",
                "query": sku,
                "params": "hitsPerPage=10"
            }]
        }
        
        response = self._post(self.ALGOLIA_URL, payload, headers)
        if not response:
            return None
        
        try:
            data = json.loads(response)
            hits = data.get("results", [{}])[0].get("hits", [])
            
            if not hits:
                logger.warning(f"[GOAT] No results for SKU: {sku}")
                return None
            
            # Find exact SKU match
            for hit in hits:
                hit_sku = hit.get("sku", "").upper().replace(" ", "-")
                if hit_sku == sku.upper().replace(" ", "-"):
                    logger.info(f"[GOAT] Found exact match: {hit.get('name')}")
                    return hit
            
            # Return first result if no exact match
            logger.info(f"[GOAT] Using first result: {hits[0].get('name')}")
            return hits[0]
            
        except Exception as e:
            logger.error(f"[GOAT] Parse error: {e}")
            return None
    
    def get_product_details(self, slug: str) -> Optional[dict]:
        """Get full product details including prices by size."""
        url = f"{self.PRODUCT_API_URL}/{slug}"
        
        response = self._get(url)
        if not response:
            return None
        
        try:
            return json.loads(response)
        except Exception as e:
            logger.error(f"[GOAT] Parse error: {e}")
            return None
    
    def get_prices(self, sku: str) -> Optional[SneakerPriceResult]:
        """Get prices by size for a SKU."""
        
        # Step 1: Search for product
        product = self.search_by_sku(sku)
        if not product:
            return None
        
        slug = product.get("slug", "")
        if not slug:
            logger.warning("[GOAT] No slug found")
            return None
        
        # Step 2: Get detailed product info
        details = self.get_product_details(slug)
        
        prices_by_size = {}
        lowest_ask = None
        
        if details:
            # Extract prices from size_options
            size_options = details.get("sizeOptions", [])
            
            for size_opt in size_options:
                size = str(size_opt.get("value", ""))
                
                # Get lowest price for this size
                lowest_price_cents = size_opt.get("lowestPriceCents", {})
                
                # Try different price keys
                price_cents = (
                    lowest_price_cents.get("amount") or
                    lowest_price_cents.get("amountUsdCents") or
                    size_opt.get("lowestPriceCents") or
                    0
                )
                
                if isinstance(price_cents, dict):
                    price_cents = price_cents.get("amount", 0)
                
                if price_cents and price_cents > 0:
                    price_usd = price_cents / 100
                    prices_by_size[size] = {"goat": price_usd}
                    
                    if lowest_ask is None or price_usd < lowest_ask:
                        lowest_ask = price_usd
            
            logger.info(f"[GOAT] Found {len(prices_by_size)} sizes with prices")
        
        # If no detailed prices, try from Algolia data
        if not prices_by_size:
            lowest_price = product.get("lowest_price_cents", 0)
            if lowest_price:
                lowest_ask = lowest_price / 100
                logger.info(f"[GOAT] Only global price available: ${lowest_ask}")
        
        return SneakerPriceResult(
            name=product.get("name", "") or details.get("name", "") if details else product.get("name", ""),
            sku=product.get("sku", sku).replace(" ", "-"),
            brand=product.get("brand_name", "") or (details.get("brandName", "") if details else ""),
            colorway=product.get("details", "") or (details.get("details", "") if details else ""),
            retail_price=(product.get("retail_price_cents", 0) or 0) / 100 or None,
            image_url=product.get("main_glow_picture_url", "") or product.get("main_picture_url", ""),
            source="goat",
            slug=slug,
            prices_by_size=prices_by_size,
            lowest_ask=lowest_ask,
        )
    
    def close(self):
        if self._session:
            self._session.close()


class ConstructorIOClient:
    """Fallback: Constructor.io (GOAT search API)."""
    
    SEARCH_URL = "https://goat.cnstrc.com/search/{query}"
    KEY = "key_XT7bjdbvjgECO5d8"
    
    def __init__(self):
        self._session = None
        try:
            from curl_cffi.requests import Session
            self._session = Session(impersonate="chrome")
        except ImportError:
            pass
    
    def get_prices(self, sku: str) -> Optional[SneakerPriceResult]:
        """Search by SKU, returns global lowest price only."""
        if not self._session:
            return None
        
        logger.info(f"[Constructor.io] Searching for SKU: {sku}")
        
        try:
            url = self.SEARCH_URL.format(query=sku.replace("-", " "))
            resp = self._session.get(url, params={
                "key": self.KEY,
                "page": "1",
            }, timeout=15)
            
            if resp.status_code != 200:
                return None
            
            data = resp.json()
            results = data.get("response", {}).get("results", [])
            
            if not results:
                return None
            
            # Find matching product
            product = None
            for r in results:
                rd = r.get("data", {})
                if rd.get("sku", "").replace(" ", "-").upper() == sku.upper():
                    product = rd
                    break
            
            if not product:
                product = results[0].get("data", {})
            
            lowest_cents = product.get("lowest_price_cents", 0)
            
            return SneakerPriceResult(
                name=product.get("name", ""),
                sku=product.get("sku", "").replace(" ", "-"),
                brand=product.get("brand_name", ""),
                colorway=product.get("details", ""),
                retail_price=(product.get("retail_price_cents", 0) or 0) / 100 or None,
                image_url=product.get("image_url", ""),
                source="goat_search",
                slug=product.get("slug", ""),
                prices_by_size={},
                lowest_ask=lowest_cents / 100 if lowest_cents else None,
            )
            
        except Exception as e:
            logger.error(f"[Constructor.io] Error: {e}")
            return None
    
    def close(self):
        if self._session:
            self._session.close()


class SneakerPriceFetcher:
    """
    Orchestrateur qui essaie les sources dans l'ordre :
    1. GOAT via Algolia + web-api (avec curl_cffi)
    2. Constructor.io (fallback, prix global uniquement)
    """
    
    def __init__(self, kicksdb_api_key: str = ""):
        self.goat = GoatScraper()
        self.constructor = ConstructorIOClient()
    
    async def get_prices(self, sku: str) -> Optional[SneakerPriceResult]:
        """
        Récupère les prix par taille pour un SKU donné.
        Note: Cette méthode est async pour compatibilité mais utilise des appels sync.
        """
        sku = sku.strip().upper()
        
        # Source 1: GOAT avec curl_cffi
        result = self.goat.get_prices(sku)
        if result and (result.prices_by_size or result.lowest_ask):
            logger.info(f"✅ Got data from GOAT: {len(result.prices_by_size)} sizes")
            return result
        
        # Source 2: Constructor.io (fallback)
        fallback = self.constructor.get_prices(sku)
        if fallback:
            logger.info(f"⚠️ Fallback to Constructor.io: ${fallback.lowest_ask}")
            if result:
                result.lowest_ask = result.lowest_ask or fallback.lowest_ask
                return result
            return fallback
        
        logger.warning(f"❌ No prices found for SKU: {sku}")
        return None
    
    async def close(self):
        self.goat.close()
        self.constructor.close()


# Test standalone
if __name__ == "__main__":
    import asyncio
    
    async def main():
        fetcher = SneakerPriceFetcher()
        
        test_skus = ["DD1391-100", "CW2288-111", "DZ5485-612"]
        
        for sku in test_skus:
            print(f"\n{'='*60}")
            print(f"SKU: {sku}")
            print("=" * 60)
            
            result = await fetcher.get_prices(sku)
            
            if result:
                print(f"Name    : {result.name}")
                print(f"Brand   : {result.brand}")
                print(f"Source  : {result.source}")
                print(f"Lowest  : ${result.lowest_ask}")
                print(f"Sizes   : {len(result.prices_by_size)}")
                
                if result.prices_by_size:
                    print("\nPrices by size:")
                    for size in sorted(result.prices_by_size.keys(), key=lambda x: float(x) if x.replace(".", "").isdigit() else 0):
                        prices = result.prices_by_size[size]
                        print(f"  US {size:>5s} → ${prices.get('goat', 'N/A')}")
            else:
                print("❌ No prices found")
        
        await fetcher.close()
    
    asyncio.run(main())
