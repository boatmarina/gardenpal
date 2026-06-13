import base64
import json
import os
from typing import Dict, List, Optional, Tuple

import anthropic
import requests


def resolve_scientific_name(common_name: str) -> Optional[str]:
    """Ask Claude for the scientific name of a plant given an informal or regional common name.
    Returns the scientific name string, or None if unavailable or unrecognised."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not common_name.strip():
        return None
    client = anthropic.Anthropic(api_key=api_key, timeout=7.0)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": (
                    "What is the scientific name (Genus species) of the plant known as "
                    f'"{common_name.strip()}"? '
                    "Reply with ONLY the scientific name (e.g. Cotinus coggygria). "
                    "If the name is not a recognisable plant, reply with an empty string."
                ),
            }],
        )
        name = next((b.text for b in response.content if b.type == "text"), "").strip()
        # Sanity-check: expect at least two words that look like a binomial
        parts = name.split()
        if len(parts) >= 2 and parts[0][0].isupper() and parts[1][0].islower():
            return name
        return None
    except Exception:
        return None


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


def identify_plant_from_image(file_storage, provider: str = "plantid", location: Optional[str] = None) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Returns (suggestions_list, error). suggestions_list is [] on error."""
    if file_storage is None or not file_storage.filename:
        return [], "Please attach a plant photo first."
    if provider == "gemini":
        return _identify_via_gemini(file_storage, location)
    if provider == "claude":
        return _identify_via_claude(file_storage, location)
    return _identify_via_plantid(file_storage, location)


_DEFAULT_LOCATION = "Pacific Northwest"


def _effective_location(location: Optional[str]) -> str:
    return (location or "").strip() or _DEFAULT_LOCATION


def _location_hint(location: Optional[str]) -> str:
    loc = _effective_location(location)
    return (
        f"The photo was taken in {loc}. "
        "Prefer species common to this region when confidence is similar."
    )


def _claude_system(location: Optional[str]) -> str:
    loc = _effective_location(location)
    return (
        f"You are a plant encyclopedia specializing in plants as grown in {loc}. "
        "Respond ONLY with a valid JSON object — no markdown, no explanation, nothing else. "
        "If the plant name is completely unrecognizable, respond with exactly: {\"recognized\": false}"
    )


def _make_details_prompt(query: str, location: Optional[str]) -> str:
    loc = _effective_location(location)
    bloom_hint = f"when it blooms in {loc}, e.g. 'June to August'"
    native_note = f"true only if native to {loc}; null if uncertain"
    evergreen_note = f"as it behaves in {loc} conditions; empty string if unknown or not applicable (e.g. annual)"
    desc_note = f"what it looks like, what it's known for, and any notable growing tips for {loc}. No markdown."
    return (
        f"Plant: {query}\n\n"
        "Return a JSON object with these fields:\n"
        "{\n"
        '  "recognized": true,\n'
        '  "name": "most common English name",\n'
        '  "scientific_name": "Genus species",\n'
        '  "sun_needs": "full_sun or part_shade or shade (leave empty string if unknown)",\n'
        '  "watering_needs": "frequent or average or minimal (leave empty string if unknown)",\n'
        '  "lifecycle": "annual or biennial or perennial (leave empty string if unknown)",\n'
        "  \"size_info\": \"typical height and spread, e.g. '2–4 ft tall, 1–2 ft wide'\",\n"
        f'  "flowering_schedule": "{bloom_hint}",\n'
        f'  "locally_native": true or false or null ({native_note}),\n'
        f'  "evergreen_status": "evergreen or deciduous or semi-evergreen {evergreen_note}",\n'
        '  "plant_form": "one of: tree, shrub, perennial, annual, climber, ground-cover, grass, fern, bulb, succulent, herb, bamboo — empty string if unknown",\n'
        '  "height_category": "low (under 2 ft) or medium (2–5 ft) or tall (5–13 ft) or large (13 ft+) — respond with just the key word: low, medium, tall, or large; empty string if unknown",\n'
        '  "deadheading": "yes (recommended for best blooms) or beneficial (optional but helpful) or not needed — empty string if not applicable (non-flowering) or unknown",\n'
        '  "deer_resistant": "yes or somewhat or no — empty string if unknown",\n'
        f'  "description": "1–2 sentence plain-English description: {desc_note}"\n'
        "}"
    )


def _api_error_msg(service: str, response) -> str:
    status = response.status_code
    if status == 429:
        return f"{service} quota reached — wait 30 seconds and try again, or switch to a different ID service in Tools."
    if status in (401, 403):
        return f"{service} rejected the API key (HTTP {status}) — check the key in your Vercel environment variables."
    try:
        detail = str(response.json())[:200]
    except Exception:
        detail = (response.text or "")[:200]
    return f"{service} error (HTTP {status}): {detail}"


