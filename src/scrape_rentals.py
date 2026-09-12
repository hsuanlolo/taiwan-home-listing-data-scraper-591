#!/usr/bin/env python3
"""
Scrape 591.com.tw rental listings via the BFF JSON API.

API: GET https://bff-house.591.com.tw/v3/web/rent/list
     ?region={city}&firstRow={offset}&totalRows={total}

Outputs:
    data/raw/rentals_{date}.csv     – raw scraped data
    data/output/591_rentals.csv     – deduplicated final
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (
    Http591Client,
    ensure_dirs,
    load_config,
    normalize_area,
    normalize_price,
    parse_floor,
    parse_posttime,
    parse_room_str,
    setup_logging,
    today_str,
)

ROOT = Path(__file__).resolve().parent.parent


def scrape_city(client, cfg, region_id, city_name, logger):
    """Scrape all rental listings for one city."""
    api_base = cfg["rental"]["api_base"]
    api_path = cfg["rental"]["api_path"]
    referer = cfg["rental"]["referer"]
    origin = cfg["rental"]["origin"]
    per_page = cfg["rental"]["items_per_page"]
    max_pages = cfg["rental"]["max_pages"]

    all_items = []
    seen_ids = set()
    total_rows = 0

    for page in range(max_pages):
        first_row = page * per_page
        params = {
            "regionid": region_id,
            "firstRow": first_row,
            "totalRows": total_rows,
            "order": "posttime",
            "orderType": "desc",
        }
        try:
            data = client.get_json(
                api_base, api_path, params=params, referer=referer, origin=origin
            )
        except Exception as exc:
            logger.error("API error for %s page %d: %s", city_name, page, exc)
            break

        if data.get("status") != 1:
            logger.warning("Non-success status for %s page %d: %s",
                           city_name, page, data.get("status"))
            break

        inner = data.get("data", {})
        if page == 0 and inner.get("total"):
            total_rows = inner["total"]
            logger.info("%s: %s total listings", city_name, total_rows)

        items = inner.get("items", [])
        if not items:
            logger.info("%s: no more items at page %d", city_name, page)
            break

        new_count = 0
        for item in items:
            lid = item.get("id")
            if lid and lid not in seen_ids:
                seen_ids.add(lid)
                item["_city_name"] = city_name
                item["_region_id"] = region_id
                all_items.append(item)
                new_count += 1

        logger.info(
            "%s page %d: %d items, %d new (total %d)",
            city_name, page, len(items), new_count, len(all_items),
        )

        if len(items) < per_page:
            break

    return all_items


def _extract_district(address: str) -> str:
    """Extract district name from address like '中山區-南京東路三段259號'."""
    import re
    if not address:
        return ""
    m = re.match(r"(.+?區)", address)
    return m.group(1) if m else ""


def normalize_rental(item: dict) -> dict:
    """Transform a raw API item into a flat row."""
    floor, total_floors = parse_floor(item.get("floor_name", ""))
    room_info = parse_room_str(item.get("room", "") or item.get("layoutStr", ""))
    posted = parse_posttime(item.get("posttime"))
    scrape = today_str()

    tags = item.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    raw_address = (item.get("address") or "").replace("\\u002F", "/")
    district = item.get("section_name") or _extract_district(raw_address)

    return {
        "listing_id": item.get("id"),
        "listing_type": "rental",
        "title": item.get("title", ""),
        "city": item.get("_city_name", ""),
        "region_id": item.get("_region_id"),
        "district": district,
        "sectionid": item.get("sectionid"),
        "address": raw_address,
        "rent": normalize_price(item.get("price")),
        "price_unit": (item.get("price_unit") or "").replace("\\u002F", "/"),
        "area_ping": normalize_area(item.get("area")),
        "area_display": (item.get("area_name") or "").replace("\\u002F", "/"),
        "layout": item.get("layoutStr", ""),
        "floor": floor,
        "total_floors": total_floors,
        "floor_name": (item.get("floor_name") or "").replace("\\u002F", "/"),
        "rooms": room_info["rooms"],
        "living_rooms": room_info["living_rooms"],
        "bathrooms": room_info["bathrooms"],
        "property_type": item.get("kind_name", ""),
        "property_type_code": item.get("kind"),
        "fitment": item.get("fitment_name", ""),
        "community_name": item.get("community_name", ""),
        "role_name": item.get("role_name", ""),
        "has_elevator": "有電梯" in " ".join(tags),
        "allows_cooking": "可開伙" in " ".join(tags),
        "allows_pet": "可養寵物" in " ".join(tags),
        "near_metro": "近捷運" in " ".join(tags),
        "posted_date": posted,
        "scrape_date": scrape,
        "days_on_market": (
            (datetime.strptime(scrape, "%Y-%m-%d") - datetime.strptime(posted, "%Y-%m-%d")).days
            if posted else None
        ),
        "browse_count": item.get("browse_count"),
        "listing_url": item.get("url", ""),
        "photo_url": (item.get("cover") or "").replace("\\u002F", "/"),
    }


def main():
    os.chdir(ROOT)
    config = load_config()
    ensure_dirs(config)
    logger = setup_logging("scrape_rentals", config["paths"]["log_dir"])
    client = Http591Client(config, logger)

    active_cities = config.get("active_cities", [1])
    city_map = config["target_cities"]

    all_rows = []
    for region_id in active_cities:
        city_name = city_map.get(region_id, city_map.get(str(region_id), f"city_{region_id}"))
        logger.info("=== Scraping rentals for %s (region=%s) ===", city_name, region_id)
        items = scrape_city(client, config, region_id, city_name, logger)
        for item in items:
            try:
                row = normalize_rental(item)
                all_rows.append(row)
            except Exception as exc:
                logger.warning("Normalize error for listing %s: %s",
                               item.get("id"), exc)

    df = pd.DataFrame(all_rows)
    if df.empty:
        logger.warning("No rental data collected.")
        return

    df.drop_duplicates(subset=["listing_id"], keep="first", inplace=True)
    logger.info("Total unique rental listings: %d", len(df))

    date = today_str()
    raw_path = Path(config["paths"]["raw_dir"]) / f"rentals_{date}.csv"
    df.to_csv(raw_path, index=False, encoding="utf-8-sig")
    logger.info("Raw data saved to %s", raw_path)

    output_path = Path(config["paths"]["output_dir"]) / "591_rentals.csv"
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("Output saved to %s", output_path)

    logger.info(
        "Rental scrape complete: %d listings across %d cities",
        len(df), len(active_cities),
    )


if __name__ == "__main__":
    main()
