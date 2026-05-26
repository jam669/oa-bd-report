"""
OA BD Weekly Report Updater
============================
Fetches HubSpot data for the current report week and patches bd-weekly-report.html.

Run manually:
    python update_weekly_report.py

Discover HubSpot contact properties (run once to find your property names):
    python update_weekly_report.py --discover

Requirements:
    pip install requests

Environment variable required:
    HUBSPOT_TOKEN   Your HubSpot Private App token
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────

HUBSPOT_TOKEN  = os.environ.get("HUBSPOT_TOKEN", "")
BASE_URL       = "https://api.hubapi.com"
HEADERS        = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}

BD_PIPELINE_ID = "68218158"
PORTAL_ID      = "44390857"
MANILA_TZ      = timezone(timedelta(hours=8))

# Outbound Paid sales cycles — used to group the Paid Outbound page by Discovery Call Date.
# Update this list as new cycles begin; UI will auto-default to the cycle that contains today.
PAID_CYCLES = [
    {"name": "1st Cycle", "start": "2026-03-15", "end": "2026-04-14"},
    {"name": "2nd Cycle", "start": "2026-04-15", "end": "2026-05-31"},
]

HTML_FILE      = os.path.join(os.path.dirname(__file__), "bd-weekly-report.html")

# ── HubSpot contact property names (confirmed via API schema discovery) ───────
PROP_LEAD_CATEGORY = "lead_category"   # Source/type of BD contact
PROP_VALIDITY      = "lead_validity"   # Lead validity classification
PROP_LEAD_STATUS   = "hs_lead_status"  # "CONNECTED" means lead replied/engaged

# "BD Lead" is the internal value for the "OA Business Development" lead category.
# Only these contacts are counted for weekly reporting.
OA_BD_VALUE     = "BD Lead"
OA_BD_VALUES    = {OA_BD_VALUE}

# Kept for top-50 pool and paid-deals (broader BD universe)
INBOUND_VALUES  = {"BD Lead"}
OUTBOUND_VALUES = {"OA Outbound", "BizDev Outbound", "Outbound Enterprise", "Duane Test"}
SP_VALUES       = {"SP Lead", "SP Lead Enterprise"}
ALL_BD_VALUES   = INBOUND_VALUES | OUTBOUND_VALUES | SP_VALUES | {"Inbound Transfer"}

# lead_validity values
VALID_STRICT_VALUES  = {"Valid"}
VALID_NI_VALUES      = {"Valid - Not Interested", "Valid - Unreachable"}
SPAM_VALUES          = {"Invalid - Spam"}
JOBSEEKER_VALUES     = {"Invalid - Jobseeker"}
SP_INVALID_VALUES    = {"Invalid - Service Provider", "Invalid - Potential SP"}
UNQUALIFIED_VALUES   = {"Invalid - Unqualified"}

CONNECTED_VALUE = "CONNECTED"  # hs_lead_status value

# Stage IDs
STAGES = {
    "deal_created":      "132946329",
    "dc_outreach":       "244709522",
    "dc_completed":      "222405237",
    "dc_no_show":        "132946331",
    "ac_outreach":       "244520495",
    "ac_no_show":        "133003872",
    "ac_completed":      "132946333",
    "cd_main":           "1029860491",
    "cd_scheduled":      "1053002936",
    "cd_no_show":        "1053002935",
    "cd_completed":      "1053002937",
    "hiring_recruiting": "133348729",
    "closed_won":        "132946334",
    "closed_lost":       "132946335",
    "deal_unqualified":  "991351894",
}

STAGE_LABELS = {
    STAGES["dc_completed"]:      "DC Completed",
    STAGES["dc_no_show"]:        "DC No Show",
    STAGES["ac_outreach"]:       "AC Outreach",
    STAGES["ac_completed"]:      "AC Completed",
    STAGES["ac_no_show"]:        "AC No Show",
    STAGES["cd_main"]:           "CD Main",
    STAGES["cd_scheduled"]:      "CD Scheduled",
    STAGES["cd_no_show"]:        "CD No Show",
    STAGES["cd_completed"]:      "CD Completed",
    STAGES["hiring_recruiting"]:  "Hiring & Recruiting",
    STAGES["closed_won"]:        "Closed Won",
    STAGES["closed_lost"]:       "Closed Lost",
    STAGES["deal_unqualified"]:  "Deal Unqualified",
    STAGES["deal_created"]:      "Deal Created",
}

ACTIVE_STAGES = {v for k, v in STAGES.items()
                 if k not in ("closed_won", "closed_lost", "deal_unqualified")}

# Stages that indicate DC / AC was attended, or Paid Fee reached
DC_ATTENDED_STAGES = frozenset({
    STAGES["dc_completed"],
    STAGES["ac_outreach"], STAGES["ac_no_show"], STAGES["ac_completed"],
    STAGES["cd_main"], STAGES["cd_scheduled"], STAGES["cd_no_show"], STAGES["cd_completed"],
    STAGES["hiring_recruiting"], STAGES["closed_won"],
})
AC_ATTENDED_STAGES = frozenset({
    STAGES["ac_completed"],
    STAGES["cd_main"], STAGES["cd_scheduled"], STAGES["cd_no_show"], STAGES["cd_completed"],
    STAGES["hiring_recruiting"], STAGES["closed_won"],
})
PAID_FEE_STAGES = frozenset({
    STAGES["hiring_recruiting"],
    STAGES["closed_won"],
})

# ── VALIDITY / SOURCE NORMALISATION ──────────────────────────────────────────

def norm_validity(raw):
    v = (raw or "").strip()
    if v in VALID_STRICT_VALUES:  return "valid_strict"
    if v in VALID_NI_VALUES:      return "valid_ni"
    if v in SPAM_VALUES:          return "spam"
    if v in JOBSEEKER_VALUES:     return "jobseeker"
    if v in SP_INVALID_VALUES:    return "service_provider"
    if v in UNQUALIFIED_VALUES:   return "unqualified"
    return "no_validity"

def norm_source(lead_cat):
    v = (lead_cat or "").strip()
    if v in INBOUND_VALUES:  return "inbound"
    if v in OUTBOUND_VALUES: return "outbound"
    if v in SP_VALUES:       return "sp"
    return "other"

def is_connected(lead_status):
    return (lead_status or "").strip() == CONNECTED_VALUE

# ── DATE HELPERS ──────────────────────────────────────────────────────────────

def _week_range_for_anchor(anchor_dt):
    """Return (start_dt, end_dt, week_num, dates_label) for a 7-day Wed–Tue window
    ending on the Tuesday <= anchor_dt. Manila time."""
    wd        = anchor_dt.weekday()          # Mon=0, Tue=1, …, Sun=6
    days_back = (wd - 1) % 7                  # Tue=0, Wed=1, …, Mon=6
    end       = (anchor_dt - timedelta(days=days_back)).replace(
                    hour=23, minute=59, second=59, microsecond=0)
    start     = (end - timedelta(days=6)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
    week_num  = start.isocalendar()[1]

    mo = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    if start.month == end.month:
        dates_label = f"{mo[start.month-1]} {start.day}–{end.day}"
    else:
        dates_label = f"{mo[start.month-1]} {start.day}–{mo[end.month-1]} {end.day}"

    return start, end, week_num, dates_label

def current_week_range():
    """Report-week range, anchored to the most recent Tuesday in Manila time.
    Cron runs Tuesday 09:00 Manila and reports the Wed–Tue week ending that Tuesday.
    Manual mid-week runs report the most recently completed Wed–Tue week so existing
    weeks never get a partial-data overwrite and tab dates never gap."""
    return _week_range_for_anchor(datetime.now(MANILA_TZ))

def to_iso(dt):
    return dt.isoformat()

# ── HUBSPOT HELPERS ──────────────────────────────────────────────────────────

def search_contacts(filters, properties, limit=100):
    """Single contacts search call."""
    r = requests.post(
        f"{BASE_URL}/crm/v3/objects/contacts/search",
        headers=HEADERS,
        json={"filterGroups": [{"filters": filters}], "properties": properties, "limit": limit}
    )
    r.raise_for_status()
    return r.json()

def _search_one_batch(filter_groups, properties):
    """Single paginated contacts search (≤5 filter groups per HubSpot limit).
    Retries on transient errors (400/429/5xx) instead of silently dropping pages —
    HubSpot's search API returns occasional 400s under load and the old behavior
    truncated week counts."""
    import time
    results, after = [], None
    while True:
        body = {"filterGroups": filter_groups, "properties": properties, "limit": 100}
        if after:
            body["after"] = after

        last_err = None
        for attempt in range(4):
            r = requests.post(f"{BASE_URL}/crm/v3/objects/contacts/search",
                              headers=HEADERS, json=body)
            if r.ok:
                break
            last_err = (r.status_code, r.text[:200])
            # Transient: retry. 401/403 = auth → give up.
            if r.status_code in (400, 429) or 500 <= r.status_code < 600:
                time.sleep(0.5 * (2 ** attempt))
                continue
            break
        else:
            print(f"  [ERROR] contacts search exhausted retries {last_err}")
            raise RuntimeError(f"HubSpot contacts search failed after retries: {last_err}")

        if not r.ok:
            print(f"  [ERROR] contacts search {r.status_code}: {r.text[:200]}")
            raise RuntimeError(f"HubSpot contacts search failed: {r.status_code}")

        data    = r.json()
        results.extend(data.get("results", []))
        after   = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return results

def search_contacts_by_categories(cat_values, shared_filters, properties):
    """Search contacts whose multi-select lead_category contains any of cat_values + shared_filters.
    Uses CONTAINS_TOKEN because lead_category is a checkbox (multi-value) enumeration —
    EQ misses contacts that have lead_category=['BD Lead','Outbound Paid']. Batches into
    groups of 5 to stay within HubSpot's filterGroups limit."""
    seen, results = set(), []
    for i in range(0, len(cat_values), 5):
        batch = cat_values[i:i+5]
        groups = [
            {"filters": [{"propertyName": PROP_LEAD_CATEGORY, "operator": "CONTAINS_TOKEN", "value": v}] + shared_filters}
            for v in batch
        ]
        for c in _search_one_batch(groups, properties):
            if c["id"] not in seen:
                seen.add(c["id"])
                results.append(c)
    return results

