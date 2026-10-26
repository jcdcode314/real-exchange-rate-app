"""IMF Real Exchange Rate Explorer. Run with Streamlit or --self-test/--live-smoke."""

from __future__ import annotations

import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import io
import math
import os
import re
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pycountry
import requests
import streamlit as st


API_BASE = "https://api.imf.org/external/sdmx/3.0"
FIRST_MONTH = pd.Timestamp("1976-01-01")
EURO_START = pd.Timestamp("1999-01-01")
DATA_TTL = 3600
METADATA_TTL = 86400
TEAL = "#087f8c"
ORANGE = "#c76b32"
KEY_TAILS = {"CPI": ("CPI", "_T", "IX", "M"), "ER": ("XDC_USD", "PA_RT", "M")}

# IMF's 191 members (including Liechtenstein, admitted in 2024), followed by
# five separately reported economies. Names are maintained by ISO's pycountry
# package; UVK and WBG are IMF-specific overrides, never ISO substitutions.
# Reference: https://www.imf.org/external/np/sec/memdir/memdate.htm
MEMBER_CODES = """
AFG ALB DZA AND AGO ATG ARG ARM AUS AUT AZE BHS BHR BGD BRB BLR BEL BLZ BEN BTN
BOL BIH BWA BRA BRN BGR BFA BDI CPV KHM CMR CAN CAF TCD CHL CHN COL COM COD COG
CRI CIV HRV CYP CZE DNK DJI DMA DOM ECU EGY SLV GNQ ERI EST SWZ ETH FJI FIN FRA
GAB GMB GEO DEU GHA GRC GRD GTM GIN GNB GUY HTI HND HUN ISL IND IDN IRN IRQ
IRL ISR ITA JAM JPN JOR KAZ KEN KIR KOR UVK KWT KGZ LAO LVA LBN LSO LBR LBY LIE
LTU LUX MDG MWI MYS MDV MLI MLT MHL MRT MUS MEX FSM MDA MNG MNE MAR MOZ MMR
NAM NRU NPL NLD NZL NIC NER NGA MKD NOR OMN PAK PLW PAN PNG PRY PER PHL POL
PRT QAT ROU RUS RWA WSM SMR STP SAU SEN SRB SYC SLE SGP SVK SVN SLB SOM ZAF
SSD ESP LKA KNA LCA VCT SDN SUR SWE CHE SYR TJK TZA THA TLS TGO TON TTO TUN
TUR TKM TUV UGA UKR ARE GBR USA URY UZB VUT VEN VNM YEM ZMB ZWE
""".split()
COUNTRY_OVERRIDES = {
    "UVK": "Kosovo", "XKX": "Kosovo", "WBG": "West Bank and Gaza",
    "USA": "United States", "GBR": "United Kingdom", "KOR": "Korea, Republic of",
    "IRN": "Iran", "RUS": "Russia", "LAO": "Lao PDR", "SYR": "Syria",
    "TWN": "Taiwan Province of China", "HKG": "Hong Kong SAR",
    "MAC": "Macao SAR", "G163": "Euro area",
}


class IMFError(RuntimeError):
    """An actionable, safe-to-display source/metadata error."""


class NoObservations(IMFError):
    """A successfully addressed series has no observations."""


@dataclass
class FlowInfo:
    flow: str
    version: str
    dimensions: tuple[str, ...]
    countries: dict[str, str]
    descriptions: dict[str, str]
    metadata_url: str
    dsd_reference: str


@dataclass
class SeriesResult:
    code: str
    flow: str
    values: pd.Series = field(default_factory=lambda: pd.Series(dtype=float, index=pd.DatetimeIndex([], name="month")))
    issues: list[str] = field(default_factory=list)
    error: str | None = None
    identity: bool = False
    url: str = ""
    fetched_at: str = ""

    @property
    def key(self) -> str:
        return ".".join((self.code, *KEY_TAILS[self.flow]))


def utc_month() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc).date()).replace(day=1)


def request_imf(path: str, accept: str, params: dict | None = None) -> requests.Response:
    """Verified TLS, bounded retries, and no credentials in URLs or error messages."""
    headers = {"Accept": accept, "User-Agent": "IMF-Real-Exchange-Rate-Explorer/1.0"}
    subscription_key = os.environ.get("IMF_SDMX_SUBSCRIPTION_KEY")
    if subscription_key:
        headers["Ocp-Apim-Subscription-Key"] = subscription_key
    url = API_BASE + path
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=(6, 35), allow_redirects=False)
        except requests.exceptions.SSLError:
            raise IMFError("IMF TLS certificate verification failed. Repair the trust configuration; TLS verification remains enabled.") from None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < 2:
                time.sleep(0.6 * 2**attempt)
                continue
            raise IMFError("Cannot reach api.imf.org (connection, proxy/network policy, or timeout). Allow the IMF host and retry; no substitute data were used.") from None
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            wait = response.headers.get("Retry-After", "")
            time.sleep(min(10.0, float(wait)) if wait.isdigit() else 0.6 * 2**attempt)
            continue
        if response.status_code in (401, 403):
            raise IMFError(f"IMF access denied (HTTP {response.status_code}). Check network policy and IMF API access. If IMF requires a subscription, set IMF_SDMX_SUBSCRIPTION_KEY securely.")
        if response.status_code == 429:
            raise IMFError("IMF rate limit (HTTP 429) persists after retries. Wait and use Refresh source data.")
        if response.status_code in (204, 404) and path.startswith("/data/"):
            raise NoObservations(f"No IMF observations returned (HTTP {response.status_code}).")
        if not 200 <= response.status_code < 300:
            raise IMFError(f"IMF returned HTTP {response.status_code} for {path}. Check the dataflow/version/key and try again later.")
        if not response.content.strip():
            raise NoObservations("The IMF returned an empty response.")
        return response
    raise IMFError("IMF request did not complete.")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def elements(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if local_name(e.tag) == name]


def english_name(node: ET.Element) -> str:
    names = [e for e in node if local_name(e.tag) == "Name"]
    english = [e for e in names if e.get("{http://www.w3.org/XML/1998/namespace}lang", "en").startswith("en")]
    return ((english or names)[0].text or "").strip() if names else ""


def reference(node: ET.Element) -> tuple[str, str, str] | None:
    """Read both SDMX 2.1 Ref elements and SDMX 3.0 URNs."""
    for e in node.iter():
        if local_name(e.tag) == "Ref" and e.get("id"):
            return e.get("agencyID", "IMF.STA"), e.attrib["id"], e.get("version", "+")
        for value in [e.get("urn", ""), e.text or ""]:
            match = re.search(r"=([^:=\s]+):([^()\s]+)\(([^)]+)\)", value)
            if match:
                return match.group(1), match.group(2), match.group(3)
    return None


