import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import streamlit as st

OZON_URL = "https://api-seller.ozon.ru"
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"
REQUEST_TIMEOUT = 60

st.set_page_config(page_title="Ozon + МойСклад — аналитика", layout="wide", page_icon="📊")
st.title("Аналитика продаж Ozon и себестоимости МоегоСклада")
st.caption("Сопоставление выполняется по offer_id Ozon и коду товара в МоемСкладе. Себестоимость — средневзвешенная по проведённым документам «Приёмка» и «Оприходование» за всю доступную историю.")


def ozon_headers(client_id: str, api_key: str) -> dict:
    return {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"}


def get_streamlit_secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "")).strip()
    except (FileNotFoundError, KeyError):
        # A local secrets.toml is optional; cloud secrets are configured in app settings.
        return ""


def request_json(
    method: str,
    url: str,
    *,
    headers: dict,
    payload: dict | None = None,
    params: dict | None = None,
) -> dict:
    try:
        response = requests.request(method, url, headers=headers, json=payload, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Не удалось подключиться к {url}: {exc}") from exc
    if not response.ok:
        detail = response.text[:500].strip()
        raise RuntimeError(f"API вернул {response.status_code}: {detail or response.reason}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API вернул ответ не в формате JSON.") from exc


def fetch_ozon_sales(
    client_id: str, api_key: str, date_from: date, date_to: date
) -> tuple[pd.DataFrame, bool, bool]:
    payload = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "metrics": ["revenue", "ordered_units", "delivered_units", "returns"],
        "dimension": ["sku"],
        "limit": 1000,
        "offset": 0,
    }
    units_available = True
    try:
        response = request_json("POST", f"{OZON_URL}/v1/analytics/data", headers=ozon_headers(client_id, api_key), payload=payload)
    except RuntimeError as exc:
        # Ozon restricts delivered/returned metrics for some account plans.
        # Keep the order data, but never substitute orders for delivered units.
        detail = str(exc).lower()
        if not any(field in detail for field in ("delivered_units", "returns", "metric", "premium")):
            raise
        units_available = False
        payload["metrics"] = ["revenue", "ordered_units"]
        response = request_json("POST", f"{OZON_URL}/v1/analytics/data", headers=ozon_headers(client_id, api_key), payload=payload)
    result = response.get("result") or {}
    rows = []
    for item in result.get("data", []):
        dimensions = item.get("dimensions") or []
        metrics = item.get("metrics") or []
        if dimensions:
            rows.append({
                "sku": str(dimensions[0].get("id", "")),
                "name": dimensions[0].get("name", ""),
                # Analytics revenue is an order metric. Keep it for comparison,
                # but use finance accruals for the unit-economics calculation.
                "ordered_revenue": float(metrics[0]) if metrics else 0.0,
                "ordered_units": float(metrics[1]) if len(metrics) > 1 else 0.0,
                "delivered_units": float(metrics[2]) if units_available and len(metrics) > 2 else None,
                "returned_units": float(metrics[3]) if units_available and len(metrics) > 3 else None,
            })
    # Ozon currently applies a strict request limit to this endpoint. Do not silently
    # fetch a second page and risk turning a normal report into a rate-limit failure.
    return (
        pd.DataFrame(rows, columns=["sku", "name", "ordered_revenue", "ordered_units", "delivered_units", "returned_units"]),
        bool(result.get("has_next", False)),
        units_available,
    )


def fetch_ozon_sku_to_offer(client_id: str, api_key: str, skus: list[str]) -> pd.DataFrame:
    headers = ozon_headers(client_id, api_key)
    mapping = {}
    numeric_skus = [int(sku) for sku in skus if sku.isdigit()]
    for start in range(0, len(numeric_skus), 100):
        payload = {"sku": numeric_skus[start:start + 100]}
        response = request_json("POST", f"{OZON_URL}/v3/product/info/list", headers=headers, payload=payload)
        for item in response.get("items", []):
            sku = str(item.get("sku", ""))
            offer_id = item.get("offer_id")
            if sku and offer_id:
                mapping[sku] = str(offer_id).strip()
        if start + 100 < len(numeric_skus):
            time.sleep(0.25)
    return pd.DataFrame([{"sku": sku, "offer_id": offer} for sku, offer in mapping.items()])