def search_all_deals(filters, properties):
    """Paginate through all matching deals."""
    results, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": properties, "limit": 200}
        if after:
            body["after"] = after
        r = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=body)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return results

def get_contact_deal_info(contact_ids):
    """Batch-fetch deal associations for a list of contact IDs (auto-batches ≤100).
    Returns {contact_id: [deal_id, ...]}"""
    if not contact_ids:
        return {}
    results = {}
    for i in range(0, len(contact_ids), 100):
        inputs = [{"id": str(cid)} for cid in contact_ids[i:i+100]]
        r = requests.post(
            f"{BASE_URL}/crm/v4/associations/contacts/deals/batch/read",
            headers=HEADERS, json={"inputs": inputs}
        )
        if r.status_code not in (200, 207):
            continue
        for item in r.json().get("results", []):
            cid = str(item.get("from", {}).get("id", ""))
            results[cid] = [str(a.get("toObjectId", "")) for a in item.get("to", [])]
    return results

def get_deals_by_ids(deal_ids):
    """Fetch deal details for a list of deal IDs (auto-batches ≤100)."""
    if not deal_ids:
        return {}
    props = ["pipeline", "dealstage", "paid_recruitment_date", "dealname", "hs_lastmodifieddate", "amount"]
    result = {}
    for i in range(0, len(deal_ids), 100):
        inputs = [{"id": did} for did in deal_ids[i:i+100]]
        r = requests.post(
            f"{BASE_URL}/crm/v3/objects/deals/batch/read",
            headers=HEADERS, json={"properties": props, "inputs": inputs}
        )
        if r.status_code not in (200, 207):
            continue
        for d in r.json().get("results", []):
            result[str(d["id"])] = d.get("properties", {})
    return result

def fetch_monthly_deal_details(month_start, month_end):
    """Return BD pipeline deal details for deals whose discovery_call_date falls in [month_start, month_end].
    Each row: {id, company, leadSource, dcDate, acDate, paidDate, signedDate, stage, amount, createDate}."""
    filters = [
        {"propertyName": "pipeline",            "operator": "EQ",  "value": BD_PIPELINE_ID},
        {"propertyName": "discovery_call_date", "operator": "GTE", "value": str(_date_ms(month_start))},
        {"propertyName": "discovery_call_date", "operator": "LTE", "value": str(_date_ms(month_end))},
    ]
    props = ["dealname", "dealstage", "lead_source", "amount",
             "discovery_call_date", "alignment_call_date", "paid_recruitment_date",
             "pandadoc_signed", "createdate"]
    deals = search_all_deals(filters, props)

    rows = []
    for d in deals:
        p = d.get("properties", {})
        rows.append({
            "id":          d["id"],
            "company":     p.get("dealname", "Unknown"),
            "leadSource":  p.get("lead_source", "") or "",
            "dcDate":      (p.get("discovery_call_date")   or "")[:10],
            "acDate":      (p.get("alignment_call_date")    or "")[:10],
            "paidDate":    (p.get("paid_recruitment_date")  or "")[:10],
            "signedDate":  (p.get("pandadoc_signed")        or "")[:10],
            "stage":       STAGE_LABELS.get(p.get("dealstage", ""), p.get("dealstage", "")),
            "amount":      p.get("amount", "") or "",
            "createDate":  (p.get("createdate") or "")[:10],
        })
    rows.sort(key=lambda r: r["dcDate"])
    return rows

def fetch_deals_progress(contact_ids):
    """Count BD pipeline deal stages for the given OA BD contact IDs.
    Counts are based on each deal's current stage today."""
    if not contact_ids:
        return {"total": 0, "dcAttended": 0, "acAttended": 0, "paid": 0}

    assoc_map    = get_contact_deal_info(contact_ids)
    all_deal_ids = list({did for ids in assoc_map.values() for did in ids})
    if not all_deal_ids:
        return {"total": 0, "dcAttended": 0, "acAttended": 0, "paid": 0}

    deals = get_deals_by_ids(all_deal_ids)
    total = dc = ac = paid = 0
    for props in deals.values():
        if props.get("pipeline") != BD_PIPELINE_ID:
            continue
        total += 1
        stage  = props.get("dealstage", "")
        if stage in DC_ATTENDED_STAGES:        dc   += 1
        if stage in AC_ATTENDED_STAGES:        ac   += 1
        if props.get("paid_recruitment_date"): paid += 1  # actual payment recorded

    return {"total": total, "dcAttended": dc, "acAttended": ac, "paid": paid}

# ── SCORING ──────────────────────────────────────────────────────────────────

