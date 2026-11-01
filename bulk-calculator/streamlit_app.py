from __future__ import annotations

import time
from collections import defaultdict

import pandas as pd
import requests
import streamlit as st


MS_API = "https://api.moysklad.ru/api/remap/1.2"
OZON_API = "https://api-seller.ozon.ru"
TIMEOUT = 60

st.set_page_config(page_title="Расчёт позиций Ozon", page_icon="📦", layout="wide")
st.title("Массовый расчёт юнит-экономики")
st.caption("Данные товаров из Ozon + средняя себестоимость по 5 последним приёмкам из МоегоСклада")


def secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "")).strip()
    except (FileNotFoundError, KeyError):
        return ""


def api_request(method: str, url: str, *, headers: dict, payload: dict | None = None, params: dict | None = None) -> dict:
    last_error = None
    for attempt in range(3):
        try:
            response = requests.request(method, url, headers=headers, json=payload, params=params, timeout=TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(min(2 ** attempt, 8))
                    continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"Ошибка запроса к API: {last_error}") from last_error


def norm(value) -> str:
    if value is None or pd.isna(value):
        return ""
    result = str(value).strip()
    if result.endswith(".0") and result[:-2].isdigit():
        result = result[:-2]
    return result


def pages_moysklad(token: str, entity: str):
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    offset = 0
    while True:
        data = api_request(
            "GET", f"{MS_API}/entity/{entity}", headers=headers,
            params={"filter": "applicable=true", "limit": 100, "offset": offset,
                    "order": "moment,desc", "expand": "positions.assortment"},
        )
        rows = data.get("rows", [])
        yield from rows
        if len(rows) < 100:
            break
        offset += len(rows)


def fetch_last_receipt_costs(token: str, limit_per_product: int = 5) -> pd.DataFrame:
    # Only posted supplier receipts (Приёмка), not inventory adjustments.
    receipts = []
    for doc in pages_moysklad(token, "supply"):
        moment = doc.get("moment") or doc.get("created") or ""
        positions = doc.get("positions") or {}
        rows = positions.get("rows", []) if isinstance(positions, dict) else positions
        # The same product can occur on multiple lines in one receipt; consolidate it.
        per_receipt = defaultdict(lambda: {"name": "", "value": 0.0, "qty": 0.0})
        for pos in rows:
            assortment = pos.get("assortment") or {}
            code = norm(assortment.get("code"))
            try:
                qty, price = float(pos.get("quantity", 0)), float(pos.get("price", 0))
            except (TypeError, ValueError):
                continue
            if not code or qty <= 0 or price < 0:
                continue
            per_receipt[code]["name"] = assortment.get("name", "")
            per_receipt[code]["value"] += price * qty / 100.0  # API monetary amounts are kopecks.
            per_receipt[code]["qty"] += qty
        for code, item in per_receipt.items():
            if item["qty"]:
                receipts.append({"code": code, "name": item["name"], "moment": moment,
                                 "value": item["value"], "qty": item["qty"]})

    by_code = defaultdict(list)
    for row in receipts:
        by_code[row["code"]].append(row)
    result = []
    for code, rows in by_code.items():
        rows.sort(key=lambda x: x["moment"], reverse=True)
        latest = rows[:limit_per_product]
        qty = sum(x["qty"] for x in latest)
        value = sum(x["value"] for x in latest)
        result.append({"Код товара": code, "Наименование МС": latest[0]["name"],
                       "Приёмок учтено": len(latest), "Себестоимость": value / qty if qty else None,
                       "Дата последней приёмки": latest[0]["moment"]})
    return pd.DataFrame(result)


def ozon_post(client_id: str, api_key: str, path: str, payload: dict) -> dict:
    return api_request("POST", f"{OZON_API}{path}", headers={
        "Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"
    }, payload=payload)


