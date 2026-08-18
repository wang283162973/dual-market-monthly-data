from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


OUTPUT = Path(__file__).parent / "public" / "data.json"
MIAOXIANG_URL = "https://ai-saas.eastmoney.com/proxy/b/mcp/tool/searchData"
ALL_A_ENTITY_TAG = {"entityId": "001071", "fullName": "全部A股", "classCode": "005202"}
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get?secid=47.800005&fields=f43,f47,f48,f57,f58,f59,f86"
BREADTH_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.000002,0.399107,0.899050&fields=f12,f14,f104,f105,f106,f124"


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_current_month(now: datetime):
    api_key = (os.environ.get("EM_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("EM_API_KEY is not configured")
    month = now.strftime("%Y-%m")
    query = (
        f"选定实体的{month}-01至{now:%Y-%m-%d}每日收盘价(算术平均)、"
        "成交额(合计)、上涨家数、下跌家数、平盘家数"
    )
    response = requests.post(
        MIAOXIANG_URL,
        json={
            "query": query,
            "toolContext": {
                "callId": f"call_{uuid.uuid4().hex[:8]}",
                "userInfo": {"userId": f"user_{uuid.uuid4().hex[:8]}"},
                "toolPreTaskResultList": [{
                    "taskName": "股票基金筛选",
                    "entityTagListMap": {"1": [ALL_A_ENTITY_TAG]},
                }],
            },
        },
        headers={"Content-Type": "application/json", "em_api_key": api_key},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    result = data.get("searchDataResultDTO") or {}
    dto_list = result.get("dataTableDTOList") or data.get("dataTableDTOList") or []
    wanted = {
        "收盘价(算术平均)": "close",
        "成交额(合计)": "amount",
        "上涨家数": "up",
        "下跌家数": "down",
        "平盘家数": "flat",
    }
    rows = {}
    for dto in dto_list:
        raw_table = dto.get("rawTable") or {}
        dates = raw_table.get("headName") or []
        for field_key, field_name in (dto.get("nameMap") or {}).items():
            output_name = wanted.get(str(field_name))
            if not output_name:
                continue
            values = raw_table.get(str(field_key), [])
            for index, date_value in enumerate(dates):
                date_text = str(date_value)[:10]
                if not date_text.startswith(month) or index >= len(values):
                    continue
                value = safe_float(values[index])
                if value is not None:
                    rows.setdefault(date_text, {"date": date_text})[output_name] = value

    valid = []
    for row in rows.values():
        if not all(row.get(key) is not None for key in ("close", "amount", "up", "down", "flat")):
            continue
        normal_count = row["up"] + row["down"] + row["flat"]
        if normal_count <= 0:
            continue
        row["per_company"] = row["amount"] / 100_000_000 / normal_count
        valid.append(row)
    if not valid:
        raise RuntimeError("No valid all-A rows returned")
    return sorted(valid, key=lambda item: item["date"])


def fetch_public_json(url):
    last_error = None
    for _ in range(3):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                timeout=12,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise last_error or RuntimeError("Public quote request failed")


def fetch_intraday(now):
    quote = (fetch_public_json(QUOTE_URL).get("data") or {})
    breadth = (fetch_public_json(BREADTH_URL).get("data") or {}).get("diff") or []
    quote_timestamp = int(quote.get("f86") or 0)
    breadth_timestamps = [int(item.get("f124") or 0) for item in breadth if int(item.get("f124") or 0) > 0]
    if not quote_timestamp or not breadth_timestamps:
        raise RuntimeError("Intraday timestamp is missing")
    quote_date = datetime.fromtimestamp(quote_timestamp, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    breadth_date = datetime.fromtimestamp(min(breadth_timestamps), ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    if quote_date != breadth_date or quote_date != now.strftime("%Y-%m-%d"):
        raise RuntimeError("Intraday quote and breadth dates do not match today")
    normal_count = sum(
        int(item.get("f104") or 0) + int(item.get("f105") or 0) + int(item.get("f106") or 0)
        for item in breadth
    )
    amount = safe_float(quote.get("f48"))
    close_raw = safe_float(quote.get("f43"))
    decimals = int(quote.get("f59") or 2)
    if amount is None or close_raw is None or normal_count <= 0:
        raise RuntimeError("Intraday values are incomplete")
    return {
        "date": quote_date,
        "close": close_raw / (10 ** decimals),
        "amount": amount,
        "per_company": amount / 100_000_000 / normal_count,
    }


def ohlc(values):
    return [round(values[0], 8), round(values[-1], 8), round(min(values), 8), round(max(values), 8)]


def replace_month(series, month, values, monthly_mean=None):
    if month in series["months"]:
        index = series["months"].index(month)
        series["values"][index] = values
    else:
        series["months"].append(month)
        series["values"].append(values)
        index = len(series["months"]) - 1
    if "monthlyMeans" in series:
        while len(series["monthlyMeans"]) < len(series["months"]):
            series["monthlyMeans"].append(None)
        series["monthlyMeans"][index] = monthly_mean


def main():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
    rows = fetch_current_month(now)
    latest_mode = "official"
    try:
        intraday = fetch_intraday(now)
        if intraday["date"] > rows[-1]["date"]:
            rows.append(intraday)
            latest_mode = "intraday"
    except Exception as exc:  # noqa: BLE001
        print(f"Intraday fallback skipped: {exc}")
    month = rows[-1]["date"][:7]
    series = {item["id"]: item for item in payload["series"]}
    closes = [item["close"] for item in rows]
    per_company = [item["per_company"] for item in rows]
    replace_month(series["all_a_close"], month, ohlc(closes))
    replace_month(
        series["all_a_per_company"],
        month,
        ohlc(per_company),
        round(sum(per_company) / len(per_company), 8),
    )
    payload.update({
        "schemaVersion": 3,
        "generatedAt": now.replace(microsecond=0).isoformat(),
        "currentMonth": month,
        "latestTradeDate": rows[-1]["date"],
        "latestMode": latest_mode,
        "source": (
            "东方财富妙想正式日线+公开实时行情"
            if latest_mode == "intraday"
            else "东方财富妙想全部A股正式日线"
        ),
    })
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"latestTradeDate": rows[-1]["date"], "latestMode": latest_mode, "rows": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