C_SUITE  = {"ceo","coo","cto","cfo","founder","co-founder","owner","president","managing director","md","principal"}
VP_LEVEL = {"vp","vice president","head","chief","director","partner"}
MGR_LEVEL= {"manager","supervisor","lead","team lead","sr.","senior"}

def score_title(title):
    t = (title or "").lower()
    if any(k in t for k in C_SUITE):  return 40
    if any(k in t for k in VP_LEVEL): return 28
    if any(k in t for k in MGR_LEVEL):return 18
    if t:                              return 10
    return 5

def score_email(email):
    e = (email or "").lower()
    free = ("gmail","yahoo","hotmail","outlook","icloud","me.com","aol","proton")
    if any(e.endswith(f) for f in free): return 0
    if "@" in e:                          return 15
    return 0

def score_company(company):
    enterprise = ("fortune","sotheby","berkshire","nhs","lucid motors","fujifilm","patterson")
    c = (company or "").lower()
    if any(k in c for k in enterprise): return 20
    if c and c not in ("(individual)","(independent)","individual","independent"): return 12
    return 2

def score_contact(c_props, has_active_deal, connected):
    s  = score_title(c_props.get("jobtitle",""))
    s += score_email(c_props.get("email",""))
    s += score_company(c_props.get("company",""))
    if has_active_deal and connected: s += 30
    elif has_active_deal:             s += 22
    elif connected:                   s += 15
    return min(s, 100)

def assign_tier(has_active_deal, connected, validity_key, score):
    if has_active_deal and score >= 55: return "CLOSE"
    if connected and not has_active_deal:  return "ADVANCE"
    if validity_key in ("valid_strict","valid_ni"): return "QUALIFY"
    return "PROSPECT"

def build_why(c_props, has_active_deal, connected, tier):
    title   = c_props.get("jobtitle","") or ""
    company = c_props.get("company","") or "(Individual)"
    parts = []
    if title:   parts.append(f"{title} at {company}")
    else:       parts.append(company)
    if has_active_deal and connected: parts.append("connected with active deal — strong close signal")
    elif has_active_deal:             parts.append("active deal in pipeline — needs advancing")
    elif connected:                   parts.append("connected — relationship ready, no deal yet")
    else:                             parts.append("valid BD lead — initial outreach stage")
    return ". ".join(p.capitalize() for p in parts[:2]) + "."

def build_action(tier, has_active_deal, connected):
    if tier == "CLOSE" and has_active_deal:
        return "Follow up on open deal — confirm requirements and timeline to close."
    if tier == "ADVANCE":
        return "Create deal — send capabilities deck and schedule discovery call."
    if tier == "QUALIFY":
        return "Schedule discovery call — explore outsourcing needs and qualify role."
    return "Assign validity first, then personalised outreach to explore business needs."

# ── WEEKLY CONTACTS STATS ─────────────────────────────────────────────────────

def fetch_weekly_contacts(start, end):
    """Fetch OA Business Development contacts (lead_category = BD Lead) created in [start, end]."""
    date_filters = [
        {"propertyName": "createdate", "operator": "GTE", "value": to_iso(start)},
        {"propertyName": "createdate", "operator": "LTE", "value": to_iso(end)},
    ]
    props = [PROP_LEAD_CATEGORY, PROP_VALIDITY, PROP_LEAD_STATUS,
             "firstname","lastname","jobtitle","company","email","phone","hs_country_code"]
    contacts = search_contacts_by_categories(list(OA_BD_VALUES), date_filters, props)
    print(f"  OA Business Development contacts found: {len(contacts)}")

    stats = dict(contacts_total=0, valid_strict=0, valid_ni=0, spam=0,
                 jobseeker=0, service_provider=0, unqualified=0, no_validity=0,
                 connected=0,
                 inbound=0, outbound=0, sp=0, other=0,
                 inbound_valid=0, inbound_connected=0,
                 outbound_valid=0, outbound_connected=0,
                 sp_valid=0, sp_connected=0)

    for c in contacts:
        p        = c.get("properties", {})
        val_key  = norm_validity(p.get(PROP_VALIDITY))
        src_key  = norm_source(p.get(PROP_LEAD_CATEGORY))
        conn     = is_connected(p.get(PROP_LEAD_STATUS))
        is_valid = val_key in ("valid_strict", "valid_ni")

        stats["contacts_total"]  += 1
        stats[val_key]           += 1
        stats[src_key]           += 1
        if conn: stats["connected"] += 1

        if src_key == "inbound":
            if is_valid: stats["inbound_valid"] += 1
            if conn:     stats["inbound_connected"] += 1
        elif src_key == "outbound":
            if is_valid: stats["outbound_valid"] += 1
            if conn:     stats["outbound_connected"] += 1
        elif src_key == "sp":
            if is_valid: stats["sp_valid"] += 1
            if conn:     stats["sp_connected"] += 1

    return stats, [c["id"] for c in contacts]

# ── WEEKLY DEALS COUNT + SA SIGNED ───────────────────────────────────────────

def _date_ms(dt):
    """Convert a date/datetime to midnight-UTC milliseconds for HubSpot date-type filters."""
    from datetime import timezone as _tz
    d = dt.date() if hasattr(dt, "date") else dt
    return int(datetime(d.year, d.month, d.day, tzinfo=_tz.utc).timestamp() * 1000)

def fetch_sa_signed_count(start, end):
    """Count BD pipeline deals where pandadoc_signed date falls within [start, end]."""
    return _fetch_deal_date_count("pandadoc_signed", start, end)

def _fetch_deal_date_count(date_prop, start, end):
    """Count BD pipeline deals where a given date property falls within [start, end]."""
    filters = [
        {"propertyName": "pipeline",  "operator": "EQ",  "value": BD_PIPELINE_ID},
        {"propertyName": date_prop,   "operator": "GTE", "value": str(_date_ms(start))},
        {"propertyName": date_prop,   "operator": "LTE", "value": str(_date_ms(end))},
    ]
    body = {"filterGroups": [{"filters": filters}], "properties": ["hs_object_id"], "limit": 1}
    r = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=body)
    r.raise_for_status()
    return r.json().get("total", 0)

def _fetch_deal_date_list(date_prop, start, end, date_field):
    """Fetch BD pipeline deals where date_prop falls in [start, end]. Returns deals tagged with date_field."""
    filters = [
        {"propertyName": "pipeline",  "operator": "EQ",  "value": BD_PIPELINE_ID},
        {"propertyName": date_prop,   "operator": "GTE", "value": str(_date_ms(start))},
        {"propertyName": date_prop,   "operator": "LTE", "value": str(_date_ms(end))},
    ]
    props = ["dealname", date_prop, "dealstage", "lead_source", "amount"]
    results, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": props, "limit": 100}
        if after:
            body["after"] = after
        r = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=body)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    deals = []
    for d in results:
        p = d.get("properties", {})
        deals.append({
            "id":         d["id"],
            "company":    p.get("dealname", "Unknown"),
            date_field:   p.get(date_prop, ""),
            "leadSource": p.get("lead_source", "") or "",
            "amount":     p.get("amount", "") or "",
            "stage":      STAGE_LABELS.get(p.get("dealstage", ""), p.get("dealstage", "")),
        })
    deals.sort(key=lambda x: x.get(date_field, ""))
    return deals

def fetch_sa_deals(start, end):
    """Fetch BD pipeline deals where pandadoc_signed falls in [start, end]."""
    return _fetch_deal_date_list("pandadoc_signed", start, end, "signedDate")

