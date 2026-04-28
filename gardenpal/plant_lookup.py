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


def lookup_plant_details(query: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    api_key = os.environ.get("PERENUAL_API_KEY", "").strip()
    if not api_key:
        return None, "Name lookup is not configured. Add PERENUAL_API_KEY."
    if not query.strip():
        return None, "Enter a plant name or scientific name first."

    try:
        list_response = requests.get(
            "https://perenual.com/api/species-list",
            params={"key": api_key, "q": query.strip()},
            timeout=20,
        )
        list_response.raise_for_status()
        list_data = list_response.json().get("data", [])
    except requests.RequestException:
        return None, "Plant lookup request failed."

    if not list_data:
        return None, "No plant match found for that query."

    top = list_data[0]
    species_id = top.get("id")
    details = {}

    if species_id:
        try:
            detail_response = requests.get(
                f"https://perenual.com/api/species/details/{species_id}",
                params={"key": api_key},
                timeout=20,
            )
            detail_response.raise_for_status()
            details = detail_response.json()
        except requests.RequestException:
            details = {}

    sunlight = details.get("sunlight") or top.get("sunlight") or []
    cycle = details.get("cycle") or top.get("cycle") or ""
    watering = details.get("watering") or ""
    dimensions = details.get("dimension") or details.get("dimensions") or ""
    spread = details.get("spread") or ""
    flowering = details.get("flowers") or details.get("flowering_season") or ""

    common_name = top.get("common_name") or ""
    scientific_list = top.get("scientific_name") or []
    scientific_name = scientific_list[0] if scientific_list else (details.get("scientific_name") or "")

    return {
        "name": common_name or query.strip(),
        "scientific_name": scientific_name,
        "sun_needs": ", ".join(sunlight) if isinstance(sunlight, list) else str(sunlight),
        "watering_needs": watering,
        "flowering_schedule": flowering,
        "lifecycle": cycle,
        "size_info": dimensions,
        "spreads": spread,
    }, None
