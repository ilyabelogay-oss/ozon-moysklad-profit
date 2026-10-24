import io
import time

import pandas as pd
import requests
import streamlit as st

MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"
REQUEST_TIMEOUT = 60

st.set_page_config(page_title="Юнит-экономика Ozon + МойСклад", layout="wide", page_icon="📊")
st.markdown(
    """
    <style>
    :root {
        --xr-red: #ed3946;
        --xr-red-soft: rgba(237, 57, 70, .13);
        --xr-panel: rgba(30, 32, 39, .82);
        --xr-line: rgba(255, 255, 255, .09);
        --xr-muted: #a5a8b2;
    }
    [data-testid="stAppViewContainer"] {
        background:
            radial-gradient(ellipse at 82% 0%, rgba(237, 57, 70, .10), transparent 32%),
            linear-gradient(145deg, #111216 0%, #17191f 55%, #111216 100%);
    }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stMainBlockContainer"] { max-width: 1550px; padding-top: 2rem; }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1b1d23 0%, #15161b 100%);
        border-right: 1px solid var(--xr-line);
    }
    h1, h2, h3 { letter-spacing: -.035em; }
    h1 { font-size: clamp(1.9rem, 3vw, 2.75rem) !important; font-weight: 760 !important; }
    [data-testid="stCaptionContainer"] { color: var(--xr-muted); }
    [data-testid="stMetric"] {
        background: var(--xr-panel);
        border: 1px solid var(--xr-line);
        border-radius: 16px;
        padding: 18px 20px;
        min-height: 118px;
        box-shadow: 0 10px 28px rgba(0, 0, 0, .16);
    }
    [data-testid="stMetricLabel"] { color: var(--xr-muted); }
    [data-testid="stMetricValue"] { font-weight: 720; }
    div.stButton > button, div.stDownloadButton > button {
        border-radius: 10px;
        border: 1px solid rgba(237, 57, 70, .55);
        transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
    }
    div.stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #f04450, #cf2634);
        color: white;
        border: 0;
        box-shadow: 0 7px 20px rgba(237, 57, 70, .22);
    }
    div.stButton > button:hover, div.stDownloadButton > button:hover {
        transform: translateY(-1px);
        border-color: var(--xr-red);
        box-shadow: 0 8px 20px rgba(237, 57, 70, .16);
    }
    [data-testid="stFileUploader"] section {
        border: 1px dashed rgba(237, 57, 70, .55);
        border-radius: 16px;
        background: var(--xr-red-soft);
    }
    [data-testid="stTabs"] [role="tab"] { font-weight: 650; }
    [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
        color: var(--xr-red);
        border-bottom-color: var(--xr-red);
    }
    [data-testid="stDataFrame"] {
        border: 1px solid var(--xr-line);
        border-radius: 14px;
        overflow: hidden;
    }
    [data-testid="stAlert"] { border-radius: 12px; }
    .xr-kicker {
        color: var(--xr-red);
        text-transform: uppercase;
        font-size: .76rem;
        font-weight: 800;
        letter-spacing: .17em;
        margin: .1rem 0 .35rem;
    }
    .xr-subtitle { color: var(--xr-muted); font-size: 1rem; margin-top: -.35rem; }
    </style>
    """,
    unsafe_allow_html=True,
)
brand_logo, brand_title = st.columns([1, 7])
with brand_logo:
    st.image("assets/x-ride-logo.jpg", width=112)