def fetch_deposit_deals(start, end):
    """Fetch BD pipeline deals where paid_recruitment_date falls in [start, end]."""
    return _fetch_deal_date_list("paid_recruitment_date", start, end, "paidDate")

def fetch_dc_deals(start, end):
    """Fetch BD pipeline deals with discovery_call_date in [start, end], returning company list."""
    filters = [
        {"propertyName": "pipeline",            "operator": "EQ",  "value": BD_PIPELINE_ID},
        {"propertyName": "discovery_call_date", "operator": "GTE", "value": str(_date_ms(start))},
        {"propertyName": "discovery_call_date", "operator": "LTE", "value": str(_date_ms(end))},
    ]
    props = ["dealname", "discovery_call_date", "discovery_call_attendance", "dealstage", "lead_source"]
    results, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": props, "limit": 100}
        if after:
            body["after"] = after
        r = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=body)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    deals = []
    for d in results:
        p = d.get("properties", {})
        deals.append({
            "id":         d["id"],
            "company":    p.get("dealname", "Unknown"),
            "dcDate":     p.get("discovery_call_date", ""),
            "attendance": p.get("discovery_call_attendance", ""),
            "leadSource": p.get("lead_source", "") or "",
            "stage":      STAGE_LABELS.get(p.get("dealstage", ""), p.get("dealstage", "")),
        })
    deals.sort(key=lambda x: x["dcDate"])
    return deals

def fetch_ac_deals(start, end):
    """Fetch BD pipeline deals with alignment_call_date in [start, end], returning company list."""
    filters = [
        {"propertyName": "pipeline",            "operator": "EQ",  "value": BD_PIPELINE_ID},
        {"propertyName": "alignment_call_date", "operator": "GTE", "value": str(_date_ms(start))},
        {"propertyName": "alignment_call_date", "operator": "LTE", "value": str(_date_ms(end))},
    ]
    props = ["dealname", "alignment_call_date", "alignment_call_attendance", "dealstage", "lead_source"]
    results, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": props, "limit": 100}
        if after:
            body["after"] = after
        r = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=body)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    deals = []
    for d in results:
        p = d.get("properties", {})
        deals.append({
            "id":         d["id"],
            "company":    p.get("dealname", "Unknown"),
            "acDate":     p.get("alignment_call_date", ""),
            "attendance": p.get("alignment_call_attendance", ""),
            "leadSource": p.get("lead_source", "") or "",
            "stage":      STAGE_LABELS.get(p.get("dealstage", ""), p.get("dealstage", "")),
        })
    deals.sort(key=lambda x: x["acDate"])
    return deals

def fetch_deals_created_count(start, end):
    filters = [
        {"propertyName": "pipeline",   "operator": "EQ",  "value": BD_PIPELINE_ID},
        {"propertyName": "createdate", "operator": "GTE", "value": to_iso(start)},
        {"propertyName": "createdate", "operator": "LTE", "value": to_iso(end)},
    ]
    body = {"filterGroups": [{"filters": filters}], "properties": ["hs_object_id"], "limit": 1}
    r = requests.post(f"{BASE_URL}/crm/v3/objects/deals/search", headers=HEADERS, json=body)
    r.raise_for_status()
    return r.json().get("total", 0)

# ── TOP 50 OUTREACH ───────────────────────────────────────────────────────────

def fetch_top_50():
    """Fetch top BD Lead contacts for the outreach ranking."""
    print("  Fetching top contacts for outreach ranking…")

    # Fetch recent BD contacts (last 90 days for a useful pool)
    now = datetime.now(MANILA_TZ)
    ninety_days_ago = now - timedelta(days=90)
    date_filter = [{"propertyName": "createdate", "operator": "GTE", "value": to_iso(ninety_days_ago)}]
    props = [PROP_LEAD_CATEGORY, PROP_VALIDITY, PROP_LEAD_STATUS,
             "firstname","lastname","jobtitle","company","email","phone","hs_country_code"]
    contacts = search_contacts_by_categories(list(ALL_BD_VALUES), date_filter, props)
    print(f"  Pool: {len(contacts)} BD contacts (90 days)")

    # Get deal associations in batch
    contact_ids = [c["id"] for c in contacts]
    assoc_map   = get_contact_deal_info(contact_ids)

    # Collect all deal IDs and fetch them
    all_deal_ids = list({did for ids in assoc_map.values() for did in ids})
    deals_by_id  = get_deals_by_ids(all_deal_ids) if all_deal_ids else {}

    # Score each contact
    scored = []
    for c in contacts:
        p       = c.get("properties", {})
        val_key = norm_validity(p.get(PROP_VALIDITY))
        conn    = is_connected(p.get(PROP_LEAD_STATUS))

        deal_ids = assoc_map.get(c["id"], [])
        active_deals = [d for did in deal_ids
                        if (d := deals_by_id.get(did))
                        and d.get("pipeline") == BD_PIPELINE_ID
                        and d.get("dealstage") in ACTIVE_STAGES]
        has_active = len(active_deals) > 0

        score = score_contact(p, has_active, conn)
        tier  = assign_tier(has_active, conn, val_key, score)

        fn = (p.get("firstname") or "").strip()
        ln = (p.get("lastname")  or "").strip()
        name = f"{fn} {ln}".strip() or "Unknown"
        country = (p.get("hs_country_code") or "").strip().upper()

        scored.append({
            "id":       c["id"],
            "name":     name,
            "title":    (p.get("jobtitle") or "").strip(),
            "company":  (p.get("company")  or "(Individual)").strip(),
            "email":    (p.get("email")    or "").strip(),
            "phone":    (p.get("phone")    or "").strip(),
            "country":  country,
            "score":    score,
            "tier":     tier,
            "deals":    len(active_deals),
            "conn":     conn,
            "val_key":  val_key,
        })

    scored.sort(key=lambda x: (-x["score"], x["name"]))
    top50 = scored[:50]

    result = []
    for i, c in enumerate(top50, 1):
        result.append({
            "rank":    i,
            "name":    c["name"],
            "title":   c["title"],
            "company": c["company"],
            "email":   c["email"],
            "phone":   c["phone"],
            "country": c["country"],
            "deals":   c["deals"],
            "tier":    c["tier"],
            "score":   c["score"],
            "why":     build_why({"jobtitle": c["title"], "company": c["company"]},
                                  c["deals"] > 0, c["conn"], c["tier"]),
            "action":  build_action(c["tier"], c["deals"] > 0, c["conn"]),
            "hsId":    c["id"],
        })
    return result

# ── MONTHLY CONTACTS ─────────────────────────────────────────────────────────