def fetch_ozon_accruals(
    client_id: str, api_key: str, date_from: date, date_to: date
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    headers = ozon_headers(client_id, api_key)
    rows = []
    sku_rows = {}
    unallocated_rows = []

    def amount(value) -> float:
        if isinstance(value, dict):
            value = value.get("amount")
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    current_date = date_from
    while current_date <= date_to:
        last_id = ""
        seen_cursors = set()
        for _ in range(200):
            response = request_json(
                "POST",
                f"{OZON_URL}/v1/finance/accrual/by-day",
                headers=headers,
                payload={"date": current_date.isoformat(), "last_id": last_id},
            )
            batch = response.get("accruals") or []
            for item in batch:
                total = item.get("total_amount") or {}
                posting = item.get("posting") or {}
                for product in posting.get("products") or []:
                    commission = product.get("commission") or {}
                    sku = product.get("sku")
                    if sku is None:
                        continue
                    sku = str(sku)
                    row = sku_rows.setdefault(sku, {
                        "sku": sku, "sales_revenue": 0.0, "bonus_amount": 0.0,
                        "partner_programs": 0.0, "sale_commission": 0.0,
                        "delivery_fees": 0.0, "item_fees": 0.0,
                        "seller_price_fallback": 0.0, "has_finance": True,
                    })
                    row["sales_revenue"] += amount(commission.get("sale_amount"))
                    row["bonus_amount"] += amount(commission.get("bonus"))
                    row["partner_programs"] += amount(commission.get("coinvestment"))
                    row["sale_commission"] += amount(commission.get("sale_commission"))
                    row["seller_price_fallback"] += amount(commission.get("seller_price"))

                    delivery = product.get("delivery") or {}
                    delivery_total = delivery.get("total_accrued")
                    if delivery_total is not None:
                        row["delivery_fees"] += amount(delivery_total)
                    else:
                        for service in delivery.get("services") or []:
                            row["delivery_fees"] += amount(service.get("accrued"))

                # Item fees are already grouped by SKU by Ozon. Preserve the
                # returned signs so charges reduce period profit.
                for group in (item.get("item_fees") or {}).get("fees") or []:
                    sku = group.get("sku")
                    if sku is None:
                        continue
                    sku = str(sku)
                    row = sku_rows.setdefault(sku, {
                        "sku": sku, "sales_revenue": 0.0, "bonus_amount": 0.0,
                        "partner_programs": 0.0, "sale_commission": 0.0,
                        "delivery_fees": 0.0, "item_fees": 0.0,
                        "seller_price_fallback": 0.0, "has_finance": True,
                    })
                    for fee in group.get("fees") or []:
                        row["item_fees"] += amount(fee.get("accrued"))

                non_item = item.get("non_item_fee") or {}
                if non_item:
                    unallocated_rows.append({
                        "date": item.get("date") or current_date.isoformat(),
                        "type_id": non_item.get("type_id", ""),
                        "amount": amount(non_item.get("accrued")),
                    })
                rows.append({
                    "date": item.get("date") or current_date.isoformat(),
                    "accrued_category": item.get("accrued_category", ""),
                    "type_id": item.get("type_id", ""),
                    "unit_number": item.get("unit_number", ""),
                    "amount": amount(total),
                    "currency": total.get("currency", ""),
                })
            next_id = response.get("last_id") or ""
            if not batch or not next_id:
                break
            if next_id == last_id or next_id in seen_cursors:
                raise RuntimeError(f"Ozon вернул повторяющийся курсор начислений за {current_date.isoformat()}.")
            seen_cursors.add(next_id)
            last_id = next_id
            time.sleep(0.25)
        else:
            raise RuntimeError(f"Слишком много страниц начислений за {current_date.isoformat()}.")
        current_date += timedelta(days=1)
        time.sleep(0.25)
    accruals = pd.DataFrame(rows, columns=["date", "accrued_category", "type_id", "unit_number", "amount", "currency"])
    sku_finance = pd.DataFrame(sku_rows.values())
    if not sku_finance.empty:
        fallback = sku_finance["sales_revenue"].eq(0) & sku_finance["seller_price_fallback"].ne(0)
        sku_finance.loc[fallback, "sales_revenue"] = (
            sku_finance.loc[fallback, "seller_price_fallback"]
            - sku_finance.loc[fallback, "bonus_amount"]
            - sku_finance.loc[fallback, "partner_programs"]
        )
        sku_finance = sku_finance.drop(columns=["seller_price_fallback"])
    unallocated = pd.DataFrame(unallocated_rows, columns=["date", "type_id", "amount"])
    return accruals, sku_finance, unallocated


def fetch_moysklad_costs(token: str) -> pd.DataFrame:
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    stock_response = request_json("GET", f"{MOYSKLAD_URL}/report/stock/all", headers=headers)
    stock_rows = []
    for item in stock_response.get("rows", []):
        code = item.get("code") or item.get("article") or item.get("productCode") or item.get("modificationCode")
        if not code:
            continue
        stock = pd.to_numeric(item.get("stock"), errors="coerce")
        stock_rows.append({
            "ms_code": str(code).strip(),
            "ms_name": item.get("name", ""),
            "row_stock": float(stock) if pd.notna(stock) else 0.0,
        })
    stocks = pd.DataFrame(stock_rows, columns=["ms_code", "ms_name", "row_stock"])
    if not stocks.empty:
        stocks = stocks.groupby("ms_code", as_index=False).agg(
            ms_name=("ms_name", "first"), stock=("row_stock", "sum")
        )

    # Average the unit prices from all posted receipts and inventory receipts.
    # Prices in MoySklad document positions are in kopecks, and quantity-weighting
    # avoids treating a one-unit purchase like a large replenishment.
    history_rows = []
    for entity in ("supply", "enter"):
        offset = 0
        while True:
            response = request_json(
                "GET",
                f"{MOYSKLAD_URL}/entity/{entity}",
                headers=headers,
                params={
                    "filter": "applicable=true",
                    "limit": 100,
                    "offset": offset,
                    "expand": "positions.assortment",
                },
            )
            documents = response.get("rows", [])
            for document in documents:
                if document.get("applicable") is not True:
                    continue
                position_collection = document.get("positions") or {}
                positions = position_collection.get("rows", []) if isinstance(position_collection, dict) else position_collection
                for position in positions:
                    assortment = position.get("assortment") or {}
                    code = assortment.get("code") or assortment.get("article")
                    quantity = pd.to_numeric(position.get("quantity"), errors="coerce")
                    price = pd.to_numeric(position.get("price"), errors="coerce")
                    if not code or pd.isna(quantity) or quantity <= 0 or pd.isna(price) or price < 0:
                        continue
                    history_rows.append({
                        "ms_code": str(code).strip(),
                        "ms_name": assortment.get("name", ""),
                        "receipt_value": float(price) * float(quantity) / 100,
                        "receipt_quantity": float(quantity),
                    })
            if len(documents) < 100:
                break
            offset += len(documents)

    history = pd.DataFrame(history_rows)
    if history.empty:
        costs = pd.DataFrame(columns=["ms_code", "ms_name", "cost_price"])
    else:
        costs = history.groupby("ms_code", as_index=False).agg(
            ms_name=("ms_name", "first"),
            receipt_value=("receipt_value", "sum"),
            receipt_quantity=("receipt_quantity", "sum"),
        )
        costs["cost_price"] = costs["receipt_value"] / costs["receipt_quantity"]
        costs = costs.drop(columns=["receipt_value", "receipt_quantity"])

    if not stocks.empty:
        costs = costs.merge(stocks, on="ms_code", how="outer", suffixes=("", "_stock"))
        costs["ms_name"] = costs["ms_name"].fillna(costs.get("ms_name_stock"))
        costs = costs.drop(columns=["ms_name_stock"], errors="ignore")
    else:
        costs["stock"] = 0.0
    return costs


def validate_period(start: date, end: date) -> None:
    if start > end:
        raise ValueError("Дата начала периода позже даты окончания.")
    if (end - start).days > 365:
        raise ValueError("Выберите период не длиннее одного года.")


with st.sidebar:
    st.header("Подключение")
    secret_client_id = get_streamlit_secret("OZON_CLIENT_ID")
    secret_api_key = get_streamlit_secret("OZON_API_KEY")
    secret_ms_token = get_streamlit_secret("MOYSKLAD_TOKEN")
    if any((secret_client_id, secret_api_key, secret_ms_token)):
        st.caption("Секреты из настроек Streamlit используются автоматически.")
    client_id = secret_client_id or st.text_input("Ozon Client-Id", type="password")
    api_key = secret_api_key or st.text_input("Ozon Api-Key", type="password")
    ms_token = secret_ms_token or st.text_input("Токен МоегоСклада", type="password")
    st.header("Период")
    default_end = date.today()
    default_start = default_end - timedelta(days=30)
    date_from = st.date_input("С", default_start)
    date_to = st.date_input("По", default_end)
    run = st.button("Собрать аналитику", type="primary", use_container_width=True)

if run:
    if not all((client_id, api_key, ms_token)):
        st.error("Заполните все три ключа доступа.")
    else:
        try:
            validate_period(date_from, date_to)
            progress = st.progress(0, text="Загружаем продажи Ozon…")
            sales, more_sales, units_available = fetch_ozon_sales(client_id, api_key, date_from, date_to)
            progress.progress(20, text="Загружаем себестоимость из МоегоСклада…")
            costs = fetch_moysklad_costs(ms_token)
            progress.progress(45, text="Загружаем финансовые операции Ozon…")
            accrual_error = None
            accruals_loaded = False
            finance_columns = [
                "sku", "sales_revenue", "bonus_amount", "partner_programs",
                "sale_commission", "delivery_fees", "item_fees", "has_finance",
            ]
            sku_finance = pd.DataFrame(columns=finance_columns)
            unallocated_fees = pd.DataFrame(columns=["date", "type_id", "amount"])
            try:
                accruals, sku_finance, unallocated_fees = fetch_ozon_accruals(
                    client_id, api_key, date_from, date_to
                )
                accruals_loaded = True
            except RuntimeError as exc:
                accruals = pd.DataFrame()
                accrual_error = str(exc)
            progress.progress(65, text="Сопоставляем товары и себестоимость…")

            sales_columns = [
                "sku", "name", "ordered_revenue", "ordered_units",
                "delivered_units", "returned_units",
            ]
            if sales.empty:
                sales = pd.DataFrame(columns=sales_columns)
            report = sales.merge(sku_finance, on="sku", how="outer")
            all_skus = report["sku"].dropna().astype(str).unique().tolist()
            mapping = fetch_ozon_sku_to_offer(client_id, api_key, all_skus) if all_skus else pd.DataFrame(columns=["sku", "offer_id"])
            report = report.merge(mapping, on="sku", how="left")
            if not costs.empty:
                report = report.merge(costs, left_on="offer_id", right_on="ms_code", how="left", indicator=True)
                report["cost_missing"] = (report["_merge"] != "both") | (pd.to_numeric(report["cost_price"], errors="coerce") <= 0)
            else:
                for column in ("ms_code", "ms_name", "cost_price", "stock"):
                    report[column] = pd.NA
                report["cost_missing"] = True
            report["name"] = report["name"].fillna("")
            report["has_finance"] = report["has_finance"].fillna(False).astype(bool)
            if units_available:
                report[["delivered_units", "returned_units"]] = report[["delivered_units", "returned_units"]].fillna(0)
            numeric_columns = [
                "ordered_revenue", "ordered_units", "delivered_units", "returned_units",
                "sales_revenue", "bonus_amount", "partner_programs", "sale_commission",
                "delivery_fees", "item_fees", "cost_price",
            ]
            for column in numeric_columns:
                report[column] = pd.to_numeric(report.get(column), errors="coerce")
                if column not in ("delivered_units", "returned_units"):
                    report[column] = report[column].fillna(0)

            report["net_units"] = report["delivered_units"] - report["returned_units"]
            report["revenue_total"] = report["sales_revenue"] + report["bonus_amount"] + report["partner_programs"]
            report["ozon_fees"] = report["sale_commission"] + report["delivery_fees"] + report["item_fees"]
            report["cost_total"] = report["cost_price"] * report["net_units"]
            report["profit_period"] = (
                report["revenue_total"] + report["ozon_fees"] - report["cost_total"]
            ).where(report["has_finance"] & ~report["cost_missing"] & report["net_units"].notna())
            report["profit_per_unit"] = report["profit_period"].div(
                report["net_units"].where(report["net_units"] > 0)
            ).where(report["profit_period"].notna()).fillna(0).round(4)
            report["margin_pct"] = (
                report["profit_period"].div(report["revenue_total"].where(report["revenue_total"] > 0)) * 100
            ).where(report["profit_period"] > 0).round(2)

            profit_rows = report["profit_period"].notna()
            covered = report.loc[profit_rows]
            sales_total = float(covered["sales_revenue"].sum())
            bonus_total = float(covered["bonus_amount"].sum())
            partner_total = float(covered["partner_programs"].sum())
            delivered_total = float(covered["delivered_units"].sum())
            returns_total = float(covered["returned_units"].sum())
            cost_total = float(report.loc[profit_rows, "cost_total"].sum()) if not report.empty else 0.0
            profit_total = float(report.loc[profit_rows, "profit_period"].sum()) if not report.empty else 0.0
            matched_revenue_total = float(report.loc[profit_rows, "revenue_total"].sum()) if not report.empty else 0.0
            margin_total = (profit_total / matched_revenue_total * 100
                            if profit_total > 0 and matched_revenue_total > 0 else None)
            unmatched = int(report["cost_missing"].sum()) if not report.empty else 0
            finance_missing = int((~report["has_finance"]).sum()) if not report.empty else 0
            non_item_total = float(pd.to_numeric(unallocated_fees.get("amount", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
            progress.progress(100, text="Готово")

            if more_sales:
                st.warning("Ozon сообщает о дополнительных страницах аналитики. Для защиты от лимита API загружена только первая страница (до 1 000 строк); итог неполный.")
            if unmatched:
                st.warning(f"Для {unmatched} из {len(report)} строк себестоимость не найдена или равна нулю. Прибыль по ним не рассчитывается.")
            if finance_missing:
                st.warning(f"По {finance_missing} товарам нет начислений Ozon за выбранный период; их прибыль не рассчитывается.")
            if not units_available:
                st.warning("План Ozon API не вернул показатели доставленных и возвращённых товаров. Выручку по заказам можно посмотреть, но прибыль по формуле юнит-экономики не рассчитана.")
            st.warning("Формула повторяет выгрузку юнит-экономики: выручка + баллы + программы партнёров + комиссии/услуги Ozon − себестоимость × (доставлено − возвращено). Себестоимость берётся из текущих остатков МоегоСклада, а не из исторических партий.")
            if not accruals_loaded:
                message = "Финансовые операции не получены. Прибыль и маржинальность не рассчитываются."
                if accrual_error:
                    message += f" Причина: {accrual_error}"
                st.info(message)

            a, b, c, d, e, f = st.columns(6)
            a.metric("Продажи по начислениям", f"{sales_total:,.2f} ₽")
            b.metric("Баллы Ozon", f"{bonus_total:,.2f} ₽")
            c.metric("Доставлено / возвраты", f"{delivered_total:,.0f} / {returns_total:,.0f} шт.")
            d.metric("Себестоимость", f"{cost_total:,.2f} ₽")
            e.metric("Прибыль за период", f"{profit_total:,.2f} ₽" if accruals_loaded else "нет данных")
            f.metric("Доля от продаж", f"{margin_total:.2f}%" if margin_total is not None else "—")
            st.caption(f"Сводные суммы показаны только по SKU с начислениями и найденной себестоимостью ({int(profit_rows.sum())} из {len(report)} строк). Программы партнёров: {partner_total:,.2f} ₽ · Нераспределённые начисления без SKU: {non_item_total:,.2f} ₽; они не включены в прибыль по товарам.")

            st.subheader("Юнит-экономика по товарам")
            display_columns = [
                "offer_id", "name", "sku", "sales_revenue", "bonus_amount", "partner_programs",
                "sale_commission", "delivery_fees", "item_fees", "delivered_units", "returned_units",
                "net_units", "cost_price", "cost_total", "margin_pct", "profit_per_unit", "profit_period",
                "ordered_revenue", "ms_name", "stock", "cost_missing", "has_finance",
            ]
            column_names_ru = {
                "offer_id": "Артикул",
                "name": "Название товара",
                "sku": "SKU Ozon",
                "sales_revenue": "Продажи по начислениям, ₽",
                "bonus_amount": "Баллы Ozon, ₽",
                "partner_programs": "Программы партнёров, ₽",
                "sale_commission": "Вознаграждение Ozon, ₽",
                "delivery_fees": "Доставка и логистика, ₽",
                "item_fees": "Дополнительные услуги и штрафы, ₽",
                "delivered_units": "Доставлено, шт.",
                "returned_units": "Возвращено, шт.",
                "net_units": "Продано за вычетом возвратов, шт.",
                "cost_price": "Историческая средняя себестоимость, ₽",
                "cost_total": "Себестоимость за период, ₽",
                "margin_pct": "Доля от продаж, %",
                "profit_per_unit": "Прибыль за единицу, ₽",
                "profit_period": "Прибыль за период, ₽",
                "ordered_revenue": "Заказано на сумму, ₽",
                "ordered_units": "Заказано, шт.",
                "ms_name": "Название в МойСклад",
                "ms_code": "Код в МойСклад",
                "stock": "Остаток, шт.",
                "cost_missing": "Себестоимость не найдена",
                "has_finance": "Начисления найдены",
                "revenue_total": "Продажи с баллами и программами, ₽",
                "ozon_fees": "Расходы Ozon, ₽",
                "seller_price_fallback": "Цена продавца из начислений, ₽",
                "date": "Дата",
                "accrued_category": "Категория начисления",
                "type_id": "Код типа операции",
                "unit_number": "Номер отправления или операции",
                "amount": "Сумма, ₽",
                "currency": "Валюта",
            }
            table = report[[col for col in display_columns if col in report]].sort_values("sales_revenue", ascending=False)
            st.dataframe(table.rename(columns=column_names_ru), use_container_width=True, height=500)
            if not accruals.empty:
                with st.expander("Финансовые операции Ozon"):
                    st.dataframe(accruals.rename(columns=column_names_ru), use_container_width=True)
            if not unallocated_fees.empty:
                with st.expander("Начисления без привязки к товару"):
                    st.dataframe(unallocated_fees.rename(columns=column_names_ru), use_container_width=True)
            csv_report = report.drop(columns=["_merge"], errors="ignore").rename(columns=column_names_ru)
            st.download_button("Скачать CSV", csv_report.to_csv(index=False).encode("utf-8-sig"), "ozon_moysklad_analytics.csv", "text/csv")
        except (RuntimeError, ValueError, requests.RequestException) as exc:
            st.error(f"Не удалось собрать отчёт: {exc}")
        except Exception as exc:
            st.error(f"При обработке данных возникла непредвиденная ошибка: {exc}")

st.markdown("---")
st.caption("Юнит-экономика: прибыль за период = продажи + баллы + программы партнёров + комиссии и услуги Ozon − себестоимость × (доставлено − возвращено). Доля от продаж = прибыль за период / (продажи + баллы + программы партнёров). Для убыточных товаров доля не показывается, как в исходной выгрузке.")