with brand_title:
    st.markdown('<div class="xr-kicker">X-RIDE · ANALYTICS</div>', unsafe_allow_html=True)
    st.title("Юнит-экономика Ozon × МойСклад")
    st.markdown('<div class="xr-subtitle">Прибыль, себестоимость и целевая цена по каждой позиции</div>', unsafe_allow_html=True)


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
    response = None
    for attempt in range(3):
        try:
            response = requests.request(
                method, url, headers=headers, json=payload, params=params, timeout=REQUEST_TIMEOUT
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise RuntimeError(f"Не удалось подключиться к {url} после 3 попыток: {exc}") from exc
            time.sleep(1 + attempt * 2)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 2:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(1.0, min(float(retry_after), 15.0))
                except (TypeError, ValueError):
                    delay = 1 + attempt * 2
                time.sleep(delay)
                continue
        break
    if not response.ok:
        detail = response.text[:500].strip()
        raise RuntimeError(f"API вернул {response.status_code}: {detail or response.reason}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API вернул ответ не в формате JSON.") from exc


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def fetch_moysklad_costs(token: str) -> pd.DataFrame:
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
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

    return costs


@st.cache_data(show_spinner=False)
def parse_unit_economics(file_bytes: bytes) -> tuple[pd.DataFrame, str]:
    raw = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="openpyxl")
    if raw.shape[0] < 5:
        raise ValueError("В файле не найдена таблица юнит-экономики.")
    period_cell = raw.iat[0, 0]
    period_label = str(period_cell) if pd.notna(period_cell) else "Период не указан"
    headers = [" ".join(str(value).split()) if pd.notna(value) else f"Столбец {i + 1}" for i, value in enumerate(raw.iloc[3])]
    data = raw.iloc[4:].copy()
    data.columns = headers
    data = data.dropna(how="all")
    required = [
        "SKU", "Артикул", "Название товара", "Себестоимость", "Текущая цена",
        "Передано в доставку покупателям, шт", "Доставлено товаров, шт",
        "Возвращено товаров, шт", "Выручка", "Баллы за скидки",
        "Программы партнёров", "Прибыль за период",
    ]
    missing = [column for column in required if column not in data.columns]
    if missing:
        raise ValueError("Не найдены обязательные столбцы: " + ", ".join(missing))
    return data.reset_index(drop=True), period_label


def normalize_code(value) -> str:
    if pd.isna(value):
        return ""
    result = str(value).strip()
    if result.endswith(".0") and result[:-2].isdigit():
        result = result[:-2]
    return result


st.markdown("---")
with st.sidebar:
    st.header("МойСклад")
    secret_ms_token = get_streamlit_secret("MOYSKLAD_TOKEN")
    if secret_ms_token:
        st.caption("Токен МоегоСклада берётся из секретов Streamlit.")
    ms_token = secret_ms_token or st.text_input("Токен МоегоСклада", type="password")
    refresh_costs = st.button(
        "Обновить себестоимость",
        use_container_width=True,
        help="Заново загрузить исторические приёмки и оприходования из МоегоСклада.",
    )

uploaded_file = st.file_uploader(
    "Загрузите XLSX-выгрузку «Юнит-экономика» из Ozon",
    type=["xlsx"],
    help="Приложение возьмёт из файла показатели продаж, начислений, цены и прибыли, а историческую себестоимость загрузит из МоегоСклада.",
)
run_report = st.button("Рассчитать по таблице", type="primary", disabled=uploaded_file is None)
if refresh_costs:
    fetch_moysklad_costs.clear()
if run_report or refresh_costs:
    st.session_state["report_ready"] = True
run = st.session_state.get("report_ready", False)

if not uploaded_file:
    st.info("Загрузите XLSX-выгрузку юнит-экономики Ozon, чтобы рассчитать прибыль с исторической себестоимостью из МоегоСклада.")
