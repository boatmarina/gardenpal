import base64
import os
from typing import Dict, List, Optional, Tuple

import requests


def extract_text_from_image(file_storage) -> Tuple[Optional[str], Optional[str]]:
    api_key = os.environ.get("OCR_SPACE_API_KEY", "").strip()
    if not api_key:
        return None, "OCR is not configured. Add OCR_SPACE_API_KEY."
    if file_storage is None or not file_storage.filename:
        return None, "Please attach a label photo first."

    file_storage.stream.seek(0)
    payload = {"apikey": api_key, "language": "eng", "isOverlayRequired": "false"}
    files = {"file": (file_storage.filename, file_storage.stream, file_storage.mimetype or "image/jpeg")}

    try:
        response = requests.post("https://api.ocr.space/parse/image", data=payload, files=files, timeout=20)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        return None, "OCR request failed. Please try again."

    if data.get("IsErroredOnProcessing"):
        return None, "OCR could not read that image."

    parsed = data.get("ParsedResults") or []
    text = " ".join((item.get("ParsedText", "") or "").strip() for item in parsed).strip()
    if not text:
        return None, "No readable label text found."
    return text, None


def identify_plant_from_image(file_storage) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    api_key = os.environ.get("PLANT_ID_API_KEY", "").strip()
    if not api_key:
        return None, "Photo identification is not configured. Add PLANT_ID_API_KEY."
    if file_storage is None or not file_storage.filename:
        return None, "Please attach a plant photo first."

    file_storage.stream.seek(0)
    encoded = base64.b64encode(file_storage.stream.read()).decode("ascii")
    file_storage.stream.seek(0)
    payload = {"images": [encoded], "similar_images": False}
    headers = {"Api-Key": api_key, "Content-Type": "application/json"}

    try:
        response = requests.post(
            "https://plant.id/api/v3/identification",
            json=payload,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        return None, "Plant photo lookup failed. Please try again."

    suggestions = data.get("result", {}).get("classification", {}).get("suggestions", [])
    if not suggestions:
        return None, "No plant suggestions found from that photo."

    top = suggestions[0]
    scientific = top.get("name", "")
    common_names = top.get("details", {}).get("common_names", [])
    common = common_names[0] if common_names else ""
    probability = top.get("probability")
    confidence = f"{round(probability * 100)}%" if isinstance(probability, (float, int)) else ""
    return {"scientific_name": scientific, "common_name": common, "confidence": confidence}, None


# ---------------------------------------------------------------------------
# iNaturalist helpers (free, no API key)
# ---------------------------------------------------------------------------

def _lookup_via_inat(query: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (scientific_name, common_name, photo_url) or (None, None, None)."""
    try:
        resp = requests.get(
            "https://api.inaturalist.org/v1/taxa",
            params={"q": query, "is_active": "true", "iconic_taxa": "Plantae", "per_page": 1},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None, None, None
        top = results[0]
        photo = top.get("default_photo") or {}
        photo_url = photo.get("medium_url") or photo.get("square_url")
        return top.get("name"), top.get("preferred_common_name"), photo_url
    except Exception:
        return None, None, None


def lookup_plant_image(query: str) -> Optional[str]:
    """Return a single iNaturalist photo URL for the plant, or None."""
    _, _, photo_url = _lookup_via_inat(query)
    return photo_url


def lookup_plant_photos(query: str, count: int = 3) -> List[str]:
    """Return up to `count` iNaturalist community photo URLs for the plant."""
    try:
        # Find taxon ID first
        taxa_resp = requests.get(
            "https://api.inaturalist.org/v1/taxa",
            params={"q": query, "is_active": "true", "iconic_taxa": "Plantae", "per_page": 1},
            timeout=12,
        )
        taxa_resp.raise_for_status()
        taxa = taxa_resp.json().get("results", [])
        if not taxa:
            return []

        taxon_id = taxa[0].get("id")
        if not taxon_id:
            return []

        # Fetch high-quality, varied observations with photos
        obs_resp = requests.get(
            "https://api.inaturalist.org/v1/observations",
            params={
                "taxon_id": taxon_id,
                "has[]": "photos",
                "quality_grade": "research",
                "per_page": count * 3,
                "order_by": "votes",
                "order": "desc",
            },
            timeout=12,
        )
        obs_resp.raise_for_status()
        observations = obs_resp.json().get("results", [])

        photos = []
        for obs in observations:
            obs_photos = obs.get("photos", [])
            if obs_photos:
                raw_url = obs_photos[0].get("url", "")
                if raw_url:
                    # iNaturalist photo URLs end in /square.jpg etc; swap to medium
                    medium_url = raw_url.replace("/square.", "/medium.")
                    photos.append(medium_url)
            if len(photos) >= count:
                break

        return photos
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Perenual lookup
# ---------------------------------------------------------------------------

def _lookup_via_perenual(query: str, api_key: str) -> Optional[Dict]:
    try:
        list_resp = requests.get(
            "https://perenual.com/api/species-list",
            params={"key": api_key, "q": query.strip()},
            timeout=20,
        )
        list_resp.raise_for_status()
        list_data = list_resp.json().get("data", [])
    except requests.RequestException:
        return None

    if not list_data:
        return None

    top = list_data[0]
    species_id = top.get("id")
    details = {}

    if species_id:
        try:
            det_resp = requests.get(
                f"https://perenual.com/api/species/details/{species_id}",
                params={"key": api_key},
                timeout=20,
            )
            det_resp.raise_for_status()
            details = det_resp.json()
        except requests.RequestException:
            details = {}

    sunlight = details.get("sunlight") or top.get("sunlight") or []
    cycle = details.get("cycle") or top.get("cycle") or ""
    watering = details.get("watering") or top.get("watering") or ""
    dimensions = details.get("dimension") or details.get("dimensions") or top.get("dimension") or ""
    spread = details.get("spread") or top.get("spread") or ""
    flowering = (
        details.get("flowering_season")
        or details.get("flowers")
        or top.get("flowering_season")
        or ""
    )

    common_name = top.get("common_name") or ""
    scientific_list = top.get("scientific_name") or []
    scientific_name = scientific_list[0] if scientific_list else (details.get("scientific_name") or "")
    sun_str = ", ".join(sunlight) if isinstance(sunlight, list) else str(sunlight)

    return {
        "name": common_name or query.strip(),
        "scientific_name": scientific_name,
        "sun_needs": sun_str,
        "watering_needs": watering,
        "flowering_schedule": str(flowering) if flowering else "",
        "lifecycle": cycle,
        "size_info": str(dimensions) if dimensions else "",
        "spreads": str(spread) if spread else "",
        "photo_url": None,
    }


# ---------------------------------------------------------------------------
# Supplementary sources: OpenFarm + Wikipedia
# ---------------------------------------------------------------------------

def _try_openfarm(result: dict) -> None:
    """Fill empty care fields from OpenFarm (good for edible/culinary plants)."""
    name = result.get("name") or ""
    if not name:
        return
    try:
        resp = requests.get(
            "https://openfarm.cc/api/v1/crops",
            params={"filter": name},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return
        attrs = data[0].get("attributes", {})
        if not result.get("sun_needs") and attrs.get("sun_requirements"):
            result["sun_needs"] = attrs["sun_requirements"]
        if not result.get("size_info"):
            height = attrs.get("height")
            spread = attrs.get("spread")
            if height:
                try:
                    size = f"{int(float(height))}cm tall"
                    if spread:
                        size += f", {int(float(spread))}cm spread"
                    result["size_info"] = size
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass


def _try_wikipedia(result: dict, query: str) -> None:
    """Extract lifecycle and sun needs from a Wikipedia intro paragraph."""
    if not query:
        return
    try:
        # Search for the best matching article
        search_resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 1},
            timeout=10,
        )
        search_resp.raise_for_status()
        hits = search_resp.json().get("query", {}).get("search", [])
        if not hits:
            return
        title = hits[0]["title"]

        summary_resp = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}",
            timeout=10,
        )
        if not summary_resp.ok:
            return

        extract = (summary_resp.json().get("extract") or "").lower()
        if not extract:
            return

        if not result.get("lifecycle"):
            if "perennial" in extract:
                result["lifecycle"] = "perennial"
            elif "biennial" in extract:
                result["lifecycle"] = "biennial"
            elif "annual" in extract:
                result["lifecycle"] = "annual"

        if not result.get("sun_needs"):
            if "full sun" in extract:
                if "partial shade" in extract or "part shade" in extract or "partial sun" in extract:
                    result["sun_needs"] = "part_shade"
                else:
                    result["sun_needs"] = "full_sun"
            elif "partial shade" in extract or "part shade" in extract:
                result["sun_needs"] = "part_shade"
            elif "shade" in extract and "sun" not in extract:
                result["sun_needs"] = "shade"
    except Exception:
        pass


def _supplement_missing_fields(result: dict, query: str) -> None:
    """Fill empty care/size fields using OpenFarm then Wikipedia."""
    _try_openfarm(result)
    if not result.get("sun_needs") or not result.get("lifecycle"):
        _try_wikipedia(result, result.get("name") or query)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def lookup_plant_details(query: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if not query.strip():
        return None, "Enter a plant name or scientific name first."

    api_key = os.environ.get("PERENUAL_API_KEY", "").strip()
    result = None
    if api_key:
        result = _lookup_via_perenual(query, api_key)

    if result is None:
        # No Perenual or no result — build from iNaturalist + OpenFarm + Wikipedia
        sci, common, photo_url = _lookup_via_inat(query)
        if not sci and not common:
            return None, "No plant information found for that name."
        result = {
            "name": common or query.strip(),
            "scientific_name": sci or "",
            "sun_needs": "",
            "watering_needs": "",
            "flowering_schedule": "",
            "lifecycle": "",
            "size_info": "",
            "spreads": "",
            "photo_url": photo_url,
        }
        _supplement_missing_fields(result, query)
    else:
        # Perenual found something — fill any gaps it left
        _supplement_missing_fields(result, query)

    # Ensure we have a photo (prefer common name for cultivar-heavy queries)
    if not result.get("photo_url"):
        inat_q = result.get("name") or result.get("scientific_name") or query
        _, _, photo_url = _lookup_via_inat(inat_q)
        result["photo_url"] = photo_url

    return result, None