def fetch_ozon_prices(client_id: str, api_key: str) -> pd.DataFrame:
    cursor = ""
    all_rows = []
    while True:
        data = ozon_post(client_id, api_key, "/v5/product/info/prices", {
            "cursor": cursor, "filter": {"offer_id": [], "visibility": "ALL"}, "limit": 100,
        })
        rows = data.get("items", [])
        all_rows.extend(rows)
        next_cursor = data.get("cursor", "")
        if not rows or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    result = []
    for item in all_rows:
        p = item.get("price") or {}
        result.append({
            "Артикул": norm(item.get("offer_id")), "SKU Ozon": item.get("product_id"),
            "Цена продавца": p.get("price"), "Предельная цена Ozon": p.get("min_price"),
            "Цена до скидок": p.get("old_price"), "Цена для покупателя": p.get("marketing_price"),
            "Объёмный вес, кг": item.get("volume_weight"),
        })
    return pd.DataFrame(result)


def fetch_ozon_dimensions(client_id: str, api_key: str, offer_ids: list[str]) -> pd.DataFrame:
    results = []
    for start in range(0, len(offer_ids), 1000):
        ids = offer_ids[start:start + 1000]
        last_id = ""
        while True:
            data = ozon_post(client_id, api_key, "/v4/product/info/attributes", {
                "filter": {"offer_id": ids, "visibility": "ALL"}, "last_id": last_id, "limit": 1000,
            })
            block = data.get("result", [])
            if isinstance(block, dict):
                block = block.get("items", [])
            results.extend(block)
            next_id = data.get("last_id", "")
            if not block or not next_id or next_id == last_id:
                break
            last_id = next_id
    result = []
    for item in results:
        result.append({
            "Артикул": norm(item.get("offer_id")), "Высота": item.get("height"),
            "Ширина": item.get("width"), "Глубина": item.get("depth"),
            "Единица габаритов": item.get("dimension_unit"),
            "Вес, значение": item.get("weight"), "Единица веса": item.get("weight_unit"),
        })
    return pd.DataFrame(result)