def fetch_monthly_contacts(num_months=6):
    """Fetch OA Business Development contact counts for each of the last N calendar months."""
    now     = datetime.now(MANILA_TZ)
    mo_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    props   = [PROP_LEAD_CATEGORY, PROP_VALIDITY, PROP_LEAD_STATUS]
    results = []

    for i in range(num_months - 1, -1, -1):
        offset = now.month - 1 - i          # Python floor-div handles negatives correctly
        y      = now.year + offset // 12
        m      = offset % 12 + 1

        month_start = datetime(y, m, 1, 0, 0, 0, tzinfo=MANILA_TZ)
        if m == 12:
            month_end = datetime(y + 1, 1, 1, tzinfo=MANILA_TZ) - timedelta(seconds=1)
        else:
            month_end = datetime(y, m + 1, 1, tzinfo=MANILA_TZ) - timedelta(seconds=1)
        is_partial = (i == 0)
        if is_partial:
            month_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

        label = f"{mo_names[m-1]} {y}"
        print(f"    {label}...", end=" ", flush=True)

        date_filters = [
            {"propertyName": "createdate", "operator": "GTE", "value": to_iso(month_start)},
            {"propertyName": "createdate", "operator": "LTE", "value": to_iso(month_end)},
        ]
        contacts = search_contacts_by_categories(list(OA_BD_VALUES), date_filters, props)

        total     = len(contacts)
        valid_c   = sum(1 for c in contacts
                        if norm_validity(c.get("properties",{}).get(PROP_VALIDITY))
                        in ("valid_strict","valid_ni"))
        connected = sum(1 for c in contacts
                        if is_connected(c.get("properties",{}).get(PROP_LEAD_STATUS)))
        print(f"{total} contacts", end=" ", flush=True)

        contact_ids  = [c["id"] for c in contacts]
        deals_prog   = fetch_deals_progress(contact_ids)
        deal_details = fetch_monthly_deal_details(month_start, month_end)
        sa_signed    = fetch_sa_signed_count(month_start, month_end)
        dc_count     = _fetch_deal_date_count("discovery_call_date",    month_start, month_end)
        ac_count     = _fetch_deal_date_count("alignment_call_date",     month_start, month_end)
        paid_count   = _fetch_deal_date_count("paid_recruitment_date",   month_start, month_end)
        print(f"| {deals_prog['total']} deals | {sa_signed} SA | {dc_count} DC | {ac_count} AC | {paid_count} Paid")

        results.append({
            "key":         f"{y}-{m:02d}",
            "label":       label,
            "year":        y,
            "month":       m,
            "partial":     is_partial,
            "contacts":    total,
            "valid":       valid_c,
            "connected":   connected,
            "validRate":   round(valid_c / total * 100, 1) if total else 0.0,
            "connectRate": round(connected / total * 100, 1) if total else 0.0,
            "dealProgress": deals_prog,
            "dealDetails":  deal_details,
            "saSigned":    sa_signed,
            "dcCount":     dc_count,
            "acCount":     ac_count,
            "paidCount":   paid_count,
        })

    return results


# ── PAID DEALS ────────────────────────────────────────────────────────────────

def _cycle_for_date(iso_date):
    """Return cycle name for a YYYY-MM-DD-ish iso date string, or None if unmatched."""
    if not iso_date:
        return None
    d = iso_date[:10]
    for c in PAID_CYCLES:
        if c["start"] <= d <= c["end"]:
            return c["name"]
    return None

def fetch_paid_deals():
    """Fetch deals with lead_source = 'Outbound Paid' and classify into HOT/ADVANCING/STALLED/CLOSED."""
    filters = [
        {"propertyName": "pipeline",    "operator": "EQ", "value": BD_PIPELINE_ID},
        {"propertyName": "lead_source", "operator": "EQ", "value": "Outbound Paid"},
    ]
    props = ["dealname","dealstage","lead_source","paid_recruitment_date","hs_lastmodifieddate",
             "amount","hs_is_closed_won","hs_probability","createdate","discovery_call_date",
             "discovery_call_attendance"]
    deals = search_all_deals(filters, props)
    print(f"  Outbound Paid deals found: {len(deals)}")

    now = datetime.now(MANILA_TZ)
    today_iso = now.strftime("%Y-%m-%d")
    current_cycle = _cycle_for_date(today_iso) or (PAID_CYCLES[-1]["name"] if PAID_CYCLES else None)
    groups = {"HOT": [], "ADVANCING": [], "STALLED": [], "CLOSED": []}

    for d in deals:
        p      = d.get("properties", {})
        stage  = p.get("dealstage", "")
        lmod   = p.get("hs_lastmodifieddate", "")
        cdate  = p.get("createdate", "")
        dcdate = p.get("discovery_call_date", "")
        dcatt  = p.get("discovery_call_attendance", "")
        name   = p.get("dealname", "Unknown Deal")
        prob   = int(float(p.get("hs_probability") or 0) * 100) or None

        try:
            lmod_dt  = datetime.fromisoformat(lmod.replace("Z","+00:00"))
            age_days = (now - lmod_dt.astimezone(MANILA_TZ)).days
        except Exception:
            age_days = 0

        stage_label = STAGE_LABELS.get(stage, stage)

        entry = {
            "id":          d["id"],
            "company":     name,
            "role":        "",
            "stage":       stage_label,
            "prob":        prob,
            "ageDays":     age_days,
            "lastMod":     lmod[:10] if lmod else "",
            "createDate":  cdate[:10] if cdate else "",
            "dcDate":      dcdate[:10] if dcdate else "",
            "dcAttendance": dcatt,
            "cycle":       _cycle_for_date(dcdate),
            "action":      "",
        }

        if stage == STAGES["ac_completed"]:
            entry["action"] = "Schedule closing call — highest urgency in pipeline"
            groups["HOT"].append(entry)
        elif stage in (STAGES["dc_completed"], STAGES["ac_outreach"]):
            entry["action"] = "Advance to next stage"
            groups["ADVANCING"].append(entry)
        elif stage in (STAGES["dc_no_show"], STAGES["ac_no_show"], STAGES["deal_unqualified"]):
            entry["action"] = "Re-engage or reschedule missed call"
            groups["STALLED"].append(entry)
        elif stage in (STAGES["closed_won"], STAGES["hiring_recruiting"]):
            entry["action"] = "Monitor progress — deal closed, in hiring stage"
            entry["closedWon"] = True
            groups["CLOSED"].append(entry)
        elif stage == STAGES["closed_lost"]:
            entry["action"] = "Request loss reason for learnings"
            entry["closedWon"] = False
            groups["CLOSED"].append(entry)
        else:
            entry["action"] = "Review and advance"
            groups["ADVANCING"].append(entry)

    return {
        "lastQueried":  now.strftime("%Y-%m-%d"),
        "groups":       groups,
        "cycles":       PAID_CYCLES,
        "currentCycle": current_cycle,
    }

# ── LONG VIEW: DC -> DEPOSIT CONVERSION TIME ─────────────────────────────────

LONG_VIEW_START = "2025-01-01"

def _batch_read_deal_contacts(deal_ids):
    """Return {deal_id: [contact_id, ...]} via v4 batch associations."""
    if not deal_ids:
        return {}
    out = {}
    for i in range(0, len(deal_ids), 100):
        inputs = [{"id": str(did)} for did in deal_ids[i:i+100]]
        r = requests.post(
            f"{BASE_URL}/crm/v4/associations/deals/contacts/batch/read",
            headers=HEADERS, json={"inputs": inputs}
        )
        if r.status_code not in (200, 207):
            continue
        for item in r.json().get("results", []):
            did = str(item.get("from", {}).get("id", ""))
            out[did] = [str(a.get("toObjectId", "")) for a in item.get("to", [])]
    return out

def _batch_read_contacts(contact_ids, props):
    """Batch v3 read of contacts. Returns {contact_id: properties_dict}."""
    if not contact_ids:
        return {}
    out = {}
    for i in range(0, len(contact_ids), 100):
        inputs = [{"id": str(cid)} for cid in contact_ids[i:i+100]]
        r = requests.post(
            f"{BASE_URL}/crm/v3/objects/contacts/batch/read",
            headers=HEADERS, json={"inputs": inputs, "properties": props}
        )
        if r.status_code not in (200, 207):
            continue
        for c in r.json().get("results", []):
            out[str(c["id"])] = c.get("properties", {})
    return out

