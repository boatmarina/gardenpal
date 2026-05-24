"""GardenPal MCP server — wraps the GardenPal REST API."""

import os
import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("GARDENPAL_API_URL", "").rstrip("/")
API_TOKEN = os.environ.get("GARDENPAL_API_TOKEN", "")

mcp = FastMCP("GardenPal")


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


def _check_config() -> None:
    if not API_URL or not API_TOKEN:
        raise ValueError(
            "GARDENPAL_API_URL and GARDENPAL_API_TOKEN environment variables must be set."
        )


@mcp.tool()
def list_garden_entries() -> list[dict]:
    """List all garden entries in the user's GardenPal tracker."""
    _check_config()
    resp = httpx.get(f"{API_URL}/api/garden/entries", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def add_garden_entry(
    plant_name: str,
    variety: str = "",
    location_type: str = "",
    location_name: str = "",
    planted_date: str = "",
    notes: str = "",
) -> dict:
    """Add a new garden entry to GardenPal.

    Args:
        plant_name: Name of the plant (e.g. 'Zucchini').
        variety: Cultivar or variety (e.g. 'Black Beauty').
        location_type: One of 'raised_bed', 'container', or 'in_ground'.
        location_name: Specific spot name (e.g. 'Bed 1', 'Big pot on deck').
        planted_date: ISO date string when planted (e.g. '2026-05-24').
        notes: Any additional observations or notes.
    """
    _check_config()
    payload = {
        "plant_name": plant_name,
        "variety": variety,
        "location_type": location_type,
        "location_name": location_name,
        "planted_date": planted_date,
        "notes": notes,
    }
    resp = httpx.post(
        f"{API_URL}/api/garden/entries",
        json={k: v for k, v in payload.items() if v},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def update_garden_entry(
    entry_id: int,
    plant_name: str = "",
    variety: str = "",
    location_type: str = "",
    location_name: str = "",
    planted_date: str = "",
    notes: str = "",
) -> dict:
    """Update an existing garden entry in GardenPal. Only provide the fields you want to change.

    Args:
        entry_id: The numeric ID of the garden entry to update.
        plant_name: Updated plant name.
        variety: Updated cultivar or variety.
        location_type: Updated location type ('raised_bed', 'container', or 'in_ground').
        location_name: Updated location name.
        planted_date: Updated planted date (ISO format, e.g. '2026-05-24').
        notes: Updated notes.
    """
    _check_config()
    payload = {
        k: v
        for k, v in {
            "plant_name": plant_name,
            "variety": variety,
            "location_type": location_type,
            "location_name": location_name,
            "planted_date": planted_date,
            "notes": notes,
        }.items()
        if v
    }
    resp = httpx.patch(
        f"{API_URL}/api/garden/entries/{entry_id}",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    mcp.run()
