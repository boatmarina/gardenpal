import base64
import json
import os
from typing import Dict, List, Optional, Tuple

import anthropic
import requests


def extract_plant_name_from_text(raw_text: str) -> Optional[str]:
    """Use Claude to pull the plant name out of raw OCR text. Returns None if unavailable."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not raw_text.strip():
        return None
    client = anthropic.Anthropic(api_key=api_key, timeout=7.0)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": (
                    "The following text was extracted via OCR from a plant tag or nursery label. "
                    "Identify the plant name — it could be a common name, scientific name, or cultivar name. "
                    "Reply with ONLY the plant name and nothing else. "
                    "If no plant name can be found, reply with an empty string.\n\n"
                    f"OCR text:\n{raw_text.strip()}"
                ),
            }],
        )
        name = next((b.text for b in response.content if b.type == "text"), "").strip()
        return name or None
    except Exception:
        return None


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
        response = requests.post("https://api.ocr.space/parse/image", data=payload, files=files, timeout=8)
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
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        return None, "Couldn't identify the plant from that photo — try a clearer shot or use a different method."

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

_CLAUDE_SYSTEM = (
    "You are a plant encyclopedia specializing in plants as grown in the Pacific Northwest "
    "of North America (USDA zones 7b–9b: mild wet winters, dry summers). "
    "Respond ONLY with a valid JSON object — no markdown, no explanation, nothing else. "
    "If the plant name is completely unrecognizable, respond with exactly: {\"recognized\": false}"
)

_CLAUDE_PROMPT = """\
Plant: {query}

Return a JSON object with these fields:
{{
  "recognized": true,
  "name": "most common English name",
  "scientific_name": "Genus species",
  "sun_needs": "full_sun or part_shade or shade (leave empty string if unknown)",
  "watering_needs": "frequent or average or minimal (leave empty string if unknown)",
  "lifecycle": "annual or biennial or perennial (leave empty string if unknown)",
  "size_info": "typical height and spread, e.g. '2–4 ft tall, 1–2 ft wide'",
  "flowering_schedule": "when it blooms in the PNW, e.g. 'June to August'",
  "pnw_native": true or false or null (true only if native to the Pacific Northwest of North America; null if uncertain),
  "evergreen_status": "evergreen or deciduous or semi-evergreen as it behaves in PNW conditions; empty string if unknown or not applicable (e.g. annual)"
}}"""


def _lookup_via_claude(query: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Returns (result, error_message)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None, "ANTHROPIC_API_KEY not set"
    client = anthropic.Anthropic(api_key=api_key, timeout=7.0)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system=_CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": _CLAUDE_PROMPT.format(query=query.strip())}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not text:
            return None, "Claude returned empty response"
        # Strip markdown fences if the model wrapped it anyway
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        if not isinstance(data, dict) or not data.get("recognized"):
            return None, f"Plant not recognized by Claude: {query}"
        return {
            "name":               data.get("name") or query.strip(),
            "scientific_name":    data.get("scientific_name") or "",
            "sun_needs":          data.get("sun_needs") or "",
            "watering_needs":     data.get("watering_needs") or "",
            "flowering_schedule": data.get("flowering_schedule") or "",
            "lifecycle":          data.get("lifecycle") or "",
            "size_info":          data.get("size_info") or "",
            "pnw_native":         data.get("pnw_native"),
            "evergreen_status":   data.get("evergreen_status") or "",
            "spreads":            "",
            "photo_url":          None,
        }, None
    except json.JSONDecodeError as exc:
        return None, f"Claude response was not valid JSON: {exc}"
    except anthropic.APIError as exc:
        return None, f"Claude API error: {exc}"
    except Exception as exc:
        return None, f"Claude lookup failed: {exc}"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def lookup_plant_details(query: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if not query.strip():
        return None, "Enter a plant name or scientific name first."

    result, claude_error = _lookup_via_claude(query)

    if result is None:
        # Claude unavailable or didn't recognise the name — fall back to iNaturalist names only
        sci, common, photo_url = _lookup_via_inat(query)
        if not sci and not common:
            err = claude_error or "No plant information found for that name."
            return None, err
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

    # Ensure we have a photo — try progressively broader queries for cultivars
    if not result.get("photo_url"):
        queries_to_try = []
        common = result.get("name") or ""
        sci = result.get("scientific_name") or ""
        if common:
            queries_to_try.append(common)
        if sci:
            queries_to_try.append(sci)
            # Strip cultivar notation (e.g. Cornus sanguinea 'Midwinter Fire' -> Cornus sanguinea)
            species_only = sci.split("'")[0].split('"')[0].strip()
            if species_only and species_only != sci:
                queries_to_try.append(species_only)
        if query.strip() not in queries_to_try:
            queries_to_try.append(query.strip())

        for inat_q in queries_to_try:
            _, _, photo_url = _lookup_via_inat(inat_q)
            if photo_url:
                result["photo_url"] = photo_url
                break

    return result, None