def xml_root(response: requests.Response) -> ET.Element:
    content = response.content
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise IMFError("Unexpected XML entity declaration in IMF metadata.")
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        raise IMFError("IMF metadata is not SDMX structure XML (possibly an access or gateway page).") from None


def structure(path: str) -> ET.Element:
    response = request_imf(path, "application/vnd.sdmx.structure+xml;version=3.0.0")
    return xml_root(response)


def version_matches(actual: str, expression: str) -> bool:
    """Resolve SDMX 3.0 range URNs such as 5.0+.0 against returned artifacts."""
    if expression in ("+", "latest", "*"):
        return True
    if "+" not in expression:
        return actual == expression
    parts = expression.split(".")
    actual_parts = actual.split(".")
    if not all(p.isdigit() for p in actual_parts):
        return False
    range_at = next(i for i, p in enumerate(parts) if "+" in p)
    if actual_parts[:range_at] != parts[:range_at]:
        return False
    floor = tuple(int(p.replace("+", "") or 0) for p in parts)
    return tuple(map(int, actual_parts)) >= floor


def resolve_artifacts(roots: list[ET.Element], kind: str, ref: tuple[str, str, str]) -> list[ET.Element]:
    matches = [e for r in roots for e in elements(r, kind)
               if e.get("id") == ref[1] and e.get("agencyID", ref[0]) == ref[0]
               and version_matches(e.get("version", ""), ref[2])]
    return sorted(matches, key=lambda e: tuple(int(p) if p.isdigit() else 0 for p in e.get("version", "").split(".")), reverse=True)


def dimension_enumeration(dimension: ET.Element, roots: list[ET.Element]) -> tuple[str, str, str] | None:
    enum_nodes = elements(dimension, "Enumeration")
    if enum_nodes:
        return reference(enum_nodes[0])
    # The live IMF DSD inherits CoreRepresentation from a ConceptIdentity URN.
    identities = elements(dimension, "ConceptIdentity")
    if not identities:
        return None
    identity = identities[0]
    ref = reference(identity)
    if not ref:
        return None
    match = re.search(r"\)\.([^\s]+)$", (identity.text or "").strip())
    if match:
        concept_id = match.group(1)
    else:
        refs = elements(identity, "Ref")
        if not refs:
            return None
        concept_id = refs[0].get("id", "")
        ref = (refs[0].get("agencyID", "IMF"), refs[0].get("maintainableParentID", ""),
               refs[0].get("maintainableParentVersion", "+"))
    for scheme in resolve_artifacts(roots, "ConceptScheme", ref):
        for concept in scheme:
            if local_name(concept.tag) == "Concept" and concept.get("id") == concept_id:
                enum_nodes = elements(concept, "Enumeration")
                return reference(enum_nodes[0]) if enum_nodes else None
    return None


def parse_flow_metadata(flow: str, root: ET.Element, dsd_root: ET.Element | None = None) -> FlowInfo:
    flows = [e for e in elements(root, "Dataflow") if e.get("id") == flow and e.get("agencyID") == "IMF.STA"]
    if len(flows) != 1:
        raise IMFError(f"Cannot uniquely identify IMF.STA/{flow} in the live dataflow metadata.")
    dataflow = flows[0]
    version = dataflow.get("version", "")
    if not version or "+" in version or version in ("latest", "*"):
        raise IMFError(f"No concrete version was reported for IMF.STA/{flow}.")
    structures = elements(dataflow, "Structure")
    dsd_ref = reference(structures[0]) if structures else None
    if not dsd_ref:
        raise IMFError(f"No data-structure reference was found for IMF.STA/{flow}.")
    roots = [root] + ([dsd_root] if dsd_root is not None else [])
    dsds = resolve_artifacts(roots, "DataStructure", dsd_ref)
    if not dsds:
        raise IMFError(f"The referenced data structure {dsd_ref[0]}/{dsd_ref[1]}/{dsd_ref[2]} is absent.")
    lists = elements(dsds[0], "DimensionList")
    if not lists:
        raise IMFError("IMF metadata has no ordered DimensionList.")
    dimensions = [e for e in lists[0] if local_name(e.tag) in ("Dimension", "MeasureDimension")]
    try:
        dimensions.sort(key=lambda e: int(e.attrib["position"]))
    except (KeyError, ValueError):
        raise IMFError("IMF dimension positions are missing or invalid.") from None
    expected = (None, *KEY_TAILS[flow])
    if len(dimensions) != len(expected):
        raise IMFError(f"IMF {flow} key schema changed: expected {len(expected)} dimensions, received {len(dimensions)}. No data were calculated.")
    country_codes, descriptions = {}, {}
    for i, dimension in enumerate(dimensions):
        enum_ref = dimension_enumeration(dimension, roots)
        codelists = resolve_artifacts(roots, "Codelist", enum_ref)[:1] if enum_ref else []
        codes = {e.attrib["id"]: english_name(e) for c in codelists for e in c if local_name(e.tag) == "Code" and e.get("id")}
        if i == 0:
            country_codes = codes
        elif codes:
            if expected[i] not in codes:
                raise IMFError(f"Key {expected[i]} is not allowed at dimension {i + 1} ({dimension.get('id')}) of IMF {flow}.")
            descriptions[f"{dimension.get('id')}={expected[i]}"] = codes[expected[i]]
        else:
            # Do not guess a dimension order when the codes cannot be checked.
            raise IMFError(f"The codelist for IMF {flow} dimension {dimension.get('id')} is unavailable; series-key validation cannot complete.")
    return FlowInfo(flow, version, tuple(e.attrib["id"] for e in dimensions), country_codes,
                    descriptions, f"{API_BASE}/structure/dataflow/IMF.STA/{flow}/+?references=all",
                    f"{dsd_ref[0]}/{dsd_ref[1]}/{dsds[0].get('version')}")


def discover_flow(flow: str) -> FlowInfo:
    # '+' is SDMX 3.0's latest-version selector. The returned concrete version,
    # never an assumed version number, is used for the subsequent data request.
    root = structure(f"/structure/dataflow/IMF.STA/{flow}/+?references=all")
    matching = [e for e in elements(root, "Dataflow") if e.get("id") == flow]
    if len(matching) == 1:
        nodes = elements(matching[0], "Structure")
        ref = reference(nodes[0]) if nodes else None
        if ref and not any(e.get("id") == ref[1] for e in elements(root, "DataStructure")):
            path = "/".join(quote(part, safe="+") for part in ref)
            dsd_root = structure(f"/structure/datastructure/{path}?references=all")
            return parse_flow_metadata(flow, root, dsd_root)
    return parse_flow_metadata(flow, root)


def fallback_countries() -> dict[str, str]:
    result = {}
    for code in [*MEMBER_CODES, "HKG", "MAC", "TWN", "PRI", "WBG"]:
        country = pycountry.countries.get(alpha_3=code)
        result[code] = COUNTRY_OVERRIDES.get(code, getattr(country, "name", code))
    result["G163"] = "Euro area"
    return result


