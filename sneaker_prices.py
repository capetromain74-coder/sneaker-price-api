"""
Sneaker Price Fetcher — Solution multi-sources pour récupérer les prix par taille.

Stratégie (par ordre de fiabilité, février 2025+) :
  1. GOAT — scraping via __NEXT_DATA__ (fonctionne depuis un serveur, pas de PerimeterX)
  2. KicksDB API — plan gratuit 50k req/mois (données StockX + GOAT agrégées)
  3. Constructor.io (API de recherche GOAT) — search par SKU

Pourquoi pas StockX directement ?
  - PerimeterX + Cloudflare + TLS fingerprinting
  - IP datacenters (Render/AWS) bloquées immédiatement
  - Même SneaksAPI est cassé depuis 2024 pour les prix

Pourquoi GOAT fonctionne ?
  - Protection moins agressive que StockX
  - __NEXT_DATA__ contient les données produit en JSON dans le HTML
  - Les prix GOAT sont très proches des prix StockX pour la plupart des sneakers

Installation :
  pip install httpx parsel --break-system-packages

Usage :
  from sneaker_prices import SneakerPriceFetcher
  
  fetcher = SneakerPriceFetcher()
  
  # Par SKU
  result = await fetcher.get_prices("DD1391-100")
  print(result)
  
  # Avec Flask (voir example_flask.py)
"""

import asyncio
import json
import re
import logging
from typing import Optional
from dataclasses import dataclass, field, asdict

import httpx
from parsel import Selector

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════

@dataclass
class SizePrice:
    size: str
    price_cents: int  # prix en centimes
    price: float  # prix en dollars
    currency: str = "USD"
    source: str = "goat"

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
    prices_by_size: dict = field(default_factory=dict)  # {"9.5": {"goat": 120, "stockx": 125}, ...}
    lowest_ask: Optional[float] = None
    
    def to_dict(self):
        return asdict(self)


# ═══════════════════════════════════════════════════════════════
# Source 1 : GOAT — Scraping __NEXT_DATA__
# ═══════════════════════════════════════════════════════════════

