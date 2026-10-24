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
st.caption("Сопоставление выполняется по offer_id Ozon и коду товара в МоемСкладе.")


def ozon_headers(client_id: str, api_key: str) -> dict:
    return {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"}


def request_json(method: str, url: str, *, headers: dict, payload: dict | None = None) -> dict:
    try:
        response = requests.request(method, url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Не удалось подключиться к {url}: {exc}") from exc
    if not response.ok:
        detail = response.text[:500].strip()
        raise RuntimeError(f"API вернул {response.status_code}: {detail or response.reason}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API вернул ответ не в формате JSON.") from exc


def fetch_ozon_sales(client_id: str, api_key: str, date_from: date, date_to: date) -> tuple[pd.DataFrame, bool]:
    payload = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "metrics": ["revenue", "ordered_units"],
        "dimension": ["sku"],
        "limit": 1000,
        "offset": 0,
    }
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
                "revenue": float(metrics[0]) if metrics else 0.0,
                "ordered_units": float(metrics[1]) if len(metrics) > 1 else 0.0,
            })
    # Ozon currently applies a strict request limit to this endpoint. Do not silently
    # fetch a second page and risk turning a normal report into a rate-limit failure.
    return pd.DataFrame(rows, columns=["sku", "name", "revenue", "ordered_units"]), bool(result.get("has_next", False))


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
) -> tuple[pd.DataFrame, pd.DataFrame]:
    headers = ozon_headers(client_id, api_key)
    rows = []
    bonus_rows = []
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
                    bonus = commission.get("bonus") or {}
                    sku = product.get("sku")
                    if sku is not None:
                        bonus_rows.append({"sku": str(sku), "bonus_amount": float(bonus.get("amount") or 0)})
                rows.append({
                    "date": item.get("date") or current_date.isoformat(),
                    "accrued_category": item.get("accrued_category", ""),
                    "type_id": item.get("type_id", ""),
                    "unit_number": item.get("unit_number", ""),
                    "amount": float(total.get("amount") or 0),
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
    bonuses = pd.DataFrame(bonus_rows, columns=["sku", "bonus_amount"])
    if not bonuses.empty:
        bonuses = bonuses.groupby("sku", as_index=False)["bonus_amount"].sum()
    return accruals, bonuses


def fetch_moysklad_costs(token: str) -> pd.DataFrame:
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    response = request_json("GET", f"{MOYSKLAD_URL}/report/stock/all", headers=headers)
    rows = []
    for item in response.get("rows", []):
        code = item.get("code") or item.get("article") or item.get("productCode") or item.get("modificationCode")
        if not code:
            continue
        rows.append({
            "ms_code": str(code).strip(),
            "ms_name": item.get("name", ""),
            # In the stock report, price is the cost price and is returned in kopecks.
            "cost_price": float(item.get("price") or 0) / 100,
            "stock": item.get("stock", 0),
        })
    costs = pd.DataFrame(rows)
    if not costs.empty:
        # A product may have rows for multiple warehouses; cost must not multiply.
        costs = costs.groupby("ms_code", as_index=False).agg(
            ms_name=("ms_name", "first"), cost_price=("cost_price", "max"), stock=("stock", "sum")
        )
    return costs


def validate_period(start: date, end: date) -> None:
    if start > end:
        raise ValueError("Дата начала периода позже даты окончания.")
    if (end - start).days > 365:
        raise ValueError("Выберите период не длиннее одного года.")


with st.sidebar:
    st.header("Подключение")
    client_id = st.text_input("Ozon Client-Id", type="password")
    api_key = st.text_input("Ozon Api-Key", type="password")
    ms_token = st.text_input("Токен МоегоСклада", type="password")
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
            sales, more_sales = fetch_ozon_sales(client_id, api_key, date_from, date_to)
            progress.progress(25, text="Сопоставляем артикулы Ozon…")
            if not sales.empty:
                mapping = fetch_ozon_sku_to_offer(client_id, api_key, sales["sku"].dropna().unique().tolist())
                sales = sales.merge(mapping, on="sku", how="left")
            else:
                sales["offer_id"] = pd.Series(dtype="object")
            progress.progress(50, text="Загружаем себестоимость из МоегоСклада…")
            costs = fetch_moysklad_costs(ms_token)
            progress.progress(70, text="Загружаем финансовые операции Ozon…")
            accrual_error = None
            accruals_loaded = False
            bonus_by_sku = pd.DataFrame(columns=["sku", "bonus_amount"])
            try:
                accruals, bonus_by_sku = fetch_ozon_accruals(client_id, api_key, date_from, date_to)
                accruals_loaded = True
            except RuntimeError as exc:
                accruals = pd.DataFrame()
                accrual_error = str(exc)
            progress.progress(90, text="Считаем показатели…")

            if not sales.empty and not costs.empty:
                report = sales.merge(costs, left_on="offer_id", right_on="ms_code", how="left", indicator=True)
            else:
                report = sales.copy()
                for column in ("ms_code", "ms_name", "cost_price", "stock"):
                    report[column] = pd.NA
                report["_merge"] = "left_only"
            if not bonus_by_sku.empty:
                report = report.merge(bonus_by_sku, on="sku", how="left")
            else:
                report["bonus_amount"] = 0.0
            report["bonus_amount"] = pd.to_numeric(report["bonus_amount"], errors="coerce").fillna(0)
            report["ordered_units"] = pd.to_numeric(report.get("ordered_units", 0), errors="coerce").fillna(0)
            report["revenue"] = pd.to_numeric(report.get("revenue", 0), errors="coerce").fillna(0)
            report["cost_price"] = pd.to_numeric(report["cost_price"], errors="coerce")
            report["cost_total"] = report["cost_price"] * report["ordered_units"]
            report["revenue_with_bonus"] = report["revenue"] + report["bonus_amount"]
            report["gross_profit"] = report["revenue_with_bonus"] - report["cost_total"]
            revenue_for_margin = report["revenue_with_bonus"].where(report["revenue_with_bonus"] != 0)
            report["margin_pct"] = (report["gross_profit"] / revenue_for_margin * 100).round(1)
            report["cost_missing"] = report["_merge"] != "both"

            expense_total = 0.0
            if not accruals.empty and "amount" in accruals:
                accruals["amount"] = pd.to_numeric(accruals["amount"], errors="coerce").fillna(0)
                expense_total = float(accruals.loc[accruals["amount"] < 0, "amount"].sum())
            revenue_total = float(report["revenue"].sum()) if not report.empty else 0.0
            bonus_total = float(report["bonus_amount"].sum()) if not report.empty else 0.0
            units_total = float(report["ordered_units"].sum()) if not report.empty else 0.0
            cost_total = float(report["cost_total"].sum()) if not report.empty else 0.0
            gross_total = revenue_total + bonus_total - cost_total
            estimated_net = gross_total + expense_total
            unmatched = int(report["cost_missing"].sum()) if not report.empty else 0
            progress.progress(100, text="Готово")

            if more_sales:
                st.warning("Ozon сообщает о дополнительных страницах аналитики. Для защиты от лимита API загружена только первая страница (до 1 000 строк); итог неполный.")
            if unmatched:
                st.warning(f"Для {unmatched} из {len(report)} строк не найдена себестоимость. Такие строки исключены из расчёта себестоимости и прибыли.")
            if not accruals_loaded:
                message = "Финансовые операции не получены. Чистая прибыль не рассчитывается."
                if accrual_error:
                    message += f" Причина: {accrual_error}"
                st.info(message)

            a, b, c, d, e, f = st.columns(6)
            a.metric("Выручка Ozon", f"{revenue_total:,.2f} ₽")
            b.metric("Баллы Ozon", f"{bonus_total:,.2f} ₽")
            c.metric("Заказано, шт.", f"{units_total:,.0f}")
            d.metric("Себестоимость", f"{report['cost_total'].fillna(0).sum():,.2f} ₽")
            e.metric("Прибыль до расходов Ozon", f"{gross_total:,.2f} ₽")
            f.metric("Расходы Ozon", f"{expense_total:,.2f} ₽" if accruals_loaded else "нет данных")
            if accruals_loaded:
                st.metric("Оценка после расходов Ozon", f"{estimated_net:,.2f} ₽", help="Выручка минус себестоимость найденных товаров плюс отрицательные суммы начислений. Это оценка: расходы не распределены по SKU.")

            st.subheader("Продажи по товарам")
            display_columns = ["offer_id", "name", "sku", "revenue", "bonus_amount", "revenue_with_bonus", "ordered_units", "cost_price", "cost_total", "gross_profit", "margin_pct", "ms_name", "stock", "cost_missing"]
            st.dataframe(report[[col for col in display_columns if col in report]].sort_values("revenue", ascending=False), use_container_width=True, height=500)
            if not accruals.empty:
                with st.expander("Финансовые операции Ozon"):
                    st.dataframe(accruals, use_container_width=True)
            st.download_button("Скачать CSV", report.drop(columns=["_merge"], errors="ignore").to_csv(index=False).encode("utf-8-sig"), "ozon_moysklad_analytics.csv", "text/csv")
        except (RuntimeError, ValueError, requests.RequestException) as exc:
            st.error(f"Не удалось собрать отчёт: {exc}")
        except Exception as exc:
            st.error(f"При обработке данных возникла непредвиденная ошибка: {exc}")

st.markdown("---")
st.caption("Прибыль до расходов Ozon = выручка Ozon + баллы Ozon за скидки − себестоимость по заказанным единицам. Оценка после расходов также учитывает отрицательные суммы начислений, которые не распределяются по товарам. Сверяйте результаты с отчётами Ozon и МоегоСклада.")
