from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).parent
OUTPUT = ROOT / "public" / "data.json"
STATE_PATH = ROOT / "state" / "current_month.json"
MEMBERS_PATH = ROOT / "market_members.json"
MIAOXIANG_URL = "https://ai-saas.eastmoney.com/proxy/b/mcp/tool/searchData"
ALL_A_ENTITY_TAG = {"entityId": "001071", "fullName": "全部A股", "classCode": "005202"}
SNAPSHOT_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SHANGHAI = ZoneInfo("Asia/Shanghai")
REQUIRED_IDS = {
    "all_a_close", "all_a_trade", "all_a_per_company",
    "broker_per_company", "broker_activity_multiple",
    "internet_per_company", "internet_activity_multiple",
    "gold_close", "gold_trade", "nonferrous_close", "nonferrous_trade",
    "stock_600362_qfq_close", "stock_300059_qfq_close",
    "stock_300033_qfq_close", "stock_600036_qfq_close",
}
QFQ_NAMES = {"600362": "江西铜业", "300059": "东方财富", "300033": "同花顺", "600036": "招商银行"}
BJ_NAMES = {"920576": "天力复合", "920634": "新威凌", "920068": "天工股份", "920078": "族兴新材", "920751": "惠同新材"}


def safe_float(value):
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def short_error(label, error):
    message = " ".join(str(error).split())
    return f"{label}:{message[:160]}"


def secid(code):
    if code == "800005":
        return "47.800005"
    return ("1." if code.startswith(("5", "6")) else "0.") + code


def chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def request_json(url, *, params=None, headers=None, timeout=25):
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_miaoxiang_bundle(now):
    api_key = (os.environ.get("EM_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("EM_API_KEY is not configured")
    month = now.strftime("%Y-%m")
    stock_names = "、".join(f"{name}{code}" for code, name in QFQ_NAMES.items())
    bj_names = "、".join(f"{name}{code}" for code, name in BJ_NAMES.items())
    query = (
        f"{month}-01至{now:%Y-%m-%d}：全部A股每日收盘价(算术平均)、成交额(合计)、"
        f"成交量(合计)、上涨家数、下跌家数、平盘家数；{stock_names}每日前复权收盘价；"
        f"{bj_names}每日收盘价、成交额、成交量"
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
        timeout=50,
    )
    response.raise_for_status()
    data = response.json().get("data") or {}
    result = data.get("searchDataResultDTO") or {}
    tables = result.get("dataTableDTOList") or data.get("dataTableDTOList") or []
    wanted = {
        "收盘价(算术平均)": "close", "成交额(合计)": "amount", "成交量(合计)": "volume",
        "上涨家数": "up", "下跌家数": "down", "平盘家数": "flat",
    }
    rows = {}
    stock_rows = defaultdict(dict)
    bj_rows = defaultdict(dict)
    for table in tables:
        raw = table.get("rawTable") or {}
        dates = raw.get("headName") or []
        entity_name = str(table.get("entityName") or table.get("title") or "")
        code_match = re.search(r"(\d{6})", entity_name)
        stock_code = code_match.group(1) if code_match and code_match.group(1) in QFQ_NAMES else None
        bj_code = code_match.group(1) if code_match and code_match.group(1) in BJ_NAMES else None
        for field_key, field_name in (table.get("nameMap") or {}).items():
            if stock_code and str(field_name) == "收盘价":
                values = raw.get(str(field_key)) or []
                for index, raw_date in enumerate(dates):
                    date_text = str(raw_date)[:10]
                    if date_text.startswith(month) and index < len(values):
                        value = safe_float(values[index])
                        if value is not None:
                            stock_rows[stock_code][date_text] = {"date": date_text, "close": value}
                continue
            if bj_code:
                normalized_name = str(field_name)
                output_name = None
                if normalized_name == "收盘价":
                    output_name = "close"
                elif normalized_name.startswith("成交额"):
                    output_name = "amount"
                elif normalized_name.startswith("成交量"):
                    output_name = "volume"
                if output_name:
                    values = raw.get(str(field_key)) or []
                    for index, raw_date in enumerate(dates):
                        date_text = str(raw_date)[:10]
                        if date_text.startswith(month) and index < len(values):
                            value = safe_float(values[index])
                            if value is not None:
                                bj_rows[bj_code].setdefault(date_text, {"date": date_text})[output_name] = value
                    continue
            output_name = wanted.get(str(field_name))
            if not output_name:
                continue
            values = raw.get(str(field_key)) or []
            for index, raw_date in enumerate(dates):
                date_text = str(raw_date)[:10]
                if not date_text.startswith(month) or index >= len(values):
                    continue
                value = safe_float(values[index])
                if value is not None:
                    rows.setdefault(date_text, {"date": date_text})[output_name] = value
    valid = []
    for row in rows.values():
        if not all(row.get(key) is not None for key in ("close", "amount", "volume", "up", "down", "flat")):
            continue
        normal_count = row["up"] + row["down"] + row["flat"]
        if normal_count <= 0 or row["volume"] <= 0:
            continue
        row["normal_count"] = normal_count
        row["per_company"] = row["amount"] / 100_000_000 / normal_count
        valid.append(row)
    if not valid:
        raise RuntimeError("妙想未返回完整全A日线")
    stock_output = {
        code: [items[date_text] for date_text in sorted(items)]
        for code, items in stock_rows.items() if items
    }
    bj_output = {
        code: [items[date_text] for date_text in sorted(items) if all(items[date_text].get(key) is not None for key in ("close", "amount", "volume"))]
        for code, items in bj_rows.items()
    }
    return sorted(valid, key=lambda item: item["date"]), stock_output, bj_output


def fetch_sse_month(code, month, end_day):
    payload = request_json(
        f"https://yunhq.sse.com.cn:32042/v1/sh1/dayk/{code}",
        params={
            "select": "date,open,high,low,close,volume,amount",
            "begin": month.replace("-", "") + "01",
            "end": end_day.replace("-", ""),
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"},
        timeout=20,
    )
    rows = []
    for item in payload.get("kline") or []:
        raw_date = str(item[0])
        rows.append({
            "date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}",
            "close": float(item[4]), "volume": float(item[5] or 0), "amount": float(item[6] or 0),
        })
    if not rows:
        raise RuntimeError("上交所本月日线为空")
    return rows


def fetch_szse_month(code, month):
    payload = request_json(
        "https://www.szse.cn/api/market/ssjjhq/getHistoryData",
        params={"cycleType": "32", "marketId": "1", "code": code},
        headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/market/product/stock/list/index.html",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=20,
    )
    rows = []
    for item in ((payload.get("data") or {}).get("picupdata") or []):
        if str(item[0]).startswith(month):
            rows.append({
                "date": item[0], "close": float(item[2]),
                "volume": float(item[7] or 0) * 100, "amount": float(item[8] or 0),
            })
    if not rows:
        raise RuntimeError("深交所本月日线为空")
    return rows


def fetch_exchange_month(code, month, end_day):
    last_error = None
    for attempt in range(3):
        try:
            if code.startswith("6"):
                return fetch_sse_month(code, month, end_day)
            if code.startswith(("0", "3")):
                return fetch_szse_month(code, month)
            raise RuntimeError("北交所由妙想统一补齐")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.6 + attempt * 0.8)
    raise RuntimeError(str(last_error))


def fetch_snapshots(codes, today, month):
    rows = {}
    failures = []
    fallback_codes = []
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    for code_group in chunks(sorted(codes), 55):
        payload = None
        last_error = None
        for attempt in range(2):
            try:
                payload = request_json(
                    SNAPSHOT_URL,
                    params={
                        "secids": ",".join(secid(code) for code in code_group),
                        "fields": "f2,f5,f6,f12,f14,f124",
                        "fltt": "2",
                    },
                    headers=headers,
                    timeout=20,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.6 + attempt)
        if payload is None:
            fallback_codes.extend(code_group)
            continue
        for item in ((payload.get("data") or {}).get("diff") or []):
            code = str(item.get("f12") or "")
            timestamp = int(item.get("f124") or 0)
            date_text = datetime.fromtimestamp(timestamp, SHANGHAI).strftime("%Y-%m-%d") if timestamp else ""
            close = safe_float(item.get("f2"))
            volume_lots = safe_float(item.get("f5"))
            amount = safe_float(item.get("f6"))
            if code in codes and date_text == today and close and volume_lots and amount and amount > 0:
                rows[code] = [{
                    "date": date_text,
                    "close": close,
                    "volume": volume_lots * 100,
                    "amount": amount,
                }]
        time.sleep(0.15)
    exchange_codes = [code for code in fallback_codes if code.startswith(("0", "3", "6"))]
    if exchange_codes:
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {pool.submit(fetch_exchange_month, code, month, today): code for code in exchange_codes}
            for future in as_completed(jobs):
                code = jobs[future]
                try:
                    rows[code] = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures.append(short_error(f"交易所备用{code}", exc))
    unresolved = [code for code in fallback_codes if not code.startswith("9") and code not in rows]
    if unresolved:
        failures.append(f"板块备用仍缺{len(unresolved)}只:{','.join(unresolved[:12])}")
    if not rows:
        raise RuntimeError("东方财富批量快照无当日数据")
    fallback_success_count = len([code for code in fallback_codes if code in rows])
    return rows, failures, fallback_success_count


def fetch_qfq_month(code, month, end_day):
    payload = request_json(
        KLINE_URL,
        params={
            "secid": secid(code), "klt": "101", "fqt": "1",
            "beg": month.replace("-", "") + "01", "end": end_day.replace("-", ""), "lmt": "40",
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=20,
    )
    rows = []
    for raw in ((payload.get("data") or {}).get("klines") or []):
        fields = raw.split(",")
        if fields[0].startswith(month):
            rows.append({"date": fields[0], "close": float(fields[2])})
    if not rows:
        raise RuntimeError(f"{code}前复权日线为空")
    return rows


def fetch_unadjusted_close_month(code, month, end_day):
    payload = request_json(
        KLINE_URL,
        params={
            "secid": secid(code), "klt": "101", "fqt": "0",
            "beg": month.replace("-", "") + "01", "end": end_day.replace("-", ""), "lmt": "40",
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=20,
    )
    rows = []
    for raw in ((payload.get("data") or {}).get("klines") or []):
        fields = raw.split(",")
        if fields[0].startswith(month):
            rows.append({"date": fields[0], "close": float(fields[2])})
    if not rows:
        raise RuntimeError(f"{code}不复权日线为空")
    return rows


def merge_row_list(existing, rows):
    merged = {item["date"]: item for item in existing}
    for item in rows:
        merged[item["date"]] = item
    return [merged[key] for key in sorted(merged)]


def reset_state_for_new_month(state, month):
    prior_close = dict(state.get("priorClose") or {})
    for code, rows in (state.get("stockRows") or {}).items():
        valid = [row for row in rows if safe_float(row.get("close"))]
        if valid:
            prior_close[code] = float(valid[-1]["close"])
    return {"month": month, "updatedAt": state.get("updatedAt"), "stockRows": {}, "priorClose": prior_close, "allARows": [], "qfqRows": {}}


def all_a_daily(rows):
    output = {}
    cumulative_amount = 0.0
    cumulative_volume = 0.0
    for row in sorted(rows, key=lambda item: item["date"]):
        amount = safe_float(row.get("amount"))
        volume = safe_float(row.get("volume"))
        if amount is None or volume is None or volume <= 0:
            continue
        cumulative_amount += amount
        cumulative_volume += volume
        output[row["date"]] = {
            "close": float(row["close"]),
            "trade": cumulative_amount / cumulative_volume,
            "per_company": float(row["per_company"]),
        }
    return output


def sector_price_daily(codes, stock_rows, prior_close):
    events = defaultdict(list)
    for code in codes:
        for row in stock_rows.get(code) or []:
            events[row["date"]].append((code, row))
    last_close = {code: float(value) for code, value in prior_close.items() if code in codes and safe_float(value)}
    cumulative_amount = 0.0
    cumulative_volume = 0.0
    output = {}
    for date_text in sorted(events):
        day_amount = 0.0
        day_volume = 0.0
        for code, row in events[date_text]:
            close = safe_float(row.get("close"))
            volume = safe_float(row.get("volume"))
            amount = safe_float(row.get("amount"))
            if close and close > 0:
                last_close[code] = close
            if amount and volume and amount > 0 and volume > 0:
                day_amount += amount
                day_volume += volume
        cumulative_amount += day_amount
        cumulative_volume += day_volume
        if last_close and cumulative_volume > 0:
            output[date_text] = {
                "close": sum(last_close.values()) / len(last_close),
                "trade": cumulative_amount / cumulative_volume,
            }
    return output


def sector_turnover_daily(members, stock_rows):
    totals = defaultdict(float)
    counts = defaultdict(int)
    for member in members:
        code = member["code"]
        start = member["start"]
        end = member.get("end")
        for row in stock_rows.get(code) or []:
            date_text = row["date"]
            amount = safe_float(row.get("amount"))
            if date_text < start or (end and date_text > end) or not amount or amount <= 0:
                continue
            totals[date_text] += amount
            counts[date_text] += 1
    return {
        date_text: {"per_company": totals[date_text] / 100_000_000 / counts[date_text]}
        for date_text in sorted(totals) if counts[date_text] > 0
    }


def relative_daily(numerator, denominator_daily):
    output = {}
    for date_text, item in numerator.items():
        denominator = safe_float((denominator_daily.get(date_text) or {}).get("per_company"))
        if denominator and denominator > 0:
            output[date_text] = {"multiple": item["per_company"] / denominator}
    return output


def qfq_daily(rows):
    return {item["date"]: {"close": float(item["close"])} for item in rows if safe_float(item.get("close"))}


def monthly_ohlc(daily, key):
    values = [float(item[key]) for _, item in sorted(daily.items()) if safe_float(item.get(key)) is not None]
    if not values:
        return None
    return [round(values[0], 8), round(values[-1], 8), round(min(values), 8), round(max(values), 8)]


def monthly_mean(daily, key):
    values = [float(item[key]) for _, item in sorted(daily.items()) if safe_float(item.get(key)) is not None]
    return round(sum(values) / len(values), 8) if values else None


def ensure_month(payload, month):
    if month in payload["months"]:
        return payload["months"].index(month)
    payload["months"].append(month)
    for series in payload["series"]:
        series["values"].append(None)
        if "monthlyMeans" in series:
            series["monthlyMeans"].append(None)
    return len(payload["months"]) - 1


def set_month(series, index, month, value, mean=None):
    if value is None:
        return
    series["values"][index] = value
    if "monthlyMeans" in series:
        while len(series["monthlyMeans"]) < len(series["values"]):
            series["monthlyMeans"].append(None)
        series["monthlyMeans"][index] = mean
    if not series.get("firstMonth"):
        series["firstMonth"] = month
    series["lastMonth"] = month


def validate(payload):
    if payload.get("schemaVersion") != 4 or len(payload.get("series") or []) != 15:
        raise RuntimeError("输出不是十五项schemaVersion 4")
    ids = {item.get("id") for item in payload["series"]}
    if ids != REQUIRED_IDS:
        raise RuntimeError(f"指标集合不一致: {sorted(REQUIRED_IDS - ids)}")
    count = len(payload["months"])
    for item in payload["series"]:
        if len(item.get("values") or []) != count:
            raise RuntimeError(f"{item['id']}与月份轴未对齐")
        if "monthlyMeans" in item and len(item["monthlyMeans"]) != count:
            raise RuntimeError(f"{item['id']}月均数组未对齐")


def main():
    now = datetime.now(SHANGHAI)
    today = now.strftime("%Y-%m-%d")
    month = today[:7]
    payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    members = json.loads(MEMBERS_PATH.read_text(encoding="utf-8"))
    if state.get("month") != month:
        state = reset_state_for_new_month(state, month)

    failures = []
    bundle_qfq = {}
    bundle_bj = {}
    try:
        state["allARows"], bundle_qfq, bundle_bj = fetch_miaoxiang_bundle(now)
    except Exception as exc:  # noqa: BLE001
        failures.append(short_error("妙想全A与个股", exc))

    active_broker = [item for item in members["broker"] if not item.get("end") or item["end"] >= f"{month}-01"]
    market_codes = {"800005"} | set(members["gold"]) | set(members["nonferrous"])
    market_codes |= {item["code"] for item in active_broker} | {item["code"] for item in members["internet"]}
    exchange_fallback_count = 0
    try:
        snapshots, snapshot_failures, exchange_fallback_count = fetch_snapshots(market_codes, today, month)
        failures.extend(snapshot_failures)
        for code, rows in snapshots.items():
            state["stockRows"][code] = merge_row_list(state["stockRows"].get(code) or [], rows)
    except Exception as exc:  # noqa: BLE001
        failures.append(short_error("板块快照", exc))
    for code, rows in bundle_bj.items():
        if rows:
            state["stockRows"][code] = merge_row_list(state["stockRows"].get(code) or [], rows)

    try:
        official_800005 = fetch_unadjusted_close_month("800005", month, today)
        state["stockRows"]["800005"] = merge_row_list(state["stockRows"].get("800005") or [], official_800005)
    except Exception as exc:  # noqa: BLE001
        failures.append(short_error("800005官方日线", exc))

    for code in members["qfq"]:
        if bundle_qfq.get(code):
            state["qfqRows"][code] = bundle_qfq[code]
            continue
        try:
            state["qfqRows"][code] = fetch_qfq_month(code, month, today)
        except Exception as exc:  # noqa: BLE001
            failures.append(short_error(f"{code}前复权备用", exc))

    all_daily = all_a_daily(state.get("allARows") or [])
    all_a_close_daily = {
        row["date"]: {"close": float(row["close"])}
        for row in state["stockRows"].get("800005", [])
        if safe_float(row.get("close")) is not None
    }
    all_a_close_missing_dates = sorted(set(all_daily) - set(all_a_close_daily))
    gold_daily = sector_price_daily(members["gold"], state["stockRows"], state.get("priorClose") or {})
    nonferrous_daily = sector_price_daily(members["nonferrous"], state["stockRows"], state.get("priorClose") or {})
    broker_daily = sector_turnover_daily(members["broker"], state["stockRows"])
    internet_daily = sector_turnover_daily(members["internet"], state["stockRows"])
    broker_relative = relative_daily(broker_daily, all_daily)
    internet_relative = relative_daily(internet_daily, broker_daily)
    qfq_maps = {code: qfq_daily(state["qfqRows"].get(code) or []) for code in members["qfq"]}

    maps = {
        "all_a_close": monthly_ohlc(all_a_close_daily, "close"),
        "all_a_trade": monthly_ohlc(all_daily, "trade"),
        "all_a_per_company": monthly_ohlc(all_daily, "per_company"),
        "broker_per_company": monthly_ohlc(broker_daily, "per_company"),
        "broker_activity_multiple": monthly_ohlc(broker_relative, "multiple"),
        "internet_per_company": monthly_ohlc(internet_daily, "per_company"),
        "internet_activity_multiple": monthly_ohlc(internet_relative, "multiple"),
        "gold_close": monthly_ohlc(gold_daily, "close"),
        "gold_trade": monthly_ohlc(gold_daily, "trade"),
        "nonferrous_close": monthly_ohlc(nonferrous_daily, "close"),
        "nonferrous_trade": monthly_ohlc(nonferrous_daily, "trade"),
        **{f"stock_{code}_qfq_close": monthly_ohlc(qfq_maps[code], "close") for code in members["qfq"]},
    }
    means = {
        "all_a_per_company": monthly_mean(all_daily, "per_company"),
        "broker_per_company": monthly_mean(broker_daily, "per_company"),
        "internet_per_company": monthly_mean(internet_daily, "per_company"),
    }
    index = ensure_month(payload, month)
    series_by_id = {item["id"]: item for item in payload["series"]}
    for series_id, value in maps.items():
        set_month(series_by_id[series_id], index, month, value, means.get(series_id))

    date_sets = [set(source) for source in (all_a_close_daily, all_daily, gold_daily, nonferrous_daily, broker_daily, internet_daily)]
    date_sets.extend(set(qfq_maps[code]) for code in members["qfq"])
    common_dates = set.intersection(*date_sets) if date_sets and all(date_sets) else set()
    latest_complete = max(common_dates) if common_dates else payload.get("latestTradeDate")
    if all_a_close_missing_dates:
        before_gap = [date_text for date_text in common_dates if date_text < all_a_close_missing_dates[0]]
        latest_complete = max(before_gap) if before_gap else payload.get("latestTradeDate")
    today_snapshot_count = len([
        code for code in market_codes
        if state["stockRows"].get(code) and state["stockRows"][code][-1]["date"] == today
    ])
    if latest_complete == today and today_snapshot_count < len(market_codes) * 0.95:
        earlier = [date_text for date_text in common_dates if date_text < today]
        latest_complete = max(earlier) if earlier else payload.get("latestTradeDate")
    state["updatedAt"] = now.replace(microsecond=0).isoformat()
    payload.update({
        "schemaVersion": 4,
        "generatedAt": now.replace(microsecond=0).isoformat(),
        "currentMonth": month,
        "latestTradeDate": latest_complete,
        "source": "800005官方序列+东方财富妙想全A成交与广度汇总+东方财富板块快照与前复权日线",
        "quality": {
            "completeThrough": latest_complete,
            "allACloseThrough": max(all_a_close_daily, default=None),
            "allACloseMissingDates": all_a_close_missing_dates,
            "stockSnapshotCount": today_snapshot_count,
            "expectedSnapshotCount": len(market_codes),
            "miaoxiangQfqCount": len(bundle_qfq),
            "miaoxiangBjCount": len([code for code, rows in bundle_bj.items() if rows]),
            "exchangeFallbackCount": exchange_fallback_count,
            "failureCount": len(failures),
            "failures": failures[:12],
            "note": "800005只接受代码47.800005官方序列，全部A股算术平均收盘价不得替代；云端保留当月逐日原始值后重算月K，接口失败时不清空上次正确值。",
        },
    })
    validate(payload)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({
        "latestTradeDate": latest_complete,
        "currentMonth": month,
        "series": len(payload["series"]),
        "failures": failures[:12],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
