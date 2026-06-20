#!/usr/bin/env python3
"""
Energievakman Uniec3 Generator - MVP v13 real-time BAG/PDOK + Kadaster BAG + 3DBAG

Doel:
- Gebruik een leeg .uniec3-template als basis.
- Haal BAG/PDOK gegevens real-time op voor adres, VBO-ID, pand-ID, bouwjaar en gebruiksoppervlakte.
- Haal gebouwhoogte op uit 3DBAG.
- Vul bekende Uniec3-velden, zonder plaats automatisch te forceren, waaronder INFIL_BGH, AFMLOC_* en UNIT-RZAG.
- Pak opnieuw in als .uniec3.

Installatie:
  pip install requests

Gebruik:
  python energievakman_uniec3_generator.py \
    --template "Template woning bestaand_2026-06-20-2.uniec3" \
    --address "Vlierboomstraat 652, Den Haag" \
    --output "Vlierboomstraat 652.uniec3"

Handmatige 3DBAG hoogte fallback:
  python energievakman_uniec3_generator.py \
    --template "Template woning bestaand_2026-06-20-2.uniec3" \
    --address "Vlierboomstraat 652, Den Haag" \
    --height 10.20 \
    --output "Vlierboomstraat 652.uniec3"

Let op:
- Altijd openen/controleren in Uniec3 voordat je het definitief gebruikt.
- Deze lege template bevat mogelijk nog geen AFMELDLOCATIE/AFMELDOBJECT-entiteiten.
  Als die ontbreken, kan het script die adresvelden niet vervangen, alleen velden die al bestaan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import zipfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote_plus

import requests

PDOK_FREE_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
PDOK_BAG_OGC_URL = "https://api.pdok.nl/kadaster/bag/ogc/v2"
THREEDBAG_URL = "https://api.3dbag.nl"
KADASTER_BAG_V2_URL = "https://api.bag.kadaster.nl/lvbag/individuelebevragingen/v2"


def dutch_decimal(value: Any, decimals: int = 2) -> str:
    if value is None or value == "":
        return ""
    v = float(str(value).replace(",", "."))
    return f"{v:.{decimals}f}".replace(".", ",")


def strip_zipcode(pc: str | None) -> str:
    return (pc or "").replace(" ", "").upper()


def split_pdok_id(doc_id: str) -> str:
    # PDOK id kan bv. adr-0518010000808173-... zijn. We pakken de eerste lange numerieke reeks.
    m = re.search(r"(\d{16})", doc_id or "")
    return m.group(1) if m else (doc_id or "")


def find_first_key_deep(obj: Any, keys: Iterable[str]) -> Optional[Any]:
    keys_lower = {k.lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys_lower and v not in (None, ""):
                return v
        for v in obj.values():
            found = find_first_key_deep(v, keys_lower)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_key_deep(item, keys_lower)
            if found not in (None, ""):
                return found
    return None


def get_nested(obj: Any, *paths: str) -> Optional[Any]:
    """Haal eerste niet-lege waarde uit geneste dict met puntpaden."""
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", []):
            return cur
    return None


def clean_bag_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    # 16-cijferige BAG ID uit o.a. adr-0518... of NL.IMBAG.Pand.0518...
    m = re.search(r"(\d{16})", text)
    return m.group(1) if m else text.strip()





def get_by_path(obj: Any, path: list[str]) -> Optional[Any]:
    cur = obj
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def request_json(url: str, *, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(url, params=params or {}, headers=headers or {}, timeout=timeout)
        if r.status_code in (200, 201):
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def kadaster_headers(api_key: Optional[str]) -> Dict[str, str]:
    if not api_key:
        return {}
    # Kadaster BAG API gebruikt doorgaans X-Api-Key; sommige gateways accepteren api-key.
    return {
        "X-Api-Key": api_key,
        "api-key": api_key,
        "Accept": "application/hal+json, application/json",
    }


def find_href_for_pand(obj: Any) -> str:
    """Zoek in Kadaster HAL-response naar een href met /panden/<id>."""
    if isinstance(obj, dict):
        href = obj.get("href")
        if isinstance(href, str) and "/panden/" in href:
            return href
        for v in obj.values():
            found = find_href_for_pand(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_href_for_pand(item)
            if found:
                return found
    return ""


def extract_first_16_digit_id(obj: Any, prefer_pand: bool = False) -> str:
    """Pak een 16-cijferige BAG-id uit nested data/links. Bij prefer_pand eerst hrefs met /panden/."""
    if prefer_pand:
        href = find_href_for_pand(obj)
        m = re.search(r"/panden/(\d{16})", href)
        if m:
            return m.group(1)
    text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    ids = re.findall(r"\b\d{16}\b", text)
    return ids[0] if ids else ""


def extract_year(value: Any) -> str:
    if value in (None, "", []):
        return ""
    m = re.search(r"(18|19|20)\d{2}", str(value))
    return m.group(0) if m else str(value)

def normalise_area(value: Any) -> str:
    """Maak BAG/PDOK oppervlakte geschikt voor Uniec: 87 -> 87,00."""
    if value in (None, "", []):
        return ""
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    text = str(value).strip()
    # Locatieserver geeft soms ranges zoals "80 - 90". Pak dan het hoogste getal.
    nums = re.findall(r"\d+(?:[\.,]\d+)?", text)
    if not nums:
        return ""
    val = max(float(n.replace(',', '.')) for n in nums)
    return dutch_decimal(val, 2)


def extract_area_from_pdok_doc(d: Dict[str, Any]) -> str:
    """Robuuste GO-extractie uit Locatieserver/BAG responses."""
    keys = [
        "oppervlakte", "gebruiksoppervlakte", "oppervlakteVerblijfsobject",
        "oppervlakteverblijfsobject", "woonoppervlakte", "oppervlakte_min", "oppervlakte_max"
    ]
    for k in keys:
        v = d.get(k)
        area = normalise_area(v)
        if area:
            return area
    v = find_first_key_deep(d, keys)
    return normalise_area(v)

def collection_variants(collection: str) -> list[str]:
    mapping = {
        "verblijfsobject": ["verblijfsobject", "verblijfsobjecten"],
        "pand": ["pand", "panden"],
    }
    return mapping.get(collection, [collection])


def ogc_get_item(collection: str, item_id: str) -> Optional[Dict[str, Any]]:
    """Probeer direct een item op ID op te halen bij PDOK BAG OGC."""
    if not item_id:
        return None
    for coll in collection_variants(collection):
        for iid in [item_id, clean_bag_id(item_id), f"NL.IMBAG.{coll.capitalize()}.{clean_bag_id(item_id)}"]:
            try:
                r = requests.get(
                    f"{PDOK_BAG_OGC_URL}/collections/{coll}/items/{quote_plus(iid)}",
                    params={"f": "json"},
                    timeout=25,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict) and (data.get("properties") or data.get("id")):
                        return data
            except Exception:
                pass
    return None


def ogc_items(collection: str, params: Dict[str, Any], limit: int = 10) -> list[Dict[str, Any]]:
    """PDOK BAG OGC Features. Probeert meerdere filtervarianten."""
    variants = []
    base = {**params, "f": "json", "limit": limit}
    variants.append(base)
    if "filter" in params:
        variants.append({**base, "filter-lang": "cql2-text"})
    for p in variants:
        try:
            for coll in collection_variants(collection):
                r = requests.get(f"{PDOK_BAG_OGC_URL}/collections/{coll}/items", params=p, timeout=25)
                if r.status_code != 200:
                    continue
                features = r.json().get("features", [])
                if features:
                    return features
        except Exception:
            continue
    return []


def ogc_first(collection: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = ogc_items(collection, params, limit=1)
    return items[0] if items else None


def first_existing_prop(props: Dict[str, Any], names: list[str]) -> Optional[Any]:
    for n in names:
        if n in props and props[n] not in (None, "", []):
            return props[n]
    return find_first_key_deep(props, names)


def parse_rd_point(value: Any) -> Optional[tuple[float, float]]:
    """PDOK locatieserver geeft vaak centroide_rd als 'POINT(x y)'."""
    if not value:
        return None
    m = re.search(r"POINT\s*\(\s*([0-9.]+)\s+([0-9.]+)\s*\)", str(value), re.I)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def pick_pand_from_point(x: float, y: float) -> Optional[Dict[str, Any]]:
    """Fallback: zoek panden in een kleine bbox rondom het adrespunt."""
    # Kleine bbox in RD meters. Soms ligt het adrespunt net naast het pand; daarom meerdere marges.
    for delta in [0.5, 2, 5, 10, 20]:
        bbox = f"{x-delta},{y-delta},{x+delta},{y+delta}"
        features = ogc_items("pand", {
            "bbox": bbox,
            "bbox-crs": "http://www.opengis.net/def/crs/EPSG/0/28992",
            "crs": "http://www.opengis.net/def/crs/EPSG/0/28992",
        }, limit=10)
        if features:
            return features[0]
    return None



def enrich_with_kadaster_bag_api(bag: Dict[str, Any], api_key: Optional[str] = None) -> Dict[str, Any]:
    """Vul GO, pand-ID en bouwjaar aan via Kadaster BAG API Individuele Bevragingen.

    Deze API is stabieler voor individuele objecten dan de PDOK Locatieserver.
    api_key kan via --kadaster-api-key of environment variable KADASTER_API_KEY.
    """
    api_key = api_key or os.getenv("KADASTER_API_KEY") or ""
    headers = kadaster_headers(api_key)
    vbo_id = clean_bag_id(bag.get("vbo_id"))
    if not vbo_id:
        return bag

    # Veel Kadaster endpoints werken zonder trailing slash; sommige gateways redirecten.
    vbo_urls = [
        f"{KADASTER_BAG_V2_URL}/verblijfsobjecten/{vbo_id}",
        f"{KADASTER_BAG_V2_URL}/verblijfsobjecten/{vbo_id}/lvc",
    ]
    vbo_data = None
    for url in vbo_urls:
        vbo_data = request_json(url, headers=headers)
        if vbo_data:
            break
    if not vbo_data:
        return bag

    bag["kadaster_vbo_found"] = True

    # GO kan op meerdere plekken/namen staan.
    opp = find_first_key_deep(vbo_data, [
        "oppervlakte", "gebruiksoppervlakte", "oppervlakteVerblijfsobject",
        "oppervlakteverblijfsobject", "gebruiksdoeloppervlakte"
    ])
    area = normalise_area(opp)
    if area:
        bag["gebruiksoppervlakte"] = area

    # Pand-ID staat vaak als relatie/link in _links.panden.href of als pandidentificaties.
    pand_id = find_first_key_deep(vbo_data, [
        "pandidentificatie", "pandIdentificatie", "pand_id", "pandid", "ligtInPand",
        "pandidentificaties", "pandIdentificaties"
    ])
    if isinstance(pand_id, list) and pand_id:
        pand_id = pand_id[0]
    if isinstance(pand_id, dict):
        pand_id = find_first_key_deep(pand_id, ["identificatie", "id", "href"])
    pand_id = clean_bag_id(pand_id) or extract_first_16_digit_id(vbo_data, prefer_pand=True)
    if pand_id:
        bag["pand_id"] = pand_id

    # Bouwjaar staat op pand.
    if pand_id:
        for url in [
            f"{KADASTER_BAG_V2_URL}/panden/{pand_id}",
            f"{KADASTER_BAG_V2_URL}/panden/{pand_id}/lvc",
        ]:
            pand_data = request_json(url, headers=headers)
            if not pand_data:
                continue
            bag["kadaster_pand_found"] = True
            bouwjaar = find_first_key_deep(pand_data, [
                "oorspronkelijkBouwjaar", "oorspronkelijkbouwjaar", "bouwjaar"
            ])
            bj = extract_year(bouwjaar)
            if bj:
                bag["bouwjaar"] = bj
            break

    return bag

def enrich_with_bag_ogc(bag: Dict[str, Any]) -> Dict[str, Any]:
    """Vul missende velden aan via PDOK BAG OGC.

    Belangrijk in v7:
    - Locatieserver geeft vaak wel adres + VBO + GO, maar géén bouwjaar/pand-ID.
    - Bouwjaar zit op BAG-pand. Daarom zoeken we eerst het pand via VBO-relatie,
      en als dat niet lukt via een kleine RD-bbox rond het adrespunt.
    """
    vbo_id = clean_bag_id(bag.get("vbo_id"))
    pand_id = clean_bag_id(bag.get("pand_id"))

    # 1) Verblijfsobject ophalen: direct item, ids, identificatie/filter.
    vbo_feature = None
    if vbo_id:
        for method in [
            lambda: ogc_get_item("verblijfsobject", vbo_id),
            lambda: ogc_first("verblijfsobject", {"ids": vbo_id}),
            lambda: ogc_first("verblijfsobject", {"filter": f"identificatie='{vbo_id}'"}),
            lambda: ogc_first("verblijfsobject", {"filter": f"identificatie = '{vbo_id}'"}),
        ]:
            vbo_feature = method()
            if vbo_feature:
                break

    if vbo_feature:
        props = vbo_feature.get("properties", {})
        opp = first_existing_prop(props, [
            "oppervlakte", "gebruiksoppervlakte", "gebruiksdoeloppervlakte", "oppervlakteVerblijfsobject"
        ])
        area = normalise_area(opp)
        if area:
            bag["gebruiksoppervlakte"] = area

        related_pand = first_existing_prop(props, [
            "pandidentificatie", "pand_id", "pandIdentificatie", "ligtInPand", "ligt_in_pand", "pandid", "pand"
        ])
        if isinstance(related_pand, list) and related_pand:
            related_pand = related_pand[0]
        if isinstance(related_pand, dict):
            related_pand = first_existing_prop(related_pand, ["identificatie", "id"])
        if related_pand:
            bag["pand_id"] = clean_bag_id(related_pand)
            pand_id = bag["pand_id"]

    # 2) Als pand-ID nog ontbreekt: zoek pand via RD-adrespunt.
    pand_feature = None
    if pand_id:
        for method in [
            lambda: ogc_get_item("pand", pand_id),
            lambda: ogc_first("pand", {"ids": pand_id}),
            lambda: ogc_first("pand", {"filter": f"identificatie='{pand_id}'"}),
            lambda: ogc_first("pand", {"filter": f"identificatie = '{pand_id}'"}),
        ]:
            pand_feature = method()
            if pand_feature:
                break

    if not pand_feature:
        point = parse_rd_point(bag.get("centroide_rd"))
        if point:
            pand_feature = pick_pand_from_point(point[0], point[1])

    if pand_feature:
        props = pand_feature.get("properties", {})
        # Let op: PDOK OGC feature.id kan een UUID zijn. Voor 3DBAG hebben we de echte BAG pandidentificatie nodig.
        found_pand_id = first_existing_prop(props, ["identificatie", "pandidentificatie", "pandIdentificatie", "pandid", "id"]) or pand_feature.get("id")
        found_pand_id = clean_bag_id(found_pand_id)
        if found_pand_id and re.fullmatch(r"\d{16}", found_pand_id):
            bag["pand_id"] = found_pand_id
            pand_id = bag["pand_id"]
        bouwjaar = first_existing_prop(props, [
            "oorspronkelijkBouwjaar", "oorspronkelijkbouwjaar", "bouwjaar", "pand_bouwjaar"
        ])
        if bouwjaar not in (None, ""):
            bag["bouwjaar"] = str(bouwjaar)

    return bag

def pdok_lookup_address(address: str, kadaster_api_key: Optional[str] = None) -> Dict[str, Any]:
    # PDOK Locatieserver = adres zoeken en BAG/VBO-ID achterhalen.
    params = {"q": address, "fq": "type:adres", "rows": 1, "fl": "*"}
    r = requests.get(PDOK_FREE_URL, params=params, timeout=25)
    r.raise_for_status()
    docs = r.json().get("response", {}).get("docs", [])
    if not docs:
        raise RuntimeError(f"Geen PDOK/BAG adresresultaat voor: {address}")

    d = docs[0]
    huisnummer = str(d.get("huisnummer") or "")
    huisletter = d.get("huisletter") or ""
    toevoeging = d.get("huisnummertoevoeging") or ""
    straat = d.get("straatnaam") or d.get("openbare_ruimte") or ""
    plaats = d.get("woonplaatsnaam") or d.get("gemeentenaam") or ""
    postcode = strip_zipcode(d.get("postcode"))

    vbo_id = clean_bag_id(
        d.get("adresseerbaarobject_id")
        or d.get("verblijfsobject_id")
        or d.get("nummeraanduiding_id")
        or d.get("id", "")
    )
    pand_id = clean_bag_id(d.get("pand_id") or d.get("pandidentificatie") or d.get("pand_identificatie") or "")
    bouwjaar = d.get("bouwjaar") or d.get("oorspronkelijkbouwjaar") or ""
    # Bewaar Locatieserver-oppervlakte apart: dit was de variant die in v6 goed werkte.
    # De OGC-verrijking mag deze niet per ongeluk leeg overschrijven.
    oppervlakte = extract_area_from_pdok_doc(d)
    oppervlakte_raw = d.get("oppervlakte") or d.get("gebruiksoppervlakte") or d.get("oppervlakteVerblijfsobject") or ""

    adresregel = f"{straat} {huisnummer}{huisletter}{('-' + toevoeging) if toevoeging else ''}".strip()
    bag = {
        "raw_pdok": d,
        "centroide_rd": d.get("centroide_rd") or d.get("centroide_rd_x") or "",
        "centroide_ll": d.get("centroide_ll") or "",
        "adresregel": adresregel,
        "straat": straat,
        "huisnummer": huisnummer,
        "huisletter": huisletter,
        "huisnummertoevoeging": toevoeging,
        "postcode": postcode,
        "plaats": plaats,
        "vbo_id": str(vbo_id or ""),
        "pand_id": str(pand_id or ""),
        "bouwjaar": str(bouwjaar or ""),
        "gebruiksoppervlakte": str(oppervlakte or normalise_area(oppervlakte_raw) or ""),
        "gebruiksoppervlakte_locatieserver": str(oppervlakte or normalise_area(oppervlakte_raw) or ""),
    }
    # Aanvullen voor GO/pand/bouwjaar via Kadaster API als key beschikbaar is, daarna publieke PDOK OGC fallback.
    bag = enrich_with_kadaster_bag_api(bag, kadaster_api_key)
    bag = enrich_with_bag_ogc(bag)
    # Als OGC wel bouwjaar/pand vindt maar geen oppervlakte, gebruik alsnog de Locatieserver-GO.
    if not bag.get("gebruiksoppervlakte") and bag.get("gebruiksoppervlakte_locatieserver"):
        bag["gebruiksoppervlakte"] = bag["gebruiksoppervlakte_locatieserver"]
    return bag


def try_fetch_3dbag_height(pand_id: str) -> Optional[float]:
    """Haal gebouwhoogte uit 3DBAG op basis van BAG pand-ID.

    3DBAG geeft een CityJSONFeature terug. De pand-collectie gebruikt IDs als
    NL.IMBAG.Pand.<16 cijfers>. Hoogtevelden kunnen per release/LOD iets anders
    heten, daarom zoeken we recursief in alle properties/attributes.
    """
    pand_id = clean_bag_id(pand_id)
    if not pand_id:
        return None

    def as_float(v):
        try:
            if isinstance(v, (list, tuple)):
                return None
            return float(str(v).replace(",", "."))
        except Exception:
            return None

    def flatten_values(obj):
        if isinstance(obj, dict):
            # CityJSONFeature kan attributes/properties/feature bevatten
            for k, v in obj.items():
                yield k, v
                yield from flatten_values(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from flatten_values(item)

    candidates = [f"NL.IMBAG.Pand.{pand_id}", pand_id]
    endpoints = []
    for cid in candidates:
        endpoints += [
            f"{THREEDBAG_URL}/collections/pand/items/{quote_plus(cid)}",
            f"{THREEDBAG_URL}/collections/panden/items/{quote_plus(cid)}",
            f"{THREEDBAG_URL}/pand/{quote_plus(cid)}",
        ]
    for url in endpoints:
        for params in ({}, {"f": "json"}, {"f": "application/json"}):
            try:
                r = requests.get(url, params=params, timeout=30)
                if r.status_code != 200:
                    continue
                data = r.json()
                # API geeft soms {feature:{...}}, soms direct feature/properties
                search_obj = data.get("feature", data) if isinstance(data, dict) else data

                values = {}
                for k, v in flatten_values(search_obj):
                    if isinstance(k, str):
                        values[k.lower()] = v

                # 3DBAG-hoogtevelden. b3_h_* zijn in meters; als maaiveld beschikbaar is
                # en dakhoogte absoluut lijkt, trekken we maaiveld af.
                roof_keys = [
                    "b3_h_nok", "b3_h_dak_max", "b3_h_max", "b3_h_99p", "b3_h_95p", "b3_h_90p",
                    "b3_h_70p", "b3_h_dak_70p", "b3_h_50p", "b3_h_dak_50p",
                    "h_dak_max", "height", "measuredheight", "gebouwhoogte"
                ]
                ground_keys = ["b3_h_maaiveld", "b3_h_min", "h_maaiveld", "maaiveldhoogte", "groundheight"]

                ground = next((as_float(values.get(k)) for k in ground_keys if as_float(values.get(k)) is not None), None)
                for key in roof_keys:
                    roof = as_float(values.get(key))
                    if roof is None:
                        continue
                    if ground is not None and roof - ground > 0:
                        return round(roof - ground, 2)
                    # Als er geen maaiveld is en waarde lijkt al gebouwhoogte, gebruik direct.
                    if 0 < roof < 80:
                        return round(roof, 2)
            except Exception:
                continue
    return None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def new_guid() -> str:
    return str(uuid.uuid4())


def update_property_values(obj: Any, replacements: Dict[str, str]) -> int:
    changed = 0
    if isinstance(obj, dict):
        if obj.get("NTAPropertyId") in replacements:
            obj["Value"] = replacements[obj["NTAPropertyId"]]
            obj["Status"] = max(int(obj.get("Status", 2) or 2), 3)
            obj["Timestamp"] = now_iso()
            changed += 1
        for v in obj.values():
            changed += update_property_values(v, replacements)
    elif isinstance(obj, list):
        for item in obj:
            changed += update_property_values(item, replacements)
    return changed


def update_plain_keys(obj: Any, replacements: Dict[str, Any]) -> int:
    changed = 0
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if k in replacements:
                obj[k] = replacements[k]
                changed += 1
            else:
                changed += update_plain_keys(obj[k], replacements)
    elif isinstance(obj, list):
        for item in obj:
            changed += update_plain_keys(item, replacements)
    return changed


def build_replacements(bag: Dict[str, Any], height: Optional[float]) -> Dict[str, str]:
    r: Dict[str, str] = {
        # Afmeldlocatie/adresvelden - alleen als deze entiteiten in de template bestaan.
        "AFMLOC_OMSCHR": bag["adresregel"],
        "AFMLOC_STRAAT": bag["straat"],
        "AFMLOC_HUISNR": bag["huisnummer"],
        "AFMLOC_HUISLETTER": bag["huisletter"],
        "AFMLOC_HUISNRTOEV": bag["huisnummertoevoeging"],
        "AFMLOC_POSTCODE": bag["postcode"],
        "AFMLOC_BAG_ID": bag["vbo_id"],
        "AFMOBJ_BAG_ID": bag["vbo_id"],
        "AFMOBJ_PAND_ID": bag["pand_id"],
        # Veel voorkomende omschrijvingen / projectvelden.
        "GEB_OMSCHR": bag["adresregel"],
    }
    if bag.get("bouwjaar"):
        # In de lege template bestaat vaak geen algemeen bouwjaarveld; bestaande RZ-bouwjaarvelden vullen we wel.
        for key in ["GEB_BWJR", "GEB_BOUWJAAR"]:
            r[key] = bag["bouwjaar"]
    if bag.get("gebruiksoppervlakte"):
        # Bestaat niet altijd als invoerveld, wel soms als result-/opp-veld. Alleen vervangen als aanwezig.
        opp = normalise_area(bag["gebruiksoppervlakte"]) or dutch_decimal(bag["gebruiksoppervlakte"], 2)
        for key in ["RESULT-OPP_GEBROPP", "RESULT-OPP_VERLOPP", "VENT_OPP_GEM", "RZ_GEBRUIKSOPP", "GEB_GBO", "UNIT-RZAG"]:
            r[key] = opp
    if height is not None:
        r["INFIL_BGH"] = dutch_decimal(height, 2)
    return r



def prop(entity_id: str, pid: str, version: int, value: Any = None, status: int = 5) -> Dict[str, Any]:
    d = {
        "NTAPropertyId": pid,
        "NTAPropertyVersionId": version,
        "NTAPropertyDataId": f"{entity_id}:{pid}",
        "Status": status,
        "Timestamp": now_iso(),
    }
    if value is not None:
        d["Value"] = str(value)
    return d



def find_entity(entities: list, entity_id: str) -> Optional[Dict[str, Any]]:
    for e in entities:
        if e.get("NTAEntityId") == entity_id:
            return e
    return None

def set_or_add_property(entity: Dict[str, Any], pid: str, value: Any, version: int = 0, status: int = 5) -> None:
    props = entity.setdefault("NTAPropertyDatas", [])
    for pr in props:
        if pr.get("NTAPropertyId") == pid:
            pr["Value"] = str(value)
            pr["Status"] = status
            pr["Timestamp"] = now_iso()
            return
    eid = entity.get("NTAEntityDataId", new_guid())
    props.append(prop(eid, pid, version, value, status))


def uniec_place_code(plaats: str) -> str:
    """Uniec slaat plaats niet als tekst op in GEB_PL, maar als interne keuzelijst-code.
    Voor Den Haag / 's-Gravenhage is dit in de 3.4 referentie: 387.
    Breid deze dict later uit als je buiten Den Haag werkt.
    """
    p = (plaats or "").strip().lower()
    mapping = {
        "den haag": "387",
        "'s-gravenhage": "387",
        "s-gravenhage": "387",
        "gravenhage": "387",
        "the hague": "387",
    }
    return mapping.get(p, "")


def force_core_fields(entities: list, bag: Dict[str, Any], height: Optional[float]) -> None:
    # Deze functie schrijft velden rechtstreeks in de juiste entiteiten, ook als de property nog niet bestaat.
    geb = find_entity(entities, "GEB")
    if geb:
        # Belangrijk: Status 3 = actieve/ingevoerde waarde. Status 5 wordt door Uniec vaak niet als ingevuld getoond.
        set_or_add_property(geb, "GEB_OMSCHR", bag["adresregel"], 824, 3)
        if bag.get("bouwjaar"):
            set_or_add_property(geb, "GEB_BWJR", bag["bouwjaar"], 821, 3)
        set_or_add_property(geb, "GEB_SRTBW", "BESTB", 830, 3)
        set_or_add_property(geb, "GEB_CALCNEEDED", "true", 17357, 5)
        set_or_add_property(geb, "GEB_HASMELD", "False", 17350, 5)

    infil = find_entity(entities, "INFIL")
    if infil and height is not None:
        set_or_add_property(infil, "INFIL_BGH", dutch_decimal(height, 2), 952, 3)

    # GO: exact terug naar de simpele werkwijze die eerder werkte: alleen de eerste UNIT-RZ vullen.
    # Geen resultaatvelden forceren; Uniec lijkt die soms te herberekenen/overschrijven.
    unit_rz = find_entity(entities, "UNIT-RZ")
    if unit_rz and bag.get("gebruiksoppervlakte"):
        opp = normalise_area(bag["gebruiksoppervlakte"]) or dutch_decimal(bag["gebruiksoppervlakte"], 2)
        set_or_add_property(unit_rz, "UNIT-RZAG", opp, 942, 3)

    # Vul alle AFMELDLOCATIE-entiteiten waar mogelijk. De actieve wordt meestal zichtbaar in het afmeldscherm.
    for loc in [e for e in entities if e.get("NTAEntityId") == "AFMELDLOCATIE"]:
        set_or_add_property(loc, "AFMLOC_BAG_ID", bag["vbo_id"], 17421, 5)
        set_or_add_property(loc, "AFMLOC_OMSCHR", bag["adresregel"], 17411, 5)
        set_or_add_property(loc, "AFMLOC_STRAAT", bag["straat"], 17432, 5)
        set_or_add_property(loc, "AFMLOC_HUISNR", bag["huisnummer"], 17423, 5)
        set_or_add_property(loc, "AFMLOC_HUISLETTER", bag.get("huisletter", ""), 17424, 5)
        set_or_add_property(loc, "AFMLOC_HUISNRTOEV", bag.get("huisnummertoevoeging", ""), 17425, 5)
        set_or_add_property(loc, "AFMLOC_POSTCODE", bag["postcode"], 17422, 5)

def add_afmeld_entities_if_missing(entities: list, relations: list, building_id: int, bag: Dict[str, Any]) -> int:
    """Voegt de afmeld/adresentiteiten toe als een lege template ze nog niet bevat.

    Dit is nodig omdat een volledig lege Uniec-template soms nog geen AFMELDLOCATIE bevat.
    De versienummers/property-id's komen uit een door Uniec zelf aangemaakt voorbeeldbestand.
    """
    if any(e.get("NTAEntityId") == "AFMELDLOCATIE" for e in entities):
        return 0

    ts = now_iso()
    afminfo_id = new_guid()
    loc_id = new_guid()
    obj_id = new_guid()

    opname = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
    regdatum = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    afminfo = {
        "NTAEntityId": "AFMELDINFO", "NTAEntityVersionId": 7104, "Order": 100.0,
        "BuildingId": building_id, "NTAEntityDataId": afminfo_id, "Status": 2,
        "NTAPropertyDatas": [
            prop(afminfo_id, "AFM_AANLEIDING", 17400, "AFM_AANL_BESTB", 3),
            prop(afminfo_id, "AFM_ADVISEUR", 17402, "AFM_ADVISEUR_ZELFDE", 3),
            prop(afminfo_id, "AFM_ADV_ACHTERN", 17794),
            prop(afminfo_id, "AFM_ADV_EXAMENNUMMER", 17404),
            prop(afminfo_id, "AFM_ADV_TUSSENV", 17793),
            prop(afminfo_id, "AFM_ADV_VOORL", 17792),
            prop(afminfo_id, "AFM_IDENTIFICATIEMETHODE", 17405, "IDENTM_BAG", 3),
            prop(afminfo_id, "AFM_IDENT_METH_EERDER", 17406),
            prop(afminfo_id, "AFM_NAAM_ORIGINEEL", 17427),
            prop(afminfo_id, "AFM_NZELFST_WOONEENH_INDIV_VBO", 17553, "AFM_NZELFST_WOONEENH_INDIV_VBO_NAANW", 3),
            prop(afminfo_id, "AFM_PROJECTNAAM", 17407, bag["adresregel"], 5),
            prop(afminfo_id, "AFM_REGISTRATIE_ENERL", 17431),
            prop(afminfo_id, "AFM_REPRESENTATIVITEIT", 17408, "AFM_REPRES_NIET", 3),
            prop(afminfo_id, "AFM_STATUS", 17409),
        ],
        "Timestamp": ts,
    }

    loc = {
        "NTAEntityId": "AFMELDLOCATIE", "NTAEntityVersionId": 7106, "Order": 100.0,
        "BuildingId": building_id, "NTAEntityDataId": loc_id, "Status": 2,
        "NTAPropertyDatas": [
            prop(loc_id, "AFMLOC_BAG_ID", 17421, bag["vbo_id"], 3),
            prop(loc_id, "AFMLOC_DETAILAANDUIDING", 17426, ""),
            prop(loc_id, "AFMLOC_HUISLETTER", 17424, bag.get("huisletter", "")),
            prop(loc_id, "AFMLOC_HUISNR", 17423, bag["huisnummer"]),
            prop(loc_id, "AFMLOC_HUISNRTOEV", 17425, bag.get("huisnummertoevoeging", "")),
            prop(loc_id, "AFMLOC_LABEL_RES_ID", 17419),
            prop(loc_id, "AFMLOC_OMSCHR", 17411, bag["adresregel"]),
            prop(loc_id, "AFMLOC_OPNAMEDATUM", 17410, opname, 3),
            prop(loc_id, "AFMLOC_POSTCODE", 17422, bag["postcode"]),
            prop(loc_id, "AFMLOC_PROVISIONAL_ID", 17417),
            prop(loc_id, "AFMLOC_REPRESENTATIEF", 17416, "false"),
            prop(loc_id, "AFMLOC_STRAAT", 17432, bag["straat"]),
        ],
        "Timestamp": ts,
    }

    obj = {
        "NTAEntityId": "AFMELDOBJECT", "NTAEntityVersionId": 7105, "Order": 100.0,
        "BuildingId": building_id, "NTAEntityDataId": obj_id, "Status": 2,
        "NTAPropertyDatas": [
            prop(obj_id, "AFMOBJ_ACTIE", 17415, "AFM_ACTIE_NIEUW"),
            prop(obj_id, "AFMOBJ_ADV_ACHTERN", 17797),
            prop(obj_id, "AFMOBJ_ADV_EXAMENNUMMER", 17539),
            prop(obj_id, "AFMOBJ_ADV_TUSSENV", 17796),
            prop(obj_id, "AFMOBJ_ADV_VOORL", 17795),
            prop(obj_id, "AFMOBJ_CREDITS", 17414, "1"),
            prop(obj_id, "AFMOBJ_ERRORS", 17420),
            prop(obj_id, "AFMOBJ_REG_DATUM", 17413, regdatum),
            prop(obj_id, "AFMOBJ_REG_NUMMER", 17418),
            prop(obj_id, "AFMOBJ_STATUS", 17412, "1"),
        ],
        "Timestamp": ts,
    }

    entities[:0] = [afminfo, loc, obj]
    relations.insert(0, {
        "ParentId": obj_id,
        "NTAEntityIdParent": "AFMELDOBJECT",
        "ChildId": loc_id,
        "NTAEntityIdChild": "AFMELDLOCATIE",
        "BuildingId": building_id,
        "NTAEntityRelationDataId": f"{obj_id}:{loc_id}",
        "OnDelete": 1,
        "OnCopy": 1,
        "Timestamp": ts,
    })
    return 3


def make_uniec3(template: Path, output: Path, address: str, height_override: Optional[float] = None, bouwjaar_override: Optional[str] = None, pand_id_override: Optional[str] = None, gebruiksoppervlakte_override: Optional[str] = None, kadaster_api_key: Optional[str] = None) -> Dict[str, Any]:
    bag = pdok_lookup_address(address, kadaster_api_key)
    if pand_id_override:
        bag["pand_id"] = clean_bag_id(pand_id_override)
        bag = enrich_with_kadaster_bag_api(bag, kadaster_api_key)
        bag = enrich_with_bag_ogc(bag)
    if bouwjaar_override:
        bag["bouwjaar"] = str(bouwjaar_override)
    if gebruiksoppervlakte_override:
        bag["gebruiksoppervlakte"] = normalise_area(gebruiksoppervlakte_override)
    height = height_override if height_override is not None else try_fetch_3dbag_height(bag.get("pand_id", ""))
    replacements = build_replacements(bag, height)

    changed_files = []
    changed_properties: Dict[str, int] = {}

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        with zipfile.ZipFile(template, "r") as zin:
            zin.extractall(work)

        # Voeg afmeld/adresentiteiten toe als de lege template deze nog niet heeft.
        buildings_data = json.loads((work / "buildings.json").read_text(encoding="utf-8"))
        building_id = int(buildings_data[0]["BuildingId"])
        entities_path = next(work.rglob("entities.json"))
        relations_path = next(work.rglob("relations.json"))
        entities_data = json.loads(entities_path.read_text(encoding="utf-8"))
        relations_data = json.loads(relations_path.read_text(encoding="utf-8"))
        added_entities = add_afmeld_entities_if_missing(entities_data, relations_data, building_id, bag)
        # Forceer kernvelden in de juiste entiteiten, ook als de property in een lege template ontbreekt.
        force_core_fields(entities_data, bag, height)
        entities_path.write_text(json.dumps(entities_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        relations_path.write_text(json.dumps(relations_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        for json_path in work.rglob("*.json"):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            before = json.dumps(data, ensure_ascii=False, sort_keys=True)
            c1 = update_property_values(data, replacements)
            plain_replacements = {
                "GEB_OMSCHR": bag["adresregel"],
                "GEB_CALCNEEDED": "true",
                "GEB_HASMELD": "False",
                "GEB_SRTBW": "BESTB",
            }
            if json_path.name in {"projects.json", "folders.json"}:
                plain_replacements["Name"] = bag["adresregel"]
            c2 = update_plain_keys(data, plain_replacements)
            after = json.dumps(data, ensure_ascii=False, sort_keys=True)
            if before != after:
                json_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                changed_files.append(str(json_path.relative_to(work)))
                changed_properties[str(json_path.relative_to(work))] = c1 + c2

        # Laatste pass: na alle generieke updates nogmaals kernvelden forceren.
        # Hiermee voorkomen we dat Uniec-resultaat/summary-updates UNIT-RZAG of INFIL_BGH weer overschrijven.
        entities_data = json.loads(entities_path.read_text(encoding="utf-8"))
        force_core_fields(entities_data, bag, height)
        # Niet meer alle UNIT-RZAG-resultaatvelden globaal forceren; dit brak de zichtbaarheid van GO in jouw test.
        entities_path.write_text(json.dumps(entities_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for p in work.rglob("*"):
                if p.is_file():
                    zout.write(p, p.relative_to(work).as_posix())

    return {
        "input_address": address,
        "output": str(output),
        "bag_pdok": {k: v for k, v in bag.items() if k != "raw_pdok"},
        "height_used_m": height,
        "warning": None if height is not None and bag.get("bouwjaar") and bag.get("gebruiksoppervlakte") else "Controleer log: bouwjaar, GO en/of 3DBAG hoogte kon niet automatisch worden gevonden. Gebruik eventueel --pand-id, --bouwjaar of --height als fallback.",
        "changed_files": changed_files,
        "added_afmeld_entities": locals().get("added_entities", 0),
        "changed_properties_count": changed_properties,
        "debug_check": {"GO_written_as": bag.get("gebruiksoppervlakte"), "height_source": "override" if height_override is not None else "3DBAG", "pand_id_used_for_3DBAG": bag.get("pand_id")},
        "note": "Open en controleer altijd in Uniec3. Plaats wordt bewust niet automatisch ingevuld.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True, help="Pad naar leeg/basis .uniec3 template")
    ap.add_argument("--address", required=True, help='Bijv. "Vlierboomstraat 652, Den Haag"')
    ap.add_argument("--output", required=True, help="Pad voor nieuw .uniec3 bestand")
    ap.add_argument("--height", type=float, default=None, help="Handmatige gebouwhoogte in meters; overschrijft 3DBAG")
    ap.add_argument("--bouwjaar", default=None, help="Handmatige fallback voor bouwjaar; overschrijft BAG")
    ap.add_argument("--pand-id", default=None, help="Handmatige fallback voor BAG pand-ID; helpt bij bouwjaar en 3DBAG")
    ap.add_argument("--gebruiksoppervlakte", "--go", dest="gebruiksoppervlakte", default=None, help="Handmatige fallback voor GO in m²; overschrijft BAG/PDOK")
    ap.add_argument("--kadaster-api-key", default=os.getenv("KADASTER_API_KEY"), help="Optioneel: Kadaster BAG API key. Kan ook via environment variable KADASTER_API_KEY.")
    args = ap.parse_args()

    template = Path(args.template)
    if not template.exists():
        raise SystemExit(f"Template niet gevonden: {template.resolve()}")
    result = make_uniec3(template, Path(args.output), args.address, args.height, args.bouwjaar, args.pand_id, args.gebruiksoppervlakte, args.kadaster_api_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