def country_catalog(flows: dict[str, FlowInfo]) -> tuple[dict[str, str], str]:
    """Filter metadata to actual countries/territories, plus the explicit euro area."""
    result = {}
    for info in flows.values():
        for code, name in info.countries.items():
            if pycountry.countries.get(alpha_3=code) or code in ("UVK", "XKX", "WBG", "G163"):
                result[code] = COUNTRY_OVERRIDES.get(code, name or code)
    if result:
        result["G163"] = "Euro area"
        return result, "Live IMF reference-area codelists; unrelated aggregates excluded. Availability varies by series."
    return fallback_countries(), "Metadata unavailable: fallback roster of 191 IMF members and 5 separately reported economies, plus Euro area. This is a country selector, not a claim of series availability."


def parse_month(value: object) -> pd.Timestamp:
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})-(?:M)?(\d{1,2})", text, re.I)
    if match:
        year, month = map(int, match.groups())
    else:
        match = re.fullmatch(r"(\d{4})-([A-Za-z]{3})", text)
        if not match:
            raise IMFError(f"Unsupported monthly TIME_PERIOD: {text[:40]!r}.")
        year = int(match.group(1))
        month_names = {name.upper(): i for i, name in enumerate(calendar.month_abbr) if i}
        month = month_names.get(match.group(2).upper(), 0)
    if not 1 <= month <= 12 or not 1 <= year <= 9999:
        raise IMFError(f"Invalid monthly TIME_PERIOD: {text[:40]!r}.")
    return pd.Timestamp(year=year, month=month, day=1)