def _identify_via_plantid(file_storage, location: Optional[str] = None) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    api_key = os.environ.get("PLANT_ID_API_KEY", "").strip()
    if not api_key:
        return [], "Plant.id is not configured — add PLANT_ID_API_KEY in Vercel environment variables."

    file_storage.stream.seek(0)
    raw = file_storage.stream.read()
    file_storage.stream.seek(0)

    encoded = base64.b64encode(raw).decode("ascii")
    payload = {"images": [encoded]}
    headers = {"Api-Key": api_key, "Content-Type": "application/json"}

    try:
        response = requests.post(
            "https://plant.id/api/v3/identification",
            json=payload,
            headers=headers,
            timeout=8,
        )
    except requests.RequestException as exc:
        return [], f"Couldn't reach plant.id ({type(exc).__name__}) — check your network or try again."

    if not response.ok:
        return [], _api_error_msg("plant.id", response)

    try:
        data = response.json()
    except ValueError:
        return None, "plant.id returned an unexpected response — try again."

    raw_suggestions = data.get("result", {}).get("classification", {}).get("suggestions", [])
    if not raw_suggestions:
        return [], "No plant suggestions found from that photo."

    out = []
    for s in raw_suggestions[:3]:
        scientific = s.get("name", "")
        common_names = s.get("details", {}).get("common_names", [])
        common = common_names[0] if common_names else ""
        probability = s.get("probability")
        pct = round(probability * 100) if isinstance(probability, (float, int)) else None
        confidence = f"{pct}%" if pct is not None else ""
        out.append({"scientific_name": scientific, "common_name": common, "confidence": confidence})
    return out, None


def _identify_via_gemini(file_storage, location: Optional[str] = None) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return [], "Google AI is not configured — add GEMINI_API_KEY in Vercel environment variables."

    file_storage.stream.seek(0)
    raw = file_storage.stream.read()
    file_storage.stream.seek(0)

    fname = (file_storage.filename or "").lower()
    if fname.endswith(".png"):
        mime = "image/png"
    elif fname.endswith(".webp"):
        mime = "image/webp"
    elif fname.endswith(".gif"):
        mime = "image/gif"
    else:
        mime = "image/jpeg"

    encoded = base64.b64encode(raw).decode("ascii")
    prompt = (
        "Identify the plant in this photo. Provide up to 3 possible matches ordered by likelihood.\n"
        f"{_location_hint(location)}\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{"recognized": true, "suggestions": [{"scientific_name": "Genus species", "common_name": "common name", "confidence": "high"}, ...]}\n'
        'If no plant is visible or recognizable, respond: {"recognized": false, "suggestions": []}'
    )
    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": encoded}},
        ]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300},
    }

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={api_key}",
            json=payload,
            timeout=8,
        )
    except requests.RequestException as exc:
        return [], f"Couldn't reach Google AI ({type(exc).__name__}) — check your network or try again."

    if not resp.ok:
        return [], _api_error_msg("Google AI", resp)

    try:
        data = resp.json()
    except ValueError:
        return [], "Google AI returned an unexpected response — try again."

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError):
        return [], "Couldn't parse the Google AI response — try again."

    if not result.get("recognized", True):
        return [], "No plant recognized in that photo — try a clearer shot."

    sgs = result.get("suggestions") or []
    if not sgs and result.get("scientific_name"):
        sgs = [{"scientific_name": result["scientific_name"], "common_name": result.get("common_name", ""), "confidence": result.get("confidence", "")}]
    out = [{"scientific_name": s.get("scientific_name", ""), "common_name": s.get("common_name", ""), "confidence": s.get("confidence", "")} for s in sgs[:3]]
    return (out if out else []), (None if out else "No plant recognized in that photo — try a clearer shot.")