class GoatScraper:
    """
    Scrape les prix par taille depuis GOAT.com via les données JSON cachées.
    
    GOAT utilise NextJS → les données produit sont dans <script id="__NEXT_DATA__">
    C'est la méthode la plus fiable depuis un serveur car GOAT a une protection
    anti-bot moins agressive que StockX.
    """
    
    BASE_URL = "https://www.goat.com"
    
    def __init__(self):
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            http2=True,  # HTTP/2 aide à passer les protections basiques
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
    
    async def search_by_sku(self, sku: str) -> Optional[str]:
        """
        Recherche un produit par SKU sur GOAT et retourne le slug.
        Utilise l'API de recherche Constructor.io (utilisée par GOAT en interne).
        """
        # L'API Constructor.io de GOAT est publiquement accessible
        search_url = "https://goat.cnstrc.com/search/{query}".format(
            query=sku.replace("-", " ")
        )
        params = {
            "key": "key_XT7bjdbvjgECO5d8",  # Clé publique Constructor.io de GOAT
            "page": "1",
            "fmt_options[hidden_fields]": "gp_lowest_price_cents_2",
            "fmt_options[hidden_facets]": "gp_lowest_price_cents_2",
            "features[display_variations]": "true",
            "feature_variants[display_variations]": "matched",
        }
        
        try:
            resp = await self.client.get(search_url, params=params)
            if resp.status_code != 200:
                logger.warning(f"GOAT search failed: HTTP {resp.status_code}")
                return None
            
            data = resp.json()
            results = data.get("response", {}).get("results", [])
            
            if not results:
                return None
            
            # Trouver le produit exact par SKU
            for result in results:
                result_data = result.get("data", {})
                result_sku = result_data.get("sku", "").replace(" ", "-").upper()
                if result_sku == sku.upper().replace(" ", "-"):
                    return result_data.get("slug")
            
            # Sinon prendre le premier résultat
            return results[0].get("data", {}).get("slug")
            
        except Exception as e:
            logger.error(f"GOAT search error: {e}")
            return None
    
    async def scrape_product_page(self, slug: str) -> Optional[SneakerPriceResult]:
        """
        Scrape la page produit GOAT et extrait les données JSON cachées.
        """
        url = f"{self.BASE_URL}/sneakers/{slug}"
        
        try:
            resp = await self.client.get(url)
            if resp.status_code != 200:
                logger.warning(f"GOAT page failed: HTTP {resp.status_code} for {slug}")
                return None
            
            html = resp.text
            
            # Extraire __NEXT_DATA__
            sel = Selector(text=html)
            next_data_raw = sel.css("script#__NEXT_DATA__::text").get()
            
            if not next_data_raw:
                logger.warning("No __NEXT_DATA__ found on page")
                return None
            
            next_data = json.loads(next_data_raw)
            
            # Naviguer dans la structure NextJS
            # La structure varie mais le produit est généralement dans props.pageProps
            page_props = next_data.get("props", {}).get("pageProps", {})
            product = self._find_product_data(page_props)
            
            if not product:
                logger.warning("Product data not found in __NEXT_DATA__")
                return None
            
            return self._parse_product(product, slug)
            
        except Exception as e:
            logger.error(f"GOAT scrape error: {e}")
            return None
    
    def _find_product_data(self, data: dict) -> Optional[dict]:
        """
        Recherche récursive des données produit dans la structure NextJS.
        GOAT change parfois la structure, cette méthode est résiliente.
        """
        # Chemins connus
        for key_path in [
            ["product"],
            ["productTemplate"],
            ["data", "product"],
            ["initialState", "product"],
        ]:
            obj = data
            for key in key_path:
                if isinstance(obj, dict):
                    obj = obj.get(key)
                else:
                    obj = None
                    break
            if obj and isinstance(obj, dict) and ("name" in obj or "title" in obj):
                return obj
        
        # Recherche récursive — chercher un objet avec "sku" et "sizeRange"
        return self._deep_find(data, lambda v: (
            isinstance(v, dict) and 
            "sku" in v and 
            ("sizeRange" in v or "variants" in v or "name" in v)
        ))
    
    def _deep_find(self, data, predicate, depth=0):
        """Recherche récursive dans un objet JSON."""
        if depth > 8:
            return None
        if predicate(data):
            return data
        if isinstance(data, dict):
            for v in data.values():
                result = self._deep_find(v, predicate, depth + 1)
                if result:
                    return result
        elif isinstance(data, list):
            for item in data[:20]:  # Limiter la profondeur
                result = self._deep_find(item, predicate, depth + 1)
                if result:
                    return result
        return None
    
    def _parse_product(self, product: dict, slug: str) -> SneakerPriceResult:
        """Parse les données produit GOAT en SneakerPriceResult."""
        prices_by_size = {}
        
        # Les variants contiennent les prix par taille
        variants = product.get("variants", [])
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    size = str(variant.get("size", variant.get("sizeOption", {}).get("value", "")))
                    # Prix en centimes
                    price_cents = (
                        variant.get("lowestPriceCents")
                        or variant.get("lowest_price_cents")
                        or variant.get("lowestPriceCentsNew")  
                        or variant.get("shoeCondition", {}).get("lowestPriceCents", {}).get("amount")
                    )
                    if size and price_cents and isinstance(price_cents, (int, float)):
                        prices_by_size[size] = {
                            "goat": round(price_cents / 100, 2)
                        }
        
        # Si pas de variants, chercher dans sizeRange + un prix global
        if not prices_by_size:
            size_range = product.get("sizeRange", [])
            lowest = product.get("lowestPriceCents", 0)
            if size_range and lowest:
                for size in size_range:
                    prices_by_size[str(size)] = {"goat": round(lowest / 100, 2)}
        
        # Image
        image = ""
        media = product.get("media", product.get("mainPictureUrl", ""))
        if isinstance(media, dict):
            image = media.get("imageUrl", media.get("mainPictureUrl", ""))
        elif isinstance(media, str):
            image = media
        if not image:
            image = product.get("mainPictureUrl", product.get("image_url", ""))
        
        # Lowest ask global
        lowest_ask = None
        if prices_by_size:
            all_prices = [
                p.get("goat", 0) for p in prices_by_size.values() if p.get("goat")
            ]
            if all_prices:
                lowest_ask = min(all_prices)
        
        return SneakerPriceResult(
            name=product.get("name", product.get("title", "")),
            sku=product.get("sku", "").replace(" ", "-"),
            brand=product.get("brandName", product.get("brand", "")),
            colorway=product.get("details", product.get("colorway", "")),
            retail_price=self._parse_retail(product),
            image_url=image,
            source="goat",
            slug=slug,
            prices_by_size=prices_by_size,
            lowest_ask=lowest_ask,
        )
    
    def _parse_retail(self, product: dict) -> Optional[float]:
        retail = product.get("retailPriceCents", product.get("retail_price_cents", 0))
        if retail:
            return retail / 100
        retail = product.get("retailPrice", product.get("retail_price"))
        if retail:
            return float(retail)
        return None
    
    async def get_prices(self, sku: str) -> Optional[SneakerPriceResult]:
        """Pipeline complet : recherche par SKU → scrape page → prix par taille."""
        logger.info(f"[GOAT] Searching for SKU: {sku}")
        
        slug = await self.search_by_sku(sku)
        if not slug:
            logger.warning(f"[GOAT] Product not found for SKU: {sku}")
            return None
        
        logger.info(f"[GOAT] Found slug: {slug}")
        result = await self.scrape_product_page(slug)
        
        if result:
            logger.info(
                f"[GOAT] Got prices for {result.name}: "
                f"{len(result.prices_by_size)} sizes"
            )
        
        return result
    
    async def close(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════
# Source 2 : KicksDB (anciennement SneakersAPI) — Plan gratuit
# ═══════════════════════════════════════════════════════════════

class KicksDBClient:
    """
    Client pour KicksDB API (kicks.dev).
    
    Plan gratuit : 50,000 requêtes/mois
    Données : StockX + GOAT + Kicks Crew agrégées
    
    Inscription : https://kicks.dev → créer un compte → récupérer l'API key
    """
    
    BASE_URL = "https://api.kicks.dev/v2"
    
    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            timeout=20,
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
            },
        )
    
    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)
    
    async def search(self, sku: str) -> Optional[dict]:
        """Recherche un produit par SKU."""
        if not self.is_configured:
            return None
        
        try:
            resp = await self.client.get(
                f"{self.BASE_URL}/search",
                params={"query": sku, "limit": 3},
            )
            if resp.status_code != 200:
                logger.warning(f"[KicksDB] Search failed: HTTP {resp.status_code}")
                return None
            
            data = resp.json()
            products = data.get("data", [])
            
            # Trouver le produit exact par SKU
            for p in products:
                if p.get("sku", "").replace(" ", "-").upper() == sku.upper():
                    return p
            
            return products[0] if products else None
            
        except Exception as e:
            logger.error(f"[KicksDB] Search error: {e}")
            return None
    
    async def get_prices(self, sku: str) -> Optional[SneakerPriceResult]:
        """Récupère les prix par taille via KicksDB."""
        if not self.is_configured:
            return None
        
        logger.info(f"[KicksDB] Fetching prices for SKU: {sku}")
        
        product = await self.search(sku)
        if not product:
            return None
        
        product_id = product.get("id")
        if not product_id:
            return None
        
        # Récupérer les prix détaillés
        try:
            resp = await self.client.get(
                f"{self.BASE_URL}/products/{product_id}/prices",
            )
            if resp.status_code != 200:
                # Fallback : utiliser les données de recherche
                return SneakerPriceResult(
                    name=product.get("title", ""),
                    sku=product.get("sku", ""),
                    brand=product.get("brand", ""),
                    source="kicksdb",
                    lowest_ask=product.get("min_price"),
                    prices_by_size={},
                )
            
            price_data = resp.json().get("data", [])
            prices_by_size = {}
            
            for entry in price_data:
                size = str(entry.get("size", ""))
                if size:
                    prices_by_size[size] = {}
                    if entry.get("stockx_price"):
                        prices_by_size[size]["stockx"] = entry["stockx_price"]
                    if entry.get("goat_price"):
                        prices_by_size[size]["goat"] = entry["goat_price"]
            
            return SneakerPriceResult(
                name=product.get("title", ""),
                sku=product.get("sku", ""),
                brand=product.get("brand", ""),
                image_url=product.get("image", ""),
                source="kicksdb",
                prices_by_size=prices_by_size,
                lowest_ask=product.get("min_price"),
            )
            
        except Exception as e:
            logger.error(f"[KicksDB] Price fetch error: {e}")
            return None
    
    async def close(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════
# Source 3 : Constructor.io (GOAT search API) — Prix basiques
# ═══════════════════════════════════════════════════════════════

class ConstructorIOClient:
    """
    Fallback : utilise l'API de recherche Constructor.io de GOAT.
    Donne le prix le plus bas global mais PAS les prix par taille.
    Utile comme dernier recours.
    """
    
    SEARCH_URL = "https://goat.cnstrc.com/search/{query}"
    KEY = "key_XT7bjdbvjgECO5d8"
    
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=15,
            headers={
                "User-Agent": "GOAT/2.0 CFNetwork/1410.0.3 Darwin/22.6.0",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    
    async def get_prices(self, sku: str) -> Optional[SneakerPriceResult]:
        """Recherche par SKU, retourne le prix global le plus bas."""
        logger.info(f"[Constructor.io] Searching for SKU: {sku}")
        
        try:
            url = self.SEARCH_URL.format(query=sku.replace("-", " "))
            resp = await self.client.get(url, params={
                "key": self.KEY,
                "page": "1",
                "fmt_options[hidden_fields]": "gp_lowest_price_cents_2",
                "features[display_variations]": "true",
                "feature_variants[display_variations]": "matched",
                "variations_map": json.dumps({
                    "dtype": "object",
                    "group_by": [
                        {"name": "product_condition", "field": "data.product_condition"},
                        {"name": "box_condition", "field": "data.box_condition"},
                    ],
                    "values": {
                        "min_regional_price": {
                            "field": "data.gp_lowest_price_cents_2",
                            "aggregation": "min",
                        },
                    },
                }),
            })
            
            if resp.status_code != 200:
                return None
            
            data = resp.json()
            results = data.get("response", {}).get("results", [])
            
            if not results:
                return None
            
            # Trouver le bon produit
            product = None
            for r in results:
                rd = r.get("data", {})
                if rd.get("sku", "").replace(" ", "-").upper() == sku.upper():
                    product = rd
                    break
            
            if not product:
                product = results[0].get("data", {})
            
            # Extraire les prix par variation (condition neuve)
            variations = r.get("variations", []) if r else []
            prices_by_size = {}
            
            # Les variations de Constructor.io ne donnent pas les tailles individuelles
            # mais on peut au moins avoir le prix global
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
                prices_by_size=prices_by_size,  # Vide — pas dispo via search
                lowest_ask=lowest_cents / 100 if lowest_cents else None,
            )
            
        except Exception as e:
            logger.error(f"[Constructor.io] Error: {e}")
            return None
    
    async def close(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════
# Orchestrateur multi-sources
# ═══════════════════════════════════════════════════════════════

class SneakerPriceFetcher:
    """
    Orchestrateur qui essaie les sources dans l'ordre :
    1. GOAT scraping (prix par taille) — GRATUIT
    2. KicksDB API (prix par taille) — GRATUIT (50k req/mois, nécessite API key)
    3. Constructor.io (prix global uniquement) — GRATUIT, fallback
    """
    
    def __init__(self, kicksdb_api_key: str = ""):
        self.goat = GoatScraper()
        self.kicksdb = KicksDBClient(api_key=kicksdb_api_key)
        self.constructor = ConstructorIOClient()
    
    async def get_prices(self, sku: str) -> Optional[SneakerPriceResult]:
        """
        Récupère les prix par taille pour un SKU donné.
        Essaie les sources dans l'ordre jusqu'à obtenir des prix par taille.
        """
        sku = sku.strip().upper()
        
        # Source 1 : GOAT (meilleure source gratuite pour prix par taille)
        result = await self.goat.get_prices(sku)
        if result and result.prices_by_size:
            logger.info(f"✅ Got {len(result.prices_by_size)} sizes from GOAT")
            return result
        
        # Source 2 : KicksDB (si configuré)
        if self.kicksdb.is_configured:
            result = await self.kicksdb.get_prices(sku)
            if result and result.prices_by_size:
                logger.info(f"✅ Got {len(result.prices_by_size)} sizes from KicksDB")
                return result
        
        # Source 3 : Constructor.io (fallback — prix global uniquement)
        fallback = await self.constructor.get_prices(sku)
        if fallback:
            logger.info(f"⚠️ Only global price from Constructor.io: ${fallback.lowest_ask}")
            # Si on avait un résultat partiel de GOAT, enrichir
            if result:
                result.lowest_ask = result.lowest_ask or fallback.lowest_ask
                return result
            return fallback
        
        logger.warning(f"❌ No prices found for SKU: {sku}")
        return None
    
    async def close(self):
        await self.goat.close()
        await self.kicksdb.close()
        await self.constructor.close()


# ═══════════════════════════════════════════════════════════════
# Test standalone
# ═══════════════════════════════════════════════════════════════

async def main():
    fetcher = SneakerPriceFetcher(
        kicksdb_api_key=""  # Mettre ta clé KicksDB ici si tu en as une
    )
    
    test_skus = ["DD1391-100", "DZ5485-612", "FV5029-141"]
    
    for sku in test_skus:
        print(f"\n{'='*60}")
        print(f"SKU: {sku}")
        print("=" * 60)
        
        result = await fetcher.get_prices(sku)
        
        if result:
            print(f"Nom     : {result.name}")
            print(f"Brand   : {result.brand}")
            print(f"Source  : {result.source}")
            print(f"Retail  : ${result.retail_price}")
            print(f"Lowest  : ${result.lowest_ask}")
            print(f"Tailles : {len(result.prices_by_size)}")
            
            if result.prices_by_size:
                print("\nPrix par taille :")
                for size in sorted(
                    result.prices_by_size.keys(),
                    key=lambda x: float(x) if x.replace(".", "").isdigit() else 0,
                ):
                    prices = result.prices_by_size[size]
                    price_str = " | ".join(
                        f"{src}: ${p}" for src, p in prices.items()
                    )
                    print(f"  US {size:>5s} → {price_str}")
        else:
            print("❌ Aucun prix trouvé")
    
    await fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())