def finite_positive(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.map(lambda x: pd.notna(x) and math.isfinite(x) and x > 0).astype(bool)


def parse_observations(text: str, code: str, info: FlowInfo) -> tuple[pd.Series, list[str]]:
    try:
        frame = pd.read_csv(io.StringIO(text.lstrip("\ufeff")), dtype=str, keep_default_na=False)
    except (pd.errors.ParserError, pd.errors.EmptyDataError):
        raise IMFError("The IMF response is not valid SDMX CSV.") from None
    frame.columns = [str(c).strip().upper() for c in frame.columns]
    if frame.columns.duplicated().any():
        raise IMFError("IMF CSV has duplicate column names.")
    required = {"TIME_PERIOD", "OBS_VALUE", *(d.upper() for d in info.dimensions)}
    if not required.issubset(frame.columns):
        raise IMFError(f"Unexpected IMF CSV schema; missing columns: {', '.join(sorted(required - set(frame.columns)))}. Expected monthly SDMX observations.")
    if frame.empty:
        raise NoObservations("IMF CSV contains no monthly observations.")
    if frame["TIME_PERIOD"].str.strip().eq("").all() and frame["OBS_VALUE"].str.strip().eq("").all():
        # The live G163 CPI endpoint returns HTTP 200 with one empty dataflow
        # row. This is missing data, not a different country or a usable series.
        raise NoObservations("IMF returned no observations for this series (an empty SDMX dataflow row). Try Nominal mode or another country pair.")
    for dimension, expected in zip(info.dimensions, (code, *KEY_TAILS[info.flow]), strict=True):
        if not frame[dimension.upper()].str.strip().eq(expected).all():
            raise IMFError(f"IMF returned a different series at dimension {dimension}; refusing to mix series.")
    months = frame["TIME_PERIOD"].map(parse_month)
    values = pd.to_numeric(frame["OBS_VALUE"], errors="coerce")
    # IMF's SCALE attribute references CL_UNIT_MULT in its live concept scheme.
    multiplier = "SCALE" if "SCALE" in frame else "UNIT_MULT"
    if multiplier in frame:
        mult = pd.to_numeric(frame[multiplier].replace("", "0"), errors="coerce")
        if mult.isna().any() or not mult.between(-12, 12).all() or not mult.mod(1).eq(0).all():
            raise IMFError(f"Invalid or unsupported {multiplier} in IMF CSV.")
        values = values * mult.map(lambda x: 10.0**x)
    # Only genuinely numeric finite data are accepted. Zero/negative values
    # remain visible in raw CSV columns and are excluded from calculations.
    values = values.where(values.map(lambda x: pd.isna(x) or math.isfinite(x)))
    observations = pd.DataFrame({"month": months, "value": values})
    issues = []
    if not finite_positive(values).all():
        issues.append(f"{int((~finite_positive(values)).sum())} nonnumeric, missing, non-finite, zero or negative source observation(s); excluded from calculations.")
    resolved = {}
    duplicates = conflicts = 0
    for month, group in observations.groupby("month", sort=True):
        distinct = group["value"].drop_duplicates()
        if len(group) > 1:
            duplicates += 1
        if len(distinct) > 1:
            resolved[month] = float("nan")
            conflicts += 1
        else:
            resolved[month] = group["value"].iloc[0]
    if duplicates:
        issues.append(f"{duplicates} duplicate month(s): identical repeats collapsed; {conflicts} conflicting month(s) set to missing.")
    result = pd.Series(resolved, dtype=float).sort_index()
    result.index = pd.DatetimeIndex(result.index, name="month")
    result = result.loc[(result.index >= FIRST_MONTH) & (result.index <= utc_month())]
    if code == "G163" and info.flow == "ER":
        if (result.index < EURO_START).any():
            issues.append("Euro area exchange-rate observations before 1999-01 excluded: no pre-euro currency splice is allowed.")
        result = result.loc[result.index >= EURO_START]
    if result.empty:
        raise NoObservations("No monthly observations remain in the supported historical window.")
    return result, issues


def fetch_series(code: str, info: FlowInfo) -> SeriesResult:
    result = SeriesResult(code, info.flow)
    path = f"/data/dataflow/IMF.STA/{info.flow}/{quote(info.version, safe='')}/{result.key}"
    params = {"c[TIME_PERIOD]": f"ge:1976-01+le:{utc_month():%Y-%m}"}
    result.url = requests.Request("GET", API_BASE + path, params=params).prepare().url or ""
    result.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        response = request_imf(path, "text/csv;version=2.0", params)
        result.values, result.issues = parse_observations(response.text, code, info)
        if not finite_positive(result.values).any():
            raise IMFError("Source rows were returned but none are numeric, finite and positive. Invalid observations are not filled or replaced by an accounting identity.")
    except NoObservations as exc:
        if code == "USA" and info.flow == "ER":
            result.values = pd.Series(1.0, index=pd.date_range(FIRST_MONTH, utc_month(), freq="MS"))
            result.identity = True
            result.issues.append("IMF USA FX series unavailable: using the accounting identity 1 USD per USD, not an observed or imputed rate.")
        else:
            result.error = str(exc)
    except IMFError as exc:
        result.error = str(exc)
    return result


def load_flows() -> tuple[dict[str, FlowInfo], list[str]]:
    flows, errors = {}, []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {flow: pool.submit(discover_flow, flow) for flow in KEY_TAILS}
        for flow, future in futures.items():
            try:
                flows[flow] = future.result()
            except IMFError as exc:
                errors.append(f"{flow} metadata: {exc}")
    return flows, errors


def load_pair(code1: str, code2: str, flows: dict[str, FlowInfo]) -> dict[str, SeriesResult]:
    jobs = {"P1_CPI": (code1, "CPI"), "P2_CPI": (code2, "CPI"),
            "E1_local_per_USD": (code1, "ER"), "E2_local_per_USD": (code2, "ER")}
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {column: pool.submit(fetch_series, code, flows[flow])
                   for column, (code, flow) in jobs.items() if flow in flows}
        for column, (code, flow) in jobs.items():
            results[column] = (futures[column].result() if column in futures else
                               SeriesResult(code, flow, error=f"{flow} metadata could not be verified; data request was not attempted."))
    return results


def calculate_sample(raw: dict[str, pd.Series], code1: str, code2: str,
                     name1: str, name2: str, start: object, end: object) -> pd.DataFrame:
    start, end = parse_month(str(start)[:7]), parse_month(str(end)[:7])
    if start < FIRST_MONTH:
        raise ValueError("The earliest supported sample month is January 1976.")
    if start > end:
        raise ValueError("Start month must be on or before end month.")
    if code1 == code2:
        raise ValueError("Choose two different countries.")
    frame = pd.DataFrame(index=pd.date_range(start, end, freq="MS", name="month"))
    for column in ("P1_CPI", "P2_CPI", "E1_local_per_USD", "E2_local_per_USD"):
        source = raw.get(column, pd.Series(dtype=float))
        if not source.index.is_unique:
            raise ValueError(f"Duplicate months in {column}; resolve source conflicts before alignment.")
        frame[column] = pd.to_numeric(source.reindex(frame.index), errors="coerce")
    fx_ok = finite_positive(frame["E1_local_per_USD"]) & finite_positive(frame["E2_local_per_USD"])
    frame["S_country2_per_country1"] = (frame["E2_local_per_USD"] / frame["E1_local_per_USD"]).where(fx_ok)
    frame["S_country2_per_country1"] = frame["S_country2_per_country1"].where(finite_positive(frame["S_country2_per_country1"]))
    valid = finite_positive(frame["S_country2_per_country1"]) & finite_positive(frame["P1_CPI"]) & finite_positive(frame["P2_CPI"])
    rer = (frame["S_country2_per_country1"] * (frame["P1_CPI"] / frame["P2_CPI"])).where(valid)
    valid &= finite_positive(rer)
    frame["RER"] = rer.where(valid)
    # Dividing by count before summing avoids overflow in the arithmetic mean.
    count = int(valid.sum())
    mean = (frame.loc[valid, "RER"] / count).sum() if count else float("nan")
    frame["RER_star_sample_mean"] = mean
    frame["Real_Index"] = (frame["RER"] / mean * 100) if count else float("nan")
    frame["valid_RER_month"] = valid
    frame.insert(0, "country_2_code", code2)
    frame.insert(0, "country_1_code", code1)
    frame.insert(0, "country_2", name2)
    frame.insert(0, "country_1", name1)
    return frame


def export_csv(frame: pd.DataFrame) -> bytes:
    table = frame.reset_index()
    table["month"] = table["month"].dt.strftime("%Y-%m")
    return table.to_csv(index=False, na_rep="", float_format="%.12g").encode("utf-8")


def make_chart(frame: pd.DataFrame, mode: str, name1: str, name2: str) -> go.Figure:
    both = mode == "Both"
    figure = make_subplots(specs=[[{"secondary_y": both}]])
    if mode in ("Real", "Both") and frame["Real_Index"].notna().any():
        figure.add_trace(go.Scatter(x=frame.index, y=frame["Real_Index"], name="Real index",
                                   mode="lines+markers" if frame["Real_Index"].notna().sum() == 1 else "lines", connectgaps=False, line={"color": TEAL, "width": 2.7},
                                   hovertemplate="%{x|%b %Y}<br>Real index: %{y:.3f}<br>Sample average = 100<extra></extra>"), secondary_y=False)
        figure.add_hline(y=100, line_dash="dot", line_color="#90a5ac", secondary_y=False)
    if mode in ("Nominal", "Both") and frame["S_country2_per_country1"].notna().any():
        figure.add_trace(go.Scatter(x=frame.index, y=frame["S_country2_per_country1"], name="Nominal exchange rate",
                                   mode="lines+markers" if frame["S_country2_per_country1"].notna().sum() == 1 else "lines", connectgaps=False, line={"color": ORANGE, "width": 2.4},
                                   hovertemplate=f"%{{x|%b %Y}}<br>%{{y:.8g}} {name2} currency / {name1} currency<extra>Nominal</extra>"), secondary_y=both)
    primary_label = ("Real exchange rate index (sample average = 100)" if mode != "Nominal"
                     else f"{name2} currency per {name1} currency")
    figure.update_yaxes(title_text=primary_label, secondary_y=False, zeroline=False, gridcolor="#edf1f3")
    if both:
        figure.update_yaxes(title_text=f"{name2} currency per {name1} currency", secondary_y=True, showgrid=False, zeroline=False)
    # A single-month sample still gets a visible axis range around that month.
    figure.update_xaxes(title_text="Month", range=[frame.index.min(), frame.index.max() + pd.offsets.MonthEnd(1)], showgrid=False)
    figure.update_layout(template="plotly_white", height=485, margin={"l": 16, "r": 16, "t": 28, "b": 12},
                         hovermode="x unified", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                         font={"family": "Arial, sans-serif", "color": "#233b49"},
                         legend={"orientation": "h", "y": 1.12, "x": 0}, dragmode="zoom")
    return figure


def coverage_table(results: dict[str, SeriesResult], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for label, result in results.items():
        valid = result.values.loc[finite_positive(result.values)]
        selected = valid.loc[(valid.index >= start) & (valid.index <= end)] if len(valid) else valid
        rows.append({"Input": label, "Series key": result.key,
                     "First observed": valid.index.min().strftime("%Y-%m") if len(valid) and not result.identity else "—",
                     "Last observed": valid.index.max().strftime("%Y-%m") if len(valid) and not result.identity else "—",
                     "Usable selected months": len(selected),
                     "Source": "USD accounting identity" if result.identity else "IMF"})
    return pd.DataFrame(rows)


def methodology(flows: dict[str, FlowInfo], results: dict[str, SeriesResult], country_note: str) -> None:
    with st.expander("Methodology & source details"):
        st.markdown("""
**A consistent direction.** Let E1 and E2 be local-currency units per USD,
and P1 and P2 be CPI index levels. The nominal rate is **S = E2 / E1**:
Country 2 currency units per one unit of Country 1 currency.

**A sample-relative real index.** RER = S × P1 / P2. RER★ is the equal-weight
arithmetic mean of RER across valid months in your selected inclusive window.
Real Index = 100 × RER / RER★. Its sample average is 100; this is **not a
universal purchasing-power-parity level**. Changing the window recalculates
every index value. The nominal rate retains its actual currency units.

**Gaps remain gaps.** All four inputs must be numeric, finite and positive
for a valid RER month. Missing CPI does not remove independently available
nominal rates. Identical duplicates are collapsed; conflicting duplicates
are excluded. No interpolation, forward filling or zero replacement is used.
Euro area FX starts no earlier than January 1999; historical national currencies
are never spliced into G163. A missing USA FX series can use 1 USD/USD only
as an explicitly identified accounting identity, not as an observation.

**Source.** IMF Statistics Department, Consumer Price Index (CPI) and Exchange
Rates (ER), via the [IMF SDMX 3.0 API](https://data.imf.org/en/Resource-Pages/IMF-API).
CPI key: `{country}.CPI._T.IX.M` (index levels). FX key:
`{country}.XDC_USD.PA_RT.M` (monthly period-average local currency per USD).
Metadata and ordered codelists are verified before data requests; schema
changes stop that flow. Period filter: `c[TIME_PERIOD]=ge:1976-01+le:YYYY-MM`.
The latest selector `+` is resolved to a concrete dataflow version.

**Refresh & limitations.** Observations are cached for 1 hour, metadata for
24 hours (failed metadata checks for 5 minutes). Refresh source data clears
these caches. Requests use TLS verification, connect/read timeouts of 6/35
seconds, and up to 2 retries for connection failures, 429 and transient 5xx.
CPI base revisions, currency redenominations and changing currency regimes
can affect interpretation. Gaps and revisions can change the sample average.
""")
        st.caption(country_note)
        for flow, info in flows.items():
            st.write(f"**IMF.STA/{flow}/{info.version}** · ordered dimensions: {', '.join(info.dimensions)}")
            st.caption(f"DSD: {info.dsd_reference}")
            st.json(info.descriptions, expanded=False)
            st.code(info.metadata_url, language=None)
        for result in results.values():
            st.markdown(f"**{result.key}**" + (" · USD accounting identity" if result.identity else ""))
            if result.url:
                st.code(result.url, language=None)
                st.caption(f"Requested at {result.fetched_at} (UTC); cached responses may be reused.")
        st.caption("CSV includes every selected calendar month, raw inputs, nominal S, RER, sample mean, Real_Index, validity, and identity provenance. Missing values are blank.")


def main() -> None:
    st.set_page_config(page_title="IMF Real Exchange Rate Explorer", page_icon="↔", layout="wide")
    st.markdown("""<style>
    .stApp {background: #f8fafb;}
    .block-container {max-width: 1380px; padding-top: 2.7rem;}
    [data-testid="stSidebar"] {background: #eaf1f3;}
    h1,h2,h3 {letter-spacing: -.035em; color: #163a49;}
    [data-testid="stMetric"] {background: white; border: 1px solid #e1e9ec;
        border-radius: 12px; padding: 1.05rem 1.3rem;}
    [data-testid="stMetricValue"] {color: #163a49; font-size: 1.85rem;}
    .eyebrow {color: #087f8c; font-size: .75rem; font-weight: 700;
        letter-spacing: .15em; text-transform: uppercase; margin-bottom: .7rem;}
    .subtitle {color: #526b78; font-size: 1.05rem; max-width: 850px;}
    </style>""", unsafe_allow_html=True)
    st.markdown('<div class="eyebrow">IMF data · Monthly perspectives</div>', unsafe_allow_html=True)
    st.title("Real Exchange Rate Explorer")
    st.markdown('<p class="subtitle">Explore how exchange rates and consumer prices move together. Compare two economies, choose a window, and take the data with you.</p>', unsafe_allow_html=True)

    # Defining cached wrappers here keeps CLI tests independent of a Streamlit runtime.
    # Successful metadata lives longer; errors in discovery are never treated as validation.
    cached_discovery = st.cache_data(ttl=METADATA_TTL, show_spinner=False)(discover_flow)

    def metadata_snapshot():
        # The short cache stores failures, while successful flow discovery has a 24h TTL.
        resolved, errors = {}, []
        for flow in KEY_TAILS:
            try:
                resolved[flow] = cached_discovery(flow)
            except IMFError as exc:
                errors.append(f"{flow} metadata: {exc}")
        return resolved, errors

    cached_metadata = st.cache_data(ttl=300, show_spinner=False)(metadata_snapshot)
    cached_pair = st.cache_data(ttl=DATA_TTL, show_spinner=False)(load_pair)
    with st.sidebar:
        st.markdown("### Your comparison")
        st.caption("Monthly data · IMF Statistics Department")
        if st.button("Refresh source data", width="stretch"):
            st.cache_data.clear()
            st.rerun()
    with st.spinner("Checking IMF metadata and country codelists…"):
        flows, metadata_errors = cached_metadata()
    catalog, country_note = country_catalog(flows)
    codes = sorted(catalog, key=lambda c: catalog[c])
    with st.sidebar:
        code1 = st.selectbox("Country 1", codes, index=codes.index("USA") if "USA" in codes else 0,
                             format_func=lambda c: f"{catalog[c]} ({c})")
        code2 = st.selectbox("Country 2", codes, index=codes.index("JPN") if "JPN" in codes else min(1, len(codes) - 1),
                             format_func=lambda c: f"{catalog[c]} ({c})")
        st.caption("Rates are expressed as Country 2 currency per 1 unit of Country 1 currency.")
        mode = st.radio("Chart", ["Real", "Nominal", "Both"], index=2, horizontal=True)
    if code1 == code2:
        st.error("Choose two different countries to create a comparison.")
        methodology(flows, {}, country_note)
        return
    name1, name2 = catalog[code1], catalog[code2]
    st.subheader(f"{name1} → {name2}")
    st.caption(f"Nominal direction: units of {name2} currency per 1 unit of {name1} currency.")
    with st.spinner("Retrieving monthly CPI and period-average exchange rates…"):
        results = cached_pair(code1, code2, flows)
    errors = list(metadata_errors)
    for result in results.values():
        if result.error and result.flow in flows:
            errors.append(f"{result.key}: {result.error}")
    if errors:
        st.warning("Some IMF inputs are unavailable. No alternative provider or invented observations are used.")
        with st.expander("Source access & missing series", expanded=True):
            for error in dict.fromkeys(errors):
                st.write(error)
    observed = [r.values.loc[finite_positive(r.values)] for r in results.values() if not r.identity]
    observed = [s for s in observed if not s.empty]
    if not observed:
        st.info("No usable IMF observations were received. Check source access above, then refresh. Sample bounds cannot be established until at least one observed series is available.")
        st.caption(country_note)
        methodology(flows, results, country_note)
        return
    latest = min(max(s.index.max() for s in observed), utc_month())
    options = list(pd.date_range(FIRST_MONTH, latest, freq="MS"))
    default_start = max(FIRST_MONTH, latest - pd.DateOffset(months=119))
    # Reset saved range only when pair/latest changes, so reruns preserve each edit.
    scope = (code1, code2, latest.strftime("%Y-%m"))
    if st.session_state.get("sample_scope") != scope:
        st.session_state["sample_scope"] = scope
        st.session_state["start_month"] = default_start
        st.session_state["end_month"] = latest
    with st.sidebar:
        st.divider()
        st.markdown("### Sample window")
        start = st.selectbox("Start month", options, key="start_month", format_func=lambda t: t.strftime("%b %Y"))
        end = st.selectbox("End month", options, key="end_month", format_func=lambda t: t.strftime("%b %Y"))
        st.caption(f"Inclusive months · latest available input: {latest:%b %Y}")
        st.caption("The real index is recalculated over valid months in this window.")
    if start > end:
        st.error("Start month must be on or before end month. Adjust either month in the sidebar.")
        methodology(flows, results, country_note)
        return
    raw = {label: result.values for label, result in results.items()}
    frame = calculate_sample(raw, code1, code2, name1, name2, start, end)
    frame["E1_USD_identity"] = results["E1_local_per_USD"].identity
    frame["E2_USD_identity"] = results["E2_local_per_USD"].identity
    count = int(frame["valid_RER_month"].sum())
    nominal_count = int(frame["S_country2_per_country1"].notna().sum())
    a, b, c = st.columns(3)
    a.metric("Valid real-rate months", f"{count:,} / {len(frame):,}")
    b.metric("Nominal-rate months", f"{nominal_count:,} / {len(frame):,}")
    c.metric("Real index sample average", "100.00" if count else "Unavailable")
    st.caption(f"Selected sample: {start:%b %Y} – {end:%b %Y} · equal weight per valid month. 100 is this sample’s average, not a purchasing-power-parity benchmark.")
    for result in results.values():
        for issue in result.issues:
            st.caption(f"{result.key}: {issue}")
    if not count:
        st.warning("No valid real-rate months: all four inputs must overlap and be finite and positive. Try another period or pair. Any independently available nominal rate is still shown in Nominal or Both mode.")
    elif count < len(frame):
        st.caption(f"{len(frame) - count:,} incomplete or invalid month(s) are excluded from the real-index mean and retained in the CSV. Chart lines break at gaps.")
    figure = make_chart(frame, mode, name1, name2)
    if figure.data:
        st.plotly_chart(figure, width="stretch", config={"displaylogo": False})
    else:
        st.info("No values for this chart in the selected sample. Try Nominal/Both, another period, or another pair.")
    st.download_button("Download CSV", export_csv(frame),
                       file_name=f"imf_{code1}_{code2}_{start:%Y-%m}_{end:%Y-%m}.csv",
                       mime="text/csv; charset=utf-8", type="primary", width="stretch")
    data_tab, coverage_tab = st.tabs(["Monthly data", "Data coverage"])
    with data_tab:
        display = frame.copy()
        display.index = display.index.strftime("%Y-%m")
        st.dataframe(display, width="stretch")
    with coverage_tab:
        st.dataframe(coverage_table(results, start, end), hide_index=True, width="stretch")
        st.caption("First/last observed dates describe the retrieved source history from January 1976, not guaranteed continuous coverage. Identity values are never counted as observed history.")
    methodology(flows, results, country_note)
    st.caption("IMF Real Exchange Rate Explorer · CPI & monthly period-average FX · No filling or interpolation")


class ExplorerChecks(unittest.TestCase):
    """Synthetic fixtures are confined to tests, never offered as economic data."""

    def setUp(self):
        self.months = pd.date_range("2020-01-01", periods=3, freq="MS")

    def raw(self, p1=(1, 1, 1), p2=(1, 1, 1), e1=(1, 1, 1), e2=(2, 4, 6)):
        return {name: pd.Series(values, index=self.months, dtype=float)
                for name, values in zip(("P1_CPI", "P2_CPI", "E1_local_per_USD", "E2_local_per_USD"), (p1, p2, e1, e2), strict=True)}

    def sample(self, raw=None, start="2020-01", end="2020-03"):
        return calculate_sample(raw or self.raw(), "USA", "JPN", "United States", "Japan", start, end)

    def info(self, flow="ER"):
        return FlowInfo(flow, "1.0", ("REF_AREA", "INDICATOR", "UNIT_MEASURE", "FREQUENCY") if flow == "ER" else
                        ("COUNTRY", "INDEX_TYPE", "COICOP_1999", "TYPE_OF_TRANSFORMATION", "FREQUENCY"), {}, {}, "", "")

    def csv(self, rows, code="JPN"):
        return "REF_AREA,INDICATOR,UNIT_MEASURE,FREQUENCY,TIME_PERIOD,OBS_VALUE\n" + "\n".join(f"{code},XDC_USD,PA_RT,M,{period},{value}" for period, value in rows)

    def test_direction_and_rer(self):
        result = self.sample(self.raw(p1=(120,)*3, p2=(80,)*3, e1=(2,)*3, e2=(100,)*3))
        self.assertTrue(result["S_country2_per_country1"].eq(50).all())
        self.assertTrue(result["RER"].eq(75).all())

    def test_normalization(self):
        result = self.sample()
        self.assertTrue(result["RER_star_sample_mean"].eq(4).all())
        self.assertEqual(result["Real_Index"].tolist(), [50, 100, 150])
        self.assertAlmostEqual(result["Real_Index"].mean(), 100)

    def test_changed_window(self):
        result = self.sample(end="2020-02")
        self.assertTrue(result["RER_star_sample_mean"].eq(3).all())
        self.assertAlmostEqual(result["Real_Index"].iloc[0], 200/3)
        self.assertAlmostEqual(result["Real_Index"].iloc[1], 400/3)

    def test_reciprocal(self):
        raw = self.raw(e1=(2, 4, 8), e2=(100, 120, 140))
        reverse = {"P1_CPI": raw["P2_CPI"], "P2_CPI": raw["P1_CPI"],
                   "E1_local_per_USD": raw["E2_local_per_USD"], "E2_local_per_USD": raw["E1_local_per_USD"]}
        self.assertTrue((self.sample(raw)["S_country2_per_country1"] * self.sample(reverse)["S_country2_per_country1"]).sub(1).abs().lt(1e-12).all())

    def test_missing_cpi_retains_nominal(self):
        result = self.sample(self.raw(p1=(1, float("nan"), 1)))
        self.assertFalse(result["valid_RER_month"].iloc[1])
        self.assertEqual(result["S_country2_per_country1"].iloc[1], 4)
        self.assertEqual(result["RER_star_sample_mean"].iloc[0], 4)

    def test_csv_and_chart_selected_window(self):
        result = self.sample(self.raw(p1=(1, float("nan"), 1)), start="2020-02")
        downloaded = pd.read_csv(io.BytesIO(export_csv(result)))
        self.assertEqual(downloaded["month"].tolist(), ["2020-02", "2020-03"])
        self.assertTrue(pd.isna(downloaded["P1_CPI"].iloc[0]))
        self.assertEqual(downloaded["S_country2_per_country1"].tolist(), [4, 6])
        figure = make_chart(result, "Both", "United States", "Japan")
        self.assertEqual(len(figure.data), 2)
        self.assertEqual(list(figure.data[0].x), list(result.index))
        self.assertEqual(list(figure.data[1].y), [4, 6])
        self.assertEqual(figure.data[1].yaxis, "y2")
        self.assertEqual(figure.layout.xaxis.range[0], result.index.min())
        self.assertAlmostEqual(result["Real_Index"].mean(), 100)

    def test_nominal_is_not_rebased(self):
        result = self.sample()
        figure = make_chart(result, "Nominal", "A", "B")
        self.assertEqual(len(figure.data), 1)
        self.assertEqual(list(figure.data[0].y), [2, 4, 6])

    def test_no_valid_real_months(self):
        result = self.sample(self.raw(p1=(float("nan"),)*3))
        self.assertEqual(int(result["valid_RER_month"].sum()), 0)
        self.assertTrue(result["RER_star_sample_mean"].isna().all())
        self.assertTrue(result["Real_Index"].isna().all())
        self.assertEqual(len(make_chart(result, "Both", "A", "B").data), 1)

    def test_invalid_input_values(self):
        for bad in (0, -1, float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=bad):
                result = self.sample(self.raw(e2=(2, bad, 6)))
                self.assertFalse(result["valid_RER_month"].iloc[1])
                self.assertTrue(pd.isna(result["S_country2_per_country1"].iloc[1]))

    def test_month_formats(self):
        for value in ("2020-01", "2020-M01", "2020-Jan", "2020-JAN"):
            self.assertEqual(parse_month(value), self.months[0])
        self.assertEqual(parse_month("2020-M12").month, 12)
        for value in ("2020-M00", "2020-M13", "2020-Q1", "not-a-date"):
            with self.assertRaises(IMFError):
                parse_month(value)

    def test_csv_parser_duplicates(self):
        text = self.csv([("2020-01", 2), ("2020-M01", 2), ("2020-02", 4), ("2020-Feb", 5)])
        values, issues = parse_observations(text, "JPN", self.info())
        self.assertEqual(values.iloc[0], 2)
        self.assertTrue(pd.isna(values.iloc[1]))
        self.assertIn("1 conflicting", issues[0])

    def test_wrong_series_and_schema(self):
        for text in ("<html>Denied</html>", self.csv([("2020-01", 2)], "USA"),
                     self.csv([("2020-01", 2)]).replace("PA_RT", "EOP_RT")):
            with self.assertRaises(IMFError):
                parse_observations(text, "JPN", self.info())

    def test_no_pre_euro_fx(self):
        values, issues = parse_observations(self.csv([("1998-12", 1.1), ("1999-01", 1.2)], "G163"), "G163", self.info())
        self.assertEqual(values.index.tolist(), [EURO_START])
        self.assertTrue(issues)

    def test_unit_multiplier(self):
        text = self.csv([("2020-01", 2)]).replace("OBS_VALUE\n", "OBS_VALUE,UNIT_MULT\n") + ",2"
        values, _ = parse_observations(text, "JPN", self.info())
        self.assertEqual(values.iloc[0], 200)
        values, _ = parse_observations(text.replace("UNIT_MULT", "SCALE"), "JPN", self.info())
        self.assertEqual(values.iloc[0], 200)

    def test_empty_sdmx_dataflow_row(self):
        text = "STRUCTURE[;],STRUCTURE_ID,ACTION,COUNTRY,INDEX_TYPE,COICOP_1999,TYPE_OF_TRANSFORMATION,FREQUENCY,TIME_PERIOD,OBS_VALUE\ndataflow,IMF.STA:CPI(5.0.0),R,,,,,,,\n"
        with self.assertRaisesRegex(NoObservations, "empty SDMX dataflow row"):
            parse_observations(text, "G163", self.info("CPI"))

    def test_empty_series_coverage(self):
        results = {"P2_CPI": SeriesResult("G163", "CPI", error="No observations")}
        summary = coverage_table(results, self.months[0], self.months[-1])
        self.assertEqual(summary["Usable selected months"].iloc[0], 0)
        self.assertEqual(summary["First observed"].iloc[0], "—")

    def test_inherited_concept_codelists_and_version_ranges(self):
        dimension = ET.fromstring('<Dimension id="FREQUENCY"><ConceptIdentity>urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=IMF:CS_SYSTEM(1.0+.0).FREQ</ConceptIdentity></Dimension>')
        root = ET.fromstring('<Structure><ConceptScheme agencyID="IMF" id="CS_SYSTEM" version="1.0.2"><Concept id="FREQ"><CoreRepresentation><Enumeration>urn:sdmx:org.sdmx.infomodel.codelist.Codelist=IMF:CL_FREQ(1.0+.0)</Enumeration></CoreRepresentation></Concept></ConceptScheme></Structure>')
        self.assertEqual(dimension_enumeration(dimension, [root]), ("IMF", "CL_FREQ", "1.0+.0"))
        self.assertTrue(version_matches("1.0.2", "1.0+.0"))
        self.assertTrue(version_matches("1.2.0", "1.0+.0"))
        self.assertFalse(version_matches("2.0.0", "1.0+.0"))
        self.assertFalse(version_matches("1.0.0", "1.1+.0"))

    def test_complete_calendar_and_input_validation(self):
        result = self.sample(start="2019-12")
        self.assertEqual(len(result), 4)
        self.assertTrue(pd.isna(result["RER"].iloc[0]))
        with self.assertRaises(ValueError):
            self.sample(start="2020-03", end="2020-01")
        with self.assertRaises(ValueError):
            self.sample(start="1975-12")

    def test_country_roster(self):
        self.assertEqual(len(set(MEMBER_CODES)), 191)
        self.assertEqual(len(fallback_countries()), 197)
        self.assertIn("G163", fallback_countries())
        self.assertNotIn("EUR", fallback_countries())

    def test_usd_identity_only_for_absent_series(self):
        with patch(__name__ + ".request_imf", side_effect=NoObservations("No series")):
            usa = fetch_series("USA", self.info())
            japan = fetch_series("JPN", self.info())
        self.assertTrue(usa.identity)
        self.assertTrue(usa.values.eq(1).all())
        self.assertFalse(japan.identity)
        self.assertIsNotNone(japan.error)
        with patch(__name__ + ".request_imf", side_effect=IMFError("Access denied")):
            denied = fetch_series("USA", self.info())
        self.assertFalse(denied.identity)
        self.assertIsNotNone(denied.error)
        response = requests.Response()
        response.status_code = 200
        response._content = self.csv([("2020-01", 0)], "USA").encode()
        with patch(__name__ + ".request_imf", return_value=response):
            invalid = fetch_series("USA", self.info())
        self.assertFalse(invalid.identity)
        self.assertIsNotNone(invalid.error)

    def test_single_month_is_visible(self):
        result = self.sample(start="2020-02", end="2020-02")
        self.assertEqual(result["Real_Index"].iloc[0], 100)
        for mode in ("Real", "Nominal", "Both"):
            chart = make_chart(result, mode, "A", "B")
            self.assertTrue(all(trace.mode == "lines+markers" for trace in chart.data))

    def test_http_retry_and_access_errors(self):
        limited, ok = requests.Response(), requests.Response()
        limited.status_code, limited._content = 429, b"rate limit"
        ok.status_code, ok._content = 200, b"CSV"
        with patch("requests.get", side_effect=[limited, ok]) as mocked, patch("time.sleep"):
            self.assertEqual(request_imf("/data/test", "text/csv").status_code, 200)
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(mocked.call_args.kwargs["timeout"], (6, 35))
        denied = requests.Response()
        denied.status_code = 403
        with patch("requests.get", return_value=denied):
            with self.assertRaisesRegex(IMFError, "access denied"):
                request_imf("/data/test", "text/csv")

    def test_metadata_version_and_dimension_validation(self):
        # A miniature SDMX fixture tests parsing, not live IMF availability.
        dimension_xml, codelist_xml = [], []
        for position, (dimension, code) in enumerate(zip(self.info().dimensions, ("JPN", *KEY_TAILS["ER"]), strict=True), 1):
            dimension_xml.append(f'<Dimension id="{dimension}" position="{position}"><LocalRepresentation><Enumeration><Ref agencyID="IMF.STA" id="CL{position}" version="1.0"/></Enumeration></LocalRepresentation></Dimension>')
            codelist_xml.append(f'<Codelist agencyID="IMF.STA" id="CL{position}" version="1.0"><Code id="{code}"><Name xml:lang="en">{code} label</Name></Code></Codelist>')
        xml = ('<Structure><Dataflow agencyID="IMF.STA" id="ER" version="2.7.0"><Structure><Ref agencyID="IMF.STA" id="DSD_ER" version="1.0"/></Structure></Dataflow>'
               '<DataStructure agencyID="IMF.STA" id="DSD_ER" version="1.0"><DimensionList>' + ''.join(dimension_xml) + '</DimensionList></DataStructure>' + ''.join(codelist_xml) + '</Structure>')
        info = parse_flow_metadata("ER", ET.fromstring(xml))
        self.assertEqual(info.version, "2.7.0")
        self.assertEqual(info.dimensions, self.info().dimensions)
        self.assertIn("JPN", info.countries)
        with self.assertRaisesRegex(IMFError, "not allowed"):
            parse_flow_metadata("ER", ET.fromstring(xml.replace('Code id="PA_RT"', 'Code id="EOP_RT"')))
        with self.assertRaisesRegex(IMFError, "concrete version"):
            parse_flow_metadata("ER", ET.fromstring(xml.replace('version="2.7.0"', 'version="+"')))
        urn = ET.fromstring('<Structure>urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=IMF.STA:DSD_ER(1.0)</Structure>')
        self.assertEqual(reference(urn), ("IMF.STA", "DSD_ER", "1.0"))

    def test_data_request_version_key_and_filter(self):
        response = requests.Response()
        response.status_code = 200
        response._content = self.csv([("2020-01", 100)]).encode()
        with patch(__name__ + ".request_imf", return_value=response) as mocked:
            result = fetch_series("JPN", self.info())
        self.assertIsNone(result.error)
        path, accept, params = mocked.call_args.args
        self.assertEqual(path, "/data/dataflow/IMF.STA/ER/1.0/JPN.XDC_USD.PA_RT.M")
        self.assertEqual(accept, "text/csv;version=2.0")
        self.assertEqual(params["c[TIME_PERIOD]"], f"ge:1976-01+le:{utc_month():%Y-%m}")

    def test_subscription_header_and_empty_responses(self):
        response = requests.Response()
        response.status_code = 204
        with patch.dict(os.environ, {"IMF_SDMX_SUBSCRIPTION_KEY": "test-only-not-a-credential"}), patch("requests.get", return_value=response) as mocked:
            with self.assertRaises(NoObservations):
                request_imf("/data/test", "text/csv")
        self.assertEqual(mocked.call_args.kwargs["headers"]["Ocp-Apim-Subscription-Key"], "test-only-not-a-credential")


def live_smoke() -> int:
    """Read-only live checks; a denied request is a failure, never a passing mock."""
    flows, errors = load_flows()
    failed = bool(errors)
    for error in errors:
        print("FAIL", error)
    for code1, code2 in (("USA", "JPN"), ("USA", "G163")):
        results = load_pair(code1, code2, flows)
        for result in results.values():
            if result.error:
                failed = True
                print("FAIL", result.key, result.error)
            else:
                print("PASS", result.key, len(result.values), "monthly rows;", "USD identity" if result.identity else "live IMF observations")
        observed = [r.values.index.max() for r in results.values() if not r.values.empty and not r.identity]
        if not observed:
            continue
        end = min(max(observed), utc_month())
        start = max(FIRST_MONTH, end - pd.DateOffset(months=119))
        frame = calculate_sample({k: r.values for k, r in results.items()}, code1, code2, code1, code2, start, end)
        valid_count = int(frame["valid_RER_month"].sum())
        if not valid_count or not math.isclose(frame["Real_Index"].mean(), 100, abs_tol=1e-9):
            failed = True
            print("FAIL", code1, code2, "no valid normalized sample")
        else:
            print("PASS", code1, code2, valid_count, "valid months; Real_Index mean =", frame["Real_Index"].mean())
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run deterministic, offline checks")
    parser.add_argument("--live-smoke", action="store_true", help="Verify live USA/JPN and USA/G163 IMF data")
    args = parser.parse_args()
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ExplorerChecks)
        sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
    if args.live_smoke:
        sys.exit(live_smoke())
    main()