def calculate(frame: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = frame.copy()
    price_col = cfg["price_col"]
    price = pd.to_numeric(out[price_col], errors="coerce").fillna(0)
    cost = pd.to_numeric(out["Себестоимость"], errors="coerce")
    buyout = max(cfg["buyout_pct"] / 100.0, 0.01)
    delivery = cfg["delivery_rub"] / buyout
    fulfillment = cfg["fulfillment_rub"] / buyout
    returns = cfg["return_rub"] * (1.0 - buyout) / buyout
    percentage_cost = (cfg["commission_pct"] + cfg["acquiring_pct"] + cfg["advertising_pct"] + cfg["tax_pct"]) / 100.0
    expenses = cost + price * percentage_cost + delivery + fulfillment + returns + cfg["other_rub"]
    out["Используемая цена"] = price
    out["Расходы Ozon и прочие"] = expenses - cost
    out["Прибыль на единицу"] = price - expenses
    out["Маржа, %"] = ((price - expenses) / price.replace(0, float("nan")) * 100).round(2)
    out["ROI к себестоимости, %"] = ((price - expenses) / cost.replace(0, float("nan")) * 100).round(2)
    out.loc[cost.isna(), ["Прибыль на единицу", "Маржа, %", "ROI к себестоимости, %"]] = pd.NA
    return out


with st.sidebar:
    st.header("Подключения")
    ms_token = st.text_input("Токен API МоегоСклада", value=secret("MOYSKLAD_TOKEN"), type="password")
    ozon_client = st.text_input("Client-Id Ozon", value=secret("OZON_CLIENT_ID"))
    ozon_key = st.text_input("Api-Key Ozon", value=secret("OZON_API_KEY"), type="password")
    st.caption("Ключи не сохраняются в проект и используются только для запросов API.")

st.subheader("Ручные параметры расчёта")
left, right, extra = st.columns(3)
with left:
    price_col = st.selectbox("Цена из Ozon", ["Предельная цена Ozon", "Цена продавца", "Цена до скидок", "Цена для покупателя"])
    commission_pct = st.number_input("Комиссия Ozon, %", min_value=0.0, value=20.0, step=0.5)
    acquiring_pct = st.number_input("Эквайринг, %", min_value=0.0, value=1.5, step=0.1)
with right:
    buyout_pct = st.number_input("Выкуп, %", min_value=1.0, max_value=100.0, value=95.0, step=1.0)
    advertising_pct = st.number_input("Реклама, % от цены", min_value=0.0, value=0.0, step=0.5)
    tax_pct = st.number_input("Налог, % от цены", min_value=0.0, value=6.0, step=0.5)
with extra:
    delivery_rub = st.number_input("Логистика до покупателя, ₽/выкуп", min_value=0.0, value=0.0, step=5.0)
    fulfillment_rub = st.number_input("Обработка/приёмка, ₽/выкуп", min_value=0.0, value=0.0, step=5.0)
    return_rub = st.number_input("Расходы при невыкупе, ₽", min_value=0.0, value=0.0, step=5.0)
    other_rub = st.number_input("Прочие расходы, ₽/единицу", min_value=0.0, value=0.0, step=5.0)

with st.expander("Предупреждение о модели"):
    st.write("Расчёт применяет указанные вручную общие тарифы ко всем позициям. Это массовая модель юнит-экономики, а не полная копия всех динамических правил исходного Ozon-калькулятора. Проверьте валюту и тарифы перед использованием результатов.")

if st.button("Загрузить товары и рассчитать", type="primary", use_container_width=True):
    if not (ms_token and ozon_client and ozon_key):
        st.error("Заполните три поля API в боковой панели.")
    else:
        try:
            with st.spinner("Загружаю каталог, цены, габариты и приёмки…"):
                prices = fetch_ozon_prices(ozon_client, ozon_key)
                if prices.empty:
                    raise RuntimeError("Ozon API не вернул товары. Проверьте доступ API и наличие товаров в кабинете.")
                dims = fetch_ozon_dimensions(ozon_client, ozon_key, prices["Артикул"].dropna().astype(str).unique().tolist())
                costs = fetch_last_receipt_costs(ms_token, 5)
                merged = prices.merge(dims, on="Артикул", how="left", suffixes=("", "_габариты"))
                merged = merged.merge(costs, left_on="Артикул", right_on="Код товара", how="left")
                merged["Сопоставление"] = merged["Себестоимость"].apply(lambda x: "Найдено" if pd.notna(x) else "Нет 5 приёмок/совпадения кода")
                cfg = {"price_col": price_col, "commission_pct": commission_pct, "acquiring_pct": acquiring_pct,
                       "buyout_pct": buyout_pct, "advertising_pct": advertising_pct, "tax_pct": tax_pct,
                       "delivery_rub": delivery_rub, "fulfillment_rub": fulfillment_rub,
                       "return_rub": return_rub, "other_rub": other_rub}
                report = calculate(merged, cfg)
                st.session_state["bulk_report"] = report
            st.success(f"Загружено позиций: {len(report):,}".replace(",", " "))
        except Exception as exc:
            st.error(f"Не удалось получить данные: {exc}")

report = st.session_state.get("bulk_report")
if report is not None:
    missing_cost = int(report["Себестоимость"].isna().sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Позиций", f"{len(report):,}".replace(",", " "))
    c2.metric("Без себестоимости", missing_cost)
    good = report["Прибыль на единицу"].dropna()
    c3.metric("Средняя прибыль/шт", f"{good.mean():,.2f} ₽".replace(",", " ") if not good.empty else "—")
    st.dataframe(report, use_container_width=True, hide_index=True)
    csv = report.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("Скачать результат CSV для Excel", csv, "unit_economics_ozon.csv", "text/csv")
else:
    st.info("Заполните API-доступы и нажмите «Загрузить товары и рассчитать».")

