import base64
import json
import os
from typing import Dict, List, Optional, Tuple

import anthropic
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
# Claude lookup for plant care data
# ---------------------------------------------------------------------------

_PLANT_SCHEMA = {
    "type": "object",
    "properties": {
        "name":               {"type": "string"},
        "scientific_name":    {"type": "string"},
        "sun_needs":          {"type": "string", "enum": ["full_sun", "part_shade", "shade", ""]},
        "watering_needs":     {"type": "string"},
        "lifecycle":          {"type": "string", "enum": ["annual", "biennial", "perennial", ""]},
        "size_info":          {"type": "string"},
        "flowering_schedule": {"type": "string"},
        "recognized":         {"type": "boolean"},
    },
    "required": ["name", "scientific_name", "sun_needs", "watering_needs",
                 "lifecycle", "size_info", "flowering_schedule", "recognized"],
    "additionalProperties": False,
}


def _lookup_via_claude(query: str) -> Optional[Dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system=(
                "You are a plant encyclopedia. Return accurate care data for the plant given. "
                "If the name is completely unrecognizable, set recognized=false and leave other fields empty."
            ),
            output_config={"format": {"type": "json_schema", "schema": _PLANT_SCHEMA}},
            messages=[{"role": "user", "content": f"Plant: {query.strip()}"}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not text:
            return None
        data = json.loads(text)
        if not data.get("recognized"):
            return None
        return {
            "name":               data.get("name") or query.strip(),
            "scientific_name":    data.get("scientific_name") or "",
            "sun_needs":          data.get("sun_needs") or "",
            "watering_needs":     data.get("watering_needs") or "",
            "flowering_schedule": data.get("flowering_schedule") or "",
            "lifecycle":          data.get("lifecycle") or "",
            "size_info":          data.get("size_info") or "",
            "spreads":            "",
            "photo_url":          None,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def lookup_plant_details(query: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if not query.strip():
        return None, "Enter a plant name or scientific name first."

    result = _lookup_via_claude(query)

    if result is None:
        # Claude unavailable or didn't recognise the name — fall back to iNaturalist names only
        sci, common, photo_url = _lookup_via_inat(query)
        if not sci and not common:
            return None, "No plant information found for that name."
        result = {
            "name":               common or query.strip(),
            "scientific_name":    sci or "",
            "sun_needs":          "",
            "watering_needs":     "",
            "flowering_schedule": "",
            "lifecycle":          "",
            "size_info":          "",
            "spreads":            "",
            "photo_url":          photo_url,
        }

    # Ensure we have a photo
    if not result.get("photo_url"):
        inat_q = result.get("name") or result.get("scientific_name") or query
        _, _, photo_url = _lookup_via_inat(inat_q)
        result["photo_url"] = photo_url

    return result, None
