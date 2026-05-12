import base64
import os
from typing import Dict, Optional, Tuple

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
    payload = {
        "images": [encoded],
        "similar_images": False,
    }
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


def _lookup_via_inat(query: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """iNaturalist taxa search — returns (scientific_name, common_name, photo_url). No API key required."""
    try:
        resp = requests.get(
            "https://api.inaturalist.org/v1/taxa",
            params={
                "q": query,
                "is_active": "true",
                "iconic_taxa": "Plantae",
                "per_page": 1,
            },
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
    """Return an iNaturalist photo URL for the plant, or None. No API key required."""
    _, _, photo_url = _lookup_via_inat(query)
    return photo_url


def lookup_plant_details(query: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if not query.strip():
        return None, "Enter a plant name or scientific name first."

    api_key = os.environ.get("PERENUAL_API_KEY", "").strip()
    result = None
    if api_key:
        result = _lookup_via_perenual(query, api_key)

    if result is None:
        result, error = _lookup_via_fallback(query)
        if result is None:
            return None, error or "No plant information found for that name."

    # Enrich with iNaturalist photo if not already set
    if not result.get("photo_url"):
        search_q = result.get("scientific_name") or query
        _, _, photo_url = _lookup_via_inat(search_q)
        result["photo_url"] = photo_url

    return result, None


def _lookup_via_perenual(query: str, api_key: str) -> Optional[Dict[str, str]]:
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


def _lookup_via_fallback(query: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """iNaturalist + OpenFarm — used when Perenual is not configured or returns no results."""
    result = {
        "name": query.strip(),
        "scientific_name": "",
        "sun_needs": "",
        "watering_needs": "",
        "flowering_schedule": "",
        "lifecycle": "",
        "size_info": "",
        "spreads": "",
        "photo_url": None,
    }
    found = False

    sci, common, photo_url = _lookup_via_inat(query)
    if sci or common:
        if common:
            result["name"] = common
        if sci:
            result["scientific_name"] = sci
        if photo_url:
            result["photo_url"] = photo_url
        found = True

    # OpenFarm for growing / care data (works for many edible and ornamental plants)
    try:
        resp = requests.get(
            "https://openfarm.cc/api/v1/crops",
            params={"filter": query},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            attrs = data[0].get("attributes", {})
            if attrs.get("sun_requirements"):
                result["sun_needs"] = attrs["sun_requirements"]
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
            found = True
    except Exception:
        pass

    if not found:
        return None, "No plant information found for that name."

    return result, None