def fetch_long_view_analysis():
    """Long View: time between Discovery Call Date and Paid Recruitment Date for OA BD
    leads created since 2025-01-01.

    Filter chain:
      - BD pipeline deals where both discovery_call_date AND paid_recruitment_date are set
      - At least one associated contact has lead_category = 'BD Lead' (OA Business Development)
      - That contact was createdate >= 2025-01-01
    """
    print(f"\n[Long View] Fetching paid BD deals with DC dates since {LONG_VIEW_START}…")

    filters = [
        {"propertyName": "pipeline",              "operator": "EQ",           "value": BD_PIPELINE_ID},
        {"propertyName": "paid_recruitment_date", "operator": "HAS_PROPERTY"},
        {"propertyName": "discovery_call_date",   "operator": "HAS_PROPERTY"},
    ]
    props = ["dealname", "discovery_call_date", "paid_recruitment_date",
             "lead_source", "amount", "dealstage"]
    deals = search_all_deals(filters, props)
    print(f"  BD paid deals (any date) with DC: {len(deals)}")

    if not deals:
        return {
            "periodStart": LONG_VIEW_START,
            "periodEnd":   datetime.now(MANILA_TZ).strftime("%Y-%m-%d"),
            "stats":       {"count": 0, "avg": 0, "median": 0, "min": 0, "max": 0},
            "buckets":     {},
            "deals":       [],
        }

    deal_ids = [d["id"] for d in deals]
    deal_to_contacts = _batch_read_deal_contacts(deal_ids)
    all_contact_ids  = list({cid for cids in deal_to_contacts.values() for cid in cids})
    print(f"  Associated contacts: {len(all_contact_ids)}")

    contact_props = _batch_read_contacts(all_contact_ids,
                                         [PROP_LEAD_CATEGORY, "createdate"])

    rows = []
    for d in deals:
        did = d["id"]
        p   = d.get("properties", {})
        dc   = (p.get("discovery_call_date")   or "")[:10]
        paid = (p.get("paid_recruitment_date") or "")[:10]
        if not dc or not paid:
            continue

        matching = None
        for cid in deal_to_contacts.get(did, []):
            cp       = contact_props.get(cid, {})
            lead_cat = cp.get(PROP_LEAD_CATEGORY, "")
            cdate    = (cp.get("createdate") or "")[:10]
            if lead_cat == OA_BD_VALUE and cdate >= LONG_VIEW_START:
                matching = {"id": cid, "createdate": cdate}
                break

        if not matching:
            continue

        try:
            days = (datetime.fromisoformat(paid) - datetime.fromisoformat(dc)).days
        except Exception:
            continue
        if days < 0:
            continue  # deposit paid before DC — likely data entry oddity

        rows.append({
            "dealId":         did,
            "company":        p.get("dealname", "Unknown"),
            "contactCreated": matching["createdate"],
            "dcDate":         dc,
            "paidDate":       paid,
            "days":           days,
            "leadSource":     p.get("lead_source", "") or "",
            "amount":         p.get("amount", "") or "",
            "stage":          STAGE_LABELS.get(p.get("dealstage", ""), p.get("dealstage", "")),
        })

    print(f"  Deals matching OA BD contact + 2025+ filter: {len(rows)}")

    days_list = [r["days"] for r in rows]
    if days_list:
        srt = sorted(days_list)
        n   = len(srt)
        avg = sum(days_list) / n
        med = srt[n // 2] if n % 2 == 1 else (srt[n // 2 - 1] + srt[n // 2]) / 2
        stats = {"count": n, "avg": round(avg, 1), "median": float(med),
                 "min": min(days_list), "max": max(days_list)}
    else:
        stats = {"count": 0, "avg": 0, "median": 0, "min": 0, "max": 0}

    buckets = {"0–7": 0, "8–14": 0, "15–30": 0, "31–60": 0, "61–90": 0, "90+": 0}
    for v in days_list:
        if v <= 7:    buckets["0–7"]   += 1
        elif v <= 14: buckets["8–14"]  += 1
        elif v <= 30: buckets["15–30"] += 1
        elif v <= 60: buckets["31–60"] += 1
        elif v <= 90: buckets["61–90"] += 1
        else:         buckets["90+"]   += 1

    rows.sort(key=lambda r: r["paidDate"], reverse=True)
    return {
        "periodStart": LONG_VIEW_START,
        "periodEnd":   datetime.now(MANILA_TZ).strftime("%Y-%m-%d"),
        "stats":       stats,
        "buckets":     buckets,
        "deals":       rows,
    }

# ── HTML PATCH ────────────────────────────────────────────────────────────────

def update_html(week_num, week_data, paid_deals_data, monthly_data, today, long_view=None):
    print(f"  Patching {HTML_FILE}…")
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. Parse existing JSON
    m = re.search(r'<script id="report-data" type="application/json">(.*?)</script>',
                  html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find report-data script tag in HTML")
    report = json.loads(m.group(1))

    # 2. Update meta
    report["meta"]["lastUpdated"] = today.strftime("%Y-%m-%d")

    # 3. Add / overwrite this week
    report["weeks"][str(week_num)] = week_data

    # 4. Overwrite paidDeals
    report["paidDeals"] = paid_deals_data

    # 5. Overwrite monthly contacts
    report["monthly"] = monthly_data

    # 6. Overwrite Long View analysis (if provided)
    if long_view is not None:
        report["longView"] = long_view

    new_json = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    new_tag  = f'<script id="report-data" type="application/json">\n{new_json}\n</script>'
    html     = html[:m.start()] + new_tag + html[m.end():]

    # 5. Update meta data-last-updated
    html = re.sub(r'content="[\d-]+"(\s+/>|>)\s*(?=\s*<meta name="data-cycle")',
                  f'content="{today.strftime("%Y-%m-%d")}"\\1\n  ', html)

    # 7. Update header week badge initial text
    wk_label  = f"W{week_num}"
    dates_str = week_data["dates"]
    year_str  = str(week_data["year"])
    html = re.sub(
        r'(<div class="week-badge" id="currentWeekLabel">)[^<]*(</div>)',
        rf'\g<1>{wk_label} &middot; {dates_str}, {year_str}\g<2>',
        html
    )

    # 8. Update sidebar "This Week" label
    html = re.sub(
        r'(<div class="sidebar-label">This Week \()W\d+(\)</div>)',
        rf'\g<1>{wk_label}\g<2>',
        html
    )

    # 9. Rebuild week navigator tabs — keep existing + add new if missing
    #    Find the week-tabs div and regenerate its contents
    def make_tabs(weeks_dict):
        sorted_keys = sorted(weeks_dict.keys(), key=int)
        months_short = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        tabs = []
        for k in sorted_keys:
            w = weeks_dict[k]
            active = " active" if int(k) == week_num else ""
            try:
                end_dt   = datetime.fromisoformat(w["endDate"])
                my_label = f'{months_short[end_dt.month - 1]} {end_dt.year}'
            except Exception:
                my_label = str(w.get("year", ""))
            tabs.append(
                f'<button class="week-tab{active}" data-week="{k}" '
                f'onclick="selectWeek({k}, this)">'
                f'{my_label} &middot; {w["label"]}</button>'
            )
        return "\n        ".join(tabs)

    tab_html = make_tabs(report["weeks"])
    html = re.sub(
        r'(<div class="week-tabs">)\s*(.*?)\s*(</div>)',
        rf'\g<1>\n        {tab_html}\n      \g<3>',
        html, flags=re.DOTALL, count=1
    )

    # 10. Update nextUpdateLabel static value (JS will also recalculate this)
    nxt = today + timedelta(days=7)
    mo  = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    nxt_label = f"Next update: {mo[nxt.month-1]} {nxt.day}, {nxt.year}"
    html = re.sub(r'Next update: \w+ \d+, \d+', nxt_label, html)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML updated: {HTML_FILE}")

# ── DISCOVER MODE ─────────────────────────────────────────────────────────────

def discover_properties():
    print("\n=== DISCOVERING HUBSPOT CONTACT PROPERTIES ===\n")
    r = requests.get(f"{BASE_URL}/crm/v3/properties/contacts", headers=HEADERS)
    r.raise_for_status()
    props = r.json().get("results", [])

    validity_cands, source_cands, connected_cands = [], [], []
    for p in props:
        if p.get("groupName") == "contactinformation" and not p.get("hidden"):
            continue  # skip purely standard fields
        name  = p.get("name","")
        label = p.get("label","")
        ftype = p.get("fieldType","")
        opts  = [o.get("label","").lower() for o in p.get("options",[])]
        opts_str = " | ".join(opts)

        if any(v in opts_str for v in ["valid","spam","job seeker","unqualified"]):
            validity_cands.append((name, label, opts_str))
        if any(v in opts_str for v in ["inbound","outbound"]):
            source_cands.append((name, label, opts_str))
        if ftype == "booleancheckbox" and "connect" in name.lower():
            connected_cands.append((name, label))

    print("── VALIDITY CANDIDATES (set PROP_VALIDITY to one of these) ──")
    for n,l,o in validity_cands:
        print(f"  name={n!r:40s}  label={l!r}  options: {o}")

    print("\n── SOURCE/TYPE CANDIDATES (set PROP_SOURCE_TYPE) ──")
    for n,l,o in source_cands:
        print(f"  name={n!r:40s}  label={l!r}  options: {o}")

    print("\n── CONNECTED CANDIDATES (set PROP_CONNECTED) ──")
    for n,l in connected_cands:
        print(f"  name={n!r:40s}  label={l!r}")

    print("\n─────────────────────────────────────────────")
    print("Edit PROP_VALIDITY, PROP_SOURCE_TYPE, PROP_CONNECTED at the top of this script.")

# ── BACKFILL ─────────────────────────────────────────────────────────────────

def backfill_missing_fields():
    """Patch all weeks in the HTML that are missing saSigned / dcCount / acCount."""
    print(f"\n[BACKFILL] Reading {HTML_FILE}…")
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    m = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find report-data script tag")
    report = json.loads(m.group(1))

    patched = 0
    for wk_str, d in sorted(report["weeks"].items(), key=lambda x: int(x[0])):
        def _has_lead_source(deal_list):
            return all("leadSource" in x for x in (deal_list or []))

        if ("saSigned" in d and "dcCount" in d and "acCount" in d
                and "acDeals" in d and "paidSettled" in d
                and "saDeals" in d and "depositDeals" in d
                and _has_lead_source(d.get("dcDeals"))
                and _has_lead_source(d.get("acDeals"))):
            print(f"  W{wk_str}: already complete, skipping")
            continue

        start = datetime.fromisoformat(d["startDate"]).replace(
            hour=0, minute=0, second=0, tzinfo=MANILA_TZ)
        end   = datetime.fromisoformat(d["endDate"]).replace(
            hour=23, minute=59, second=59, tzinfo=MANILA_TZ)
        print(f"  W{wk_str}: fetching missing fields ({d['startDate']} to {d['endDate']})…", flush=True)

        sa_deals      = fetch_sa_deals(start, end)
        d["saDeals"]  = sa_deals
        d["saSigned"] = len(sa_deals)
        dc_deals      = fetch_dc_deals(start, end)
        d["dcCount"]  = len(dc_deals)
        d["dcDeals"]  = dc_deals
        ac_deals      = fetch_ac_deals(start, end)
        d["acCount"]  = len(ac_deals)
        d["acDeals"]  = ac_deals
        deposit_deals = fetch_deposit_deals(start, end)
        d["depositDeals"] = deposit_deals
        d["paidSettled"]  = len(deposit_deals)

        print(f"    SA={d['saSigned']} DC={d['dcCount']} AC={d['acCount']} Deposit={d['paidSettled']}")
        patched += 1

    if patched == 0:
        print("  Nothing to backfill.")
        return

    new_json = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    new_tag  = f'<script id="report-data" type="application/json">\n{new_json}\n</script>'
    html     = html[:m.start()] + new_tag + html[m.end():]
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Backfilled {patched} week(s). HTML saved.")

# ── REBUILD WEEKS ────────────────────────────────────────────────────────────

def rebuild_weeks():
    """Re-key every stored week to a clean Wed–Tue range and refresh all its stats.
    Fixes legacy weeks whose start/end dates drifted off the Tuesday anchor (gaps).
    Preserves week_num key (W19 stays W19) and topAccounts (skipped to save time)."""
    print(f"\n[REBUILD] Reading {HTML_FILE}…")
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    m = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find report-data script tag")
    report = json.loads(m.group(1))

    sorted_keys = sorted(report["weeks"].keys(), key=int)
    for wk_str in sorted_keys:
        d   = report["weeks"][wk_str]
        old_dates = d.get("dates", "")
        # Anchor: take the stored endDate, snap to its nearest Tue ≤ endDate
        try:
            anchor = datetime.fromisoformat(d["endDate"]).replace(tzinfo=MANILA_TZ)
        except Exception:
            print(f"  W{wk_str}: bad endDate {d.get('endDate')!r}, skipping")
            continue
        start, end, _ignored_wk, dates_label = _week_range_for_anchor(anchor)
        print(f"\n  W{wk_str}: {old_dates}  ->  {dates_label}  ({start.date()} → {end.date()})")

        # Refresh contact stats
        stats, weekly_contact_ids = fetch_weekly_contacts(start, end)
        # Refresh deal-driven metrics
        deals_count   = fetch_deals_created_count(start, end)
        dc_deals      = fetch_dc_deals(start, end)
        ac_deals      = fetch_ac_deals(start, end)
        sa_deals      = fetch_sa_deals(start, end)
        deposit_deals = fetch_deposit_deals(start, end)
        weekly_deals_progress = fetch_deals_progress(weekly_contact_ids)
        valid_total = stats["valid_strict"] + stats["valid_ni"]

        d.update({
            "dates":      dates_label,
            "startDate":  start.strftime("%Y-%m-%d"),
            "endDate":    end.strftime("%Y-%m-%d"),
            "year":       end.year,
            "contacts":   stats["contacts_total"],
            "valid":      valid_total,
            "validStrict": stats["valid_strict"],
            "validNI":     stats["valid_ni"],
            "connected":   stats["connected"],
            "qualified":   valid_total,
            "deals":       deals_count,
            "dealsAll":    deals_count,
            "spam":              stats["spam"],
            "jobseekers":        stats["jobseeker"],
            "serviceProviders":  stats["service_provider"],
            "unqualified":       stats["unqualified"],
            "noValidity":        stats["no_validity"],
            "inbound":           stats["inbound"],
            "outbound":          stats["outbound"],
            "sp":                stats["sp"],
            "other":             stats["other"],
            "inboundValid":      stats["inbound_valid"],
            "inboundConnected":  stats["inbound_connected"],
            "outboundValid":     stats["outbound_valid"],
            "outboundConnected": stats["outbound_connected"],
            "spValid":           stats["sp_valid"],
            "spConnected":       stats["sp_connected"],
            "dealProgress":      weekly_deals_progress,
            "saDeals":           sa_deals,
            "saSigned":          len(sa_deals),
            "dcCount":           len(dc_deals),
            "dcDeals":           dc_deals,
            "acCount":           len(ac_deals),
            "acDeals":           ac_deals,
            "depositDeals":      deposit_deals,
            "paidSettled":       len(deposit_deals),
        })
        print(f"    contacts={stats['contacts_total']} valid={valid_total} "
              f"connected={stats['connected']} deals={deals_count} "
              f"SA={len(sa_deals)} DC={len(dc_deals)} AC={len(ac_deals)} "
              f"Deposit={len(deposit_deals)}")

    # Persist
    new_json = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    new_tag  = f'<script id="report-data" type="application/json">\n{new_json}\n</script>'
    html     = html[:m.start()] + new_tag + html[m.end():]

    # Update the static week-tab labels to match (uses make_tabs style logic inline)
    today = datetime.now(MANILA_TZ)
    _, _, current_wk, _ = current_week_range()
    months_short = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    tabs = []
    for k in sorted_keys:
        w = report["weeks"][k]
        active = " active" if int(k) == current_wk else ""
        try:
            end_dt   = datetime.fromisoformat(w["endDate"])
            my_label = f'{months_short[end_dt.month - 1]} {end_dt.year}'
        except Exception:
            my_label = str(w.get("year", ""))
        tabs.append(
            f'<button class="week-tab{active}" data-week="{k}" '
            f'onclick="selectWeek({k}, this)">'
            f'{my_label} &middot; W{k}</button>'
        )
    tab_html = "\n        ".join(tabs)
    html = re.sub(
        r'(<div class="week-tabs">)\s*(.*?)\s*(</div>)',
        rf'\g<1>\n        {tab_html}\n      \g<3>',
        html, flags=re.DOTALL, count=1
    )

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n  Rebuilt {len(sorted_keys)} week(s). HTML saved.")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if "--discover" in sys.argv:
        if not HUBSPOT_TOKEN:
            print("ERROR: set HUBSPOT_TOKEN before running --discover")
            sys.exit(1)
        discover_properties()
        return

    if "--backfill" in sys.argv:
        if not HUBSPOT_TOKEN:
            print("ERROR: set HUBSPOT_TOKEN before running --backfill")
            sys.exit(1)
        backfill_missing_fields()
        return

    if "--rebuild-weeks" in sys.argv:
        if not HUBSPOT_TOKEN:
            print("ERROR: set HUBSPOT_TOKEN before running --rebuild-weeks")
            sys.exit(1)
        rebuild_weeks()
        return

    if not HUBSPOT_TOKEN:
        print("ERROR: HUBSPOT_TOKEN environment variable not set.")
        print("  PowerShell: $env:HUBSPOT_TOKEN = 'pat-na1-...'")
        print("  Run --discover to find your contact property names.")
        sys.exit(1)

    today = datetime.now(MANILA_TZ)
    print(f"\n[{today.isoformat()}] OA BD Weekly Report Updater")
    print("-" * 55)

    start, end, week_num, dates_label = current_week_range()
    print(f"  Week {week_num}: {dates_label} ({start.date()} to {end.date()})")

    # Weekly contacts
    print("\n[1/5] Fetching weekly contact stats…")
    stats, weekly_contact_ids = fetch_weekly_contacts(start, end)

    # Weekly deals progress
    print(f"\n[2/5] Fetching weekly deal progress ({len(weekly_contact_ids)} contacts)…")
    weekly_deals_progress = fetch_deals_progress(weekly_contact_ids)
    print(f"  Deals: {weekly_deals_progress['total']} total | "
          f"{weekly_deals_progress['dcAttended']} DC | "
          f"{weekly_deals_progress['acAttended']} AC | "
          f"{weekly_deals_progress['paid']} Paid")

    # Weekly deal metrics
    print("\n[3/5] Counting weekly deal metrics…")
    deals_count = fetch_deals_created_count(start, end)
    dc_deals       = fetch_dc_deals(start, end)
    dc_count       = len(dc_deals)
    ac_deals       = fetch_ac_deals(start, end)
    ac_count       = len(ac_deals)
    sa_deals       = fetch_sa_deals(start, end)
    sa_signed      = len(sa_deals)
    deposit_deals  = fetch_deposit_deals(start, end)
    paid_settled   = len(deposit_deals)
    print(f"  Deals created: {deals_count} | SA: {sa_signed} | DC: {dc_count} | AC: {ac_count} | Deposit: {paid_settled}")

    # Top 50 outreach
    print("\n[4/5] Building top 50 outreach list…")
    top_accounts = fetch_top_50()
    print(f"  Top accounts: {len(top_accounts)}")

    # Paid deals
    print("\n[5/5] Fetching Outbound Paid deals + monthly data…")
    paid_deals = fetch_paid_deals()

    # Monthly contacts
    print("\n[5/5] Fetching monthly contact stats (last 6 months)…")
    monthly_data = fetch_monthly_contacts(num_months=6)
    print(f"  Monthly data: {len(monthly_data)} months fetched")

    # Long View analysis (DC -> Paid conversion time, since 2025-01-01)
    long_view = fetch_long_view_analysis()

    # Build week object
    valid_total = stats["valid_strict"] + stats["valid_ni"]
    week_data = {
        "label":    f"W{week_num}",
        "dates":    dates_label,
        "year":     today.year,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate":   end.strftime("%Y-%m-%d"),
        "contacts": stats["contacts_total"],
        "valid":    valid_total,
        "validStrict": stats["valid_strict"],
        "validNI":     stats["valid_ni"],
        "connected":   stats["connected"],
        "qualified":   valid_total,
        "deals":       deals_count,
        "dealsAll":    deals_count,
        "spam":              stats["spam"],
        "jobseekers":        stats["jobseeker"],
        "serviceProviders":  stats["service_provider"],
        "unqualified":       stats["unqualified"],
        "noValidity":        stats["no_validity"],
        "inbound":           stats["inbound"],
        "outbound":          stats["outbound"],
        "sp":                stats["sp"],
        "other":             stats["other"],
        "inboundValid":      stats["inbound_valid"],
        "inboundConnected":  stats["inbound_connected"],
        "outboundValid":     stats["outbound_valid"],
        "outboundConnected": stats["outbound_connected"],
        "spValid":           stats["sp_valid"],
        "spConnected":       stats["sp_connected"],
        "topAccounts":       top_accounts,
        "dealProgress":      weekly_deals_progress,
        "saSigned":          sa_signed,
        "dcCount":           dc_count,
        "dcDeals":           dc_deals,
        "acCount":           ac_count,
        "acDeals":           ac_deals,
        "saDeals":           sa_deals,
        "paidSettled":       paid_settled,
        "depositDeals":      deposit_deals,
    }

    # Patch HTML
    print("\n[Patching HTML]")
    update_html(week_num, week_data, paid_deals, monthly_data, today, long_view)

    print(f"\nDone - Week {week_num} ({dates_label}) committed to HTML.")
    print(f"  Next update: next Tuesday\n")


if __name__ == "__main__":
    main()