elif run:
    if not ms_token:
        st.error("Добавьте токен МоегоСклада в секреты Streamlit или в поле слева.")
    else:
        try:
            report, period_label = parse_unit_economics(uploaded_file.getvalue())
            progress = st.progress(15, text="Читаем выгрузку Ozon…")
            rename_columns = {
                "SKU": "sku",
                "Артикул": "offer_id",
                "Название товара": "name",
                "Схема работы": "scheme",
                "Себестоимость": "ozon_cost_price",
                "Текущая цена": "price",
                "Передано в доставку покупателям, шт": "sent_to_delivery",
                "Доставлено товаров, шт": "delivered_units",
                "Возвращено товаров, шт": "returned_units",
                "Выручка": "revenue",
                "Баллы за скидки": "discount_points",
                "Программы партнёров": "partner_programs",
                "Доля от продаж": "ozon_margin_pct",
                "Прибыль за шт": "ozon_profit_per_unit",
                "Прибыль за период": "ozon_profit_period",
            }
            report = report.rename(columns=rename_columns)
            for column in (
                "ozon_profit_per_unit", "ozon_margin_pct",
            ):
                if column not in report:
                    report[column] = pd.NA
            report["offer_id"] = report["offer_id"].map(normalize_code)
            report["sku"] = report["sku"].map(normalize_code)
            numeric_columns = [
                "ozon_cost_price", "price", "sent_to_delivery", "delivered_units",
                "returned_units", "revenue", "discount_points", "partner_programs",
                "ozon_profit_per_unit", "ozon_profit_period",
            ]
            for column in numeric_columns:
                if column in report:
                    report[column] = pd.to_numeric(report[column], errors="coerce")
            for column in ("ozon_cost_price", "price", "sent_to_delivery", "delivered_units", "returned_units", "revenue", "discount_points", "partner_programs"):
                report[column] = report[column].fillna(0)

            progress.progress(35, text="Загружаем историческую себестоимость из МоегоСклада…")
            costs = fetch_moysklad_costs(ms_token)
            report = report.merge(costs, left_on="offer_id", right_on="ms_code", how="left")
            report["cost_missing"] = pd.to_numeric(report["cost_price"], errors="coerce").isna() | (pd.to_numeric(report["cost_price"], errors="coerce") <= 0)
            report["cost_price"] = pd.to_numeric(report["cost_price"], errors="coerce")
            report["net_units"] = report["delivered_units"] - report["returned_units"]
            report["cost_total"] = report["cost_price"] * report["net_units"]
            report["profit_per_unit"] = (
                report["ozon_profit_per_unit"] + report["ozon_cost_price"] - report["cost_price"]
            ).where(~report["cost_missing"]).round(4)
            report["profit_period"] = (
                report["profit_per_unit"] * report["net_units"]
            ).where(report["profit_per_unit"].notna() & (report["net_units"] > 0))
            report["margin_pct"] = (
                report["profit_per_unit"].div(report["price"].where(report["price"] > 0)) * 100
            ).where(report["profit_per_unit"].notna()).round(2)
            progress.progress(100, text="Готово")

            missing_costs = int(report["cost_missing"].sum())
            if missing_costs:
                st.warning(f"Для {missing_costs} из {len(report)} товаров не найдена историческая себестоимость в МоёмСкладе. Прибыль по ним не рассчитывается.")
            st.caption(period_label)
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Выручка", f"{report['revenue'].sum():,.2f} ₽")
            c2.metric("Баллы и программы", f"{(report['discount_points'] + report['partner_programs']).sum():,.2f} ₽")
            c3.metric("Себестоимость по МоемуСкладу", f"{report['cost_total'].sum(skipna=True):,.2f} ₽")
            c4.metric("Прибыль с найденной себестоимостью", f"{report['profit_period'].sum(skipna=True):,.2f} ₽")
            valid_profit_rows = report["profit_period"].notna()
            margin_denominator = (
                report.loc[valid_profit_rows, "price"]
                * report.loc[valid_profit_rows, "net_units"]
            ).sum()
            margin_total = report.loc[valid_profit_rows, "profit_period"].sum() / margin_denominator * 100 if margin_denominator > 0 else 0
            c5.metric("Маржинальность от текущей цены", f"{margin_total:.2f}%")
            st.info("Прибыль за единицу пересчитывается заменой себестоимости: «Прибыль за шт» из Ozon + себестоимость Ozon из файла − историческая себестоимость из МоегоСклада. Маржинальность = прибыль за единицу ÷ текущая цена × 100%. Прибыль за период = прибыль за единицу × (доставлено − возвращено).")

            column_names_ru = {
                "sku": "SKU Ozon", "offer_id": "Артикул", "name": "Название товара",
                "scheme": "Схема работы", "ozon_cost_price": "Себестоимость из выгрузки, ₽",
                "cost_price": "Историческая себестоимость МоегоСклада, ₽",
                "cost_total": "Себестоимость доставленных товаров за вычетом возвратов, ₽",
                "net_units": "Доставлено минус возвращено, шт.",
                "price": "Текущая цена, ₽", "sent_to_delivery": "Передано в доставку, шт.",
                "delivered_units": "Доставлено, шт.", "returned_units": "Возвращено, шт.",
                "revenue": "Выручка, ₽", "discount_points": "Баллы за скидки, ₽",
                "partner_programs": "Программы партнёров, ₽", "ozon_margin_pct": "Доля от продаж Ozon, %",
                "ozon_profit_per_unit": "Прибыль за штуку из выгрузки, ₽",
                "ozon_profit_period": "Прибыль за период из выгрузки, ₽",
                "profit_per_unit": "Прибыль после себестоимости за единицу, ₽",
                "profit_period": "Прибыль после себестоимости за период, ₽",
                "margin_pct": "Маржинальность после себестоимости, %",
                "ms_name": "Название в МойСклад", "ms_code": "Код в МойСклад",
                "cost_missing": "Себестоимость не найдена",
            }
            tab_report, tab_price = st.tabs(["Юнит-экономика", "Целевая маржинальность"])
            with tab_report:
                st.subheader("Юнит-экономика по товарам из выгрузки")
                display_report = report.sort_values("profit_period", ascending=False, na_position="last").rename(columns=column_names_ru)
                st.dataframe(display_report, use_container_width=True, height=520)
                st.download_button(
                    "Скачать расчёт CSV",
                    display_report.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                    "ozon_moysklad_unit_economics.csv",
                    "text/csv",
                )
            with tab_price:
                st.subheader("Расчёт цены для целевой маржинальности")
                st.caption("Используются текущая цена из выгрузки Ozon и прибыль после вычета себестоимости за единицу. Надбавка рассчитывается по формуле: (целевая маржа × цена − прибыль за единицу) / (1 − целевая маржа).")
                with st.form("target_margin_form"):
                    target_margin_pct = st.number_input(
                        "Целевая маржинальность, %", min_value=0.0, max_value=95.0,
                        value=30.0, step=1.0, key="target_margin_pct",
                    )
                    st.form_submit_button("Пересчитать цены")
                scenario = report.loc[
                    (~report["cost_missing"])
                    & report["profit_per_unit"].notna()
                    & report["margin_pct"].notna()
                    & (report["price"] > 0)
                    & (report["margin_pct"] < target_margin_pct)
                ].copy()
                if scenario.empty:
                    st.info("Нет товаров ниже заданной целевой маржинальности.")
                else:
                    target_rate = target_margin_pct / 100
                    scenario["price_increase"] = (
                        (target_rate * scenario["price"] - scenario["profit_per_unit"])
                        / (1 - target_rate)
                    ).clip(lower=0)
                    scenario["recommended_price"] = scenario["price"] + scenario["price_increase"]
                    scenario["projected_margin"] = (
                        (scenario["profit_per_unit"] + scenario["price_increase"])
                        / scenario["recommended_price"] * 100
                    )
                    scenario_view = scenario[[
                        "offer_id", "name", "sku", "price", "sent_to_delivery", "delivered_units",
                        "returned_units", "margin_pct", "profit_per_unit", "price_increase",
                        "recommended_price", "projected_margin",
                    ]].rename(columns={
                        "offer_id": "Артикул", "name": "Название товара", "sku": "SKU Ozon",
                        "price": "Текущая цена, ₽", "margin_pct": "Текущая маржинальность, %",
                        "sent_to_delivery": "Передано покупателям, шт.",
                        "delivered_units": "Доставлено, шт.", "returned_units": "Возвращено, шт.",
                        "profit_per_unit": "Прибыль за единицу, ₽",
                        "price_increase": "Добавить к цене, ₽", "recommended_price": "Рекомендуемая цена, ₽",
                        "projected_margin": "Маржинальность после изменения, %",
                    }).sort_values("Добавить к цене, ₽", ascending=False)
                    quantity_columns = [
                        "Передано покупателям, шт.", "Доставлено, шт.", "Возвращено, шт.",
                    ]
                    scenario_view[quantity_columns] = scenario_view[quantity_columns].astype(float)
                    quantity_format = {
                        column: st.column_config.NumberColumn(format="%.1f")
                        for column in quantity_columns
                    }
                    st.dataframe(
                        scenario_view, column_config=quantity_format,
                        use_container_width=True, height=500,
                    )
                    st.download_button(
                        "Скачать расчёт цен CSV", scenario_view.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                        "ozon_target_margin_prices.csv", "text/csv",
                    )
        except (RuntimeError, ValueError, requests.RequestException, ImportError) as exc:
            st.error(f"Не удалось обработать файл или загрузить себестоимость: {exc}")
        except Exception as exc:
            st.error(f"При обработке данных возникла непредвиденная ошибка: {exc}")
else:
    st.info("Файл загружен. Нажмите «Рассчитать по таблице».")

st.markdown("---")
st.caption("Источник продаж, начислений Ozon, цены и показателей прибыли — загруженная XLSX-выгрузка юнит-экономики. Историческая средневзвешенная себестоимость загружается из проведённых документов «Приёмка» и «Оприходование» в МоемСкладе.")