def _identify_via_claude(file_storage, location: Optional[str] = None) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None, "Claude Vision is not configured — add ANTHROPIC_API_KEY."

    file_storage.stream.seek(0)
    raw = file_storage.stream.read()
    file_storage.stream.seek(0)

    fname = (file_storage.filename or "").lower()
    if fname.endswith(".png"):
        media_type = "image/png"
    elif fname.endswith(".webp"):
        media_type = "image/webp"
    elif fname.endswith(".gif"):
        media_type = "image/gif"
    else:
        media_type = "image/jpeg"

    encoded = base64.b64encode(raw).decode("ascii")
    client = anthropic.Anthropic(api_key=api_key, timeout=8.0)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": encoded},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Identify the plant in this photo. Provide up to 3 possible matches ordered by likelihood.\n"
                            + f"{_location_hint(location)}\n"
                            + "Respond ONLY with valid JSON, no markdown:\n"
                            '{"recognized": true, "suggestions": [{"scientific_name": "Genus species", "common_name": "common name", "confidence": "high"}, ...]}\n'
                            'If no plant is visible or recognizable: {"recognized": false, "suggestions": []}'
                        ),
                    },
                ],
            }],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)
    except json.JSONDecodeError:
        return [], "Couldn't parse the Claude response — try again."
    except anthropic.APIError as exc:
        return [], f"Claude API error: {exc}"
    except Exception as exc:
        return [], f"Claude Vision error: {exc}"

    if not result.get("recognized", True):
        return [], "No plant recognized in that photo — try a clearer shot."

    sgs = result.get("suggestions") or []
    if not sgs and result.get("scientific_name"):
        sgs = [{"scientific_name": result["scientific_name"], "common_name": result.get("common_name", ""), "confidence": result.get("confidence", "")}]
    out = [{"scientific_name": s.get("scientific_name", ""), "common_name": s.get("common_name", ""), "confidence": s.get("confidence", "")} for s in sgs[:3]]
    return (out if out else []), (None if out else "No plant recognized in that photo — try a clearer shot.")


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


def lookup_plant_photos(query: str, count: int = 3, taxon_id: Optional[int] = None) -> List[str]:
    """Return up to `count` iNaturalist photo URLs for the plant.

    If taxon_id is provided the taxa lookup is skipped (saves one API call).
    Photos come from iNaturalist observations (includes cultivated/garden plants).
    """
    try:
        taxon_photos: List[str] = []
        if not taxon_id:
            if not query:
                return []
            taxa_resp = requests.get(
                "https://api.inaturalist.org/v1/taxa",
                params={"q": query, "is_active": "true", "iconic_taxa": "Plantae", "per_page": 1},
                timeout=8,
            )
            taxa_resp.raise_for_status()
            taxa = taxa_resp.json().get("results", [])
            if not taxa:
                return []
            taxon = taxa[0]
            taxon_id = taxon.get("id")
            # Collect taxon_photos as a fallback only — they live on iNaturalist's own
            # CDN (static.inaturalist.org) which can go stale. Observation photos are
            # stored on S3 (inaturalist-open-data.s3.amazonaws.com) and are more stable.
            for tp in (taxon.get("taxon_photos") or [])[:count]:
                p = (tp.get("photo") or {})
                url = p.get("medium_url") or p.get("square_url") or ""
                if url:
                    taxon_photos.append(url)

        if not taxon_id:
            return []

        # Fetch observations (no quality_grade filter — includes garden/cultivated plants)
        obs_resp = requests.get(
            "https://api.inaturalist.org/v1/observations",
            params={
                "taxon_id": taxon_id,
                "has[]": "photos",
                "per_page": count * 3,
                "order_by": "votes",
                "order": "desc",
            },
            timeout=8,
        )
        obs_resp.raise_for_status()
        observations = obs_resp.json().get("results", [])

        photos: List[str] = []
        for obs in observations:
            obs_photos = obs.get("photos", [])
            if obs_photos:
                raw_url = obs_photos[0].get("url", "")
                if raw_url:
                    # iNaturalist URLs use /square.jpg; swap to medium for better quality
                    medium_url = raw_url.replace("/square.", "/medium.")
                    photos.append(medium_url)
            if len(photos) >= count:
                break

        # If no observation photos found, fall back to the curated taxon_photos
        if not photos and taxon_photos:
            return taxon_photos[:count]

        return photos
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Claude lookup for plant care data
# ---------------------------------------------------------------------------

def _lookup_via_claude(query: str, location: Optional[str] = None) -> Tuple[Optional[Dict], Optional[str]]:
    """Returns (result, error_message)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None, "ANTHROPIC_API_KEY not set"
    client = anthropic.Anthropic(api_key=api_key, timeout=7.0)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=700,
            system=_claude_system(location),
            messages=[{"role": "user", "content": _make_details_prompt(query.strip(), location)}],
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
            "pnw_native":         data.get("locally_native"),
            "evergreen_status":   data.get("evergreen_status") or "",
            "plant_form":         data.get("plant_form") or "",
            "height_category":    data.get("height_category") or "",
            "spreads":            "",
            "photo_url":          None,
            "description":        data.get("description") or "",
            "deadheading":        data.get("deadheading") or "",
            "deer_resistant":     data.get("deer_resistant") or "",
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

def lookup_plant_details(query: str, location: Optional[str] = None) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if not query.strip():
        return None, "Enter a plant name or scientific name first."

    result, claude_error = _lookup_via_claude(query, location)

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
            "deadheading":        "",
            "deer_resistant":     "",
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
