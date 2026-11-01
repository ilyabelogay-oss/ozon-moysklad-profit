import io
import math
import time

import pandas as pd
import requests
import streamlit as st

MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"
REQUEST_TIMEOUT = 60
OZON_VARIABLE_EXPENSE_COLUMNS = ["Вознаграждение Ozon", "Эквайринг"]
OZON_FIXED_EXPENSE_COLUMNS = [
    "Обработка отправления", "Логистика", "Доставка до места выдачи",
    "Стоимость размещения", "Обработка возврата", "Обратная логистика",
    "Утилизация", "Дополнительная обработка ОВХ", "Операционные ошибки",
    "Оплата за клик", "Оплата за заказ", "Звёздные товары", "Платный бренд", "Отзывы",
]
OZON_EXPENSE_COLUMNS = OZON_VARIABLE_EXPENSE_COLUMNS + OZON_FIXED_EXPENSE_COLUMNS

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


def calculate_product_economics(settings: dict) -> dict:
    """Calculate one-unit Ozon economics from editable seller assumptions."""
    price = max(float(settings["price"]), 0.0)
    buyout = min(max(float(settings["buyout"]), 0.01), 1.0)
    non_buyout = 1.0 - buyout
    volume = max(float(settings["volume"]), 0.0)
    routes = settings["routes"]
    route_share = sum(max(float(route["share"]), 0.0) for route in routes)
    if route_share > 0:
        forward_per_liter = sum(max(float(route["share"]), 0.0) * max(float(route["forward"]), 0.0) for route in routes) / route_share
        return_per_liter = sum(max(float(route["share"]), 0.0) * max(float(route["reverse"]), 0.0) for route in routes) / route_share
    else:
        forward_per_liter = return_per_liter = 0.0

    fbs = settings["scheme"] == "FBS"
    price = float(settings["price"])
    if settings["price_mode"] == "Цена покупателя":
        price = price / max(1.0 - settings["coinvest_pct"], 0.01)
    commission_rate = max(0.0, settings["commission_pct"] - settings["deferral_pp"])
    if fbs and settings["fbs_new_rules"]:
        dispatch_fee_pct = min(max(math.ceil(settings["fbs_dispatch_hours"] - 6.0), 0.0) * 0.0025, 0.03)
        dispatch_fee = price * dispatch_fee_pct
        late_penalty = min(price * settings["fbs_error_pct"], 2000.0) if not settings["fbs_on_time"] else 0.0
        speed_adjustment = -(dispatch_fee + late_penalty) / buyout
    else:
        speed_adjustment = price * settings["speed_adjustment_pct"] if fbs else 0.0
    commission = price * commission_rate - speed_adjustment
    acquiring = price * settings["acquiring_pct"]

    forward_logistics = forward_per_liter * volume / buyout
    if fbs:
        intake = settings["fbs_intake"] / buyout
    else:
        intake = 0.0 if settings["free_intake"] else min(5.0 + max(0, math.ceil(volume) - 1), 60.0)
    delivery_pvz = min(price * settings["delivery_pvz_pct"], 25.0) / buyout
    reverse_logistics = return_per_liter * volume * non_buyout / buyout
    return_processing = 15.0 * non_buyout / buyout
    repacking = settings["repack_tariff"] * settings["repack_share"] * non_buyout / buyout
    total_logistics = forward_logistics + intake + delivery_pvz + reverse_logistics + return_processing + repacking
    crossdock = 0.0 if fbs else settings["crossdock_per_liter"] * volume
    advertising = price * settings["drr_pct"] / buyout
    other_per_unit = settings["sku_monthly_expenses"] / max(settings["sales_month"], 1.0)
    indirect_per_unit = settings["company_monthly_expenses"] * settings["sku_share"] / max(settings["sales_month"], 1.0)
    stock_days = settings["stock_qty"] / max(settings["sales_month"], 1.0) * 30.0
    insurance = max(price * settings["insurance_daily_pct"], 0.01) * stock_days if settings["insurance_on"] and not fbs and settings["sales_month"] > 0 else 0.0

    coinvest_amount = price * settings["coinvest_pct"]
    buyer_price = price - coinvest_amount
    ozon_with_vat = commission + forward_logistics + advertising + crossdock + (0.0 if fbs else intake)
    points_base = 0.99 * max(0.0, ozon_with_vat + reverse_logistics)
    premium = max(0.0, coinvest_amount - points_base)
    vat_rate = settings["vat_pct"]
    output_vat = (buyer_price + premium) * vat_rate / (1.0 + vat_rate) if vat_rate else 0.0
    vat_deductible = vat_rate not in (0.05, 0.07)
    input_vat = 0.0
    if vat_deductible:
        input_vat += max(0.0, ozon_with_vat - min(coinvest_amount, points_base)) * settings["ozon_vat_rate"] / (1.0 + settings["ozon_vat_rate"])
        input_vat += settings["cost"] * settings["supplier_vat_pct"] / (1.0 + settings["supplier_vat_pct"])
        monthly_documented_unit = settings["monthly_documented_expenses"] * settings["sku_share"] / max(settings["sales_month"], 1.0)
        input_vat += monthly_documented_unit * settings["monthly_expenses_vat_pct"] / (1.0 + settings["monthly_expenses_vat_pct"])
    vat_payable = max(0.0, output_vat - input_vat)

    deductible_expenses = settings["cost"] + total_logistics + commission + acquiring + advertising + crossdock + other_per_unit + indirect_per_unit + insurance
    tax_base = max(0.0, deductible_expenses - settings["undocumented_expenses"])
    fixed_contribution = settings["fixed_contribution_year"] / 12.0 / max(settings["sales_month"], 1.0) if settings["is_ip"] and settings["tax_system"] != "ОСНО" else 0.0
    contribution_base = (buyer_price + premium - output_vat) if settings["tax_system"] in ("УСН Доходы", "УСН Доходы минус расходы") else max(0.0, price - tax_base - output_vat + input_vat)
    variable_contribution = 0.01 * max(0.0, contribution_base) if settings["is_ip"] and settings["tax_system"] != "ОСНО" else 0.0
    contributions = fixed_contribution + variable_contribution
    tax_rate = settings["tax_rate"]
    if settings["tax_system"] == "УСН Доходы":
        income_tax = max(0.0, (buyer_price + premium - output_vat) * tax_rate - contributions)
    elif settings["tax_system"] in ("УСН Доходы минус расходы", "УСН Доходы минус расходы (минимальный налог 1%)"):
        income_tax = max((price - (tax_base + contributions) - output_vat + input_vat) * tax_rate, (buyer_price + premium) * 0.01)
    elif settings["tax_system"] == "ОСНО":
        income_tax = max(0.0, (price - output_vat) - (tax_base + contributions - input_vat)) * tax_rate
    else:
        income_tax = 0.0
    tax = income_tax + vat_payable

    expenses = commission + acquiring + total_logistics + advertising + settings["cost"] + crossdock + other_per_unit + indirect_per_unit + insurance + contributions
    profit_before_tax = price - expenses
    profit = profit_before_tax - tax
    invested = settings["cost"] + crossdock
    return {
        "Цена продавца": price, "Цена покупателя": buyer_price, "Премия за баллы": premium,
        "Комиссия Ozon": commission, "Эквайринг": acquiring,
        "Прямая логистика": forward_logistics, "Приёмка": intake,
        "Доставка до ПВЗ": delivery_pvz, "Обратная логистика": reverse_logistics,
        "Обработка возврата": return_processing, "Доупаковка": repacking,
        "Логистика всего": total_logistics, "Кросс-докинг": crossdock,
        "Реклама": advertising, "Себестоимость": settings["cost"],
        "Прочие расходы на товар": other_per_unit, "Доля общих расходов": indirect_per_unit,
        "Страхование": insurance, "Страховые взносы": contributions,
        "НДС к уплате": vat_payable, "Налог УСН / на прибыль": income_tax,
        "Расходы до налога": expenses, "Прибыль до налога": profit_before_tax,
        "Чистая прибыль": profit, "Маржинальность": profit / price * 100.0 if price > 0 else 0.0,
        "ROI": profit / invested * 100.0 if invested > 0 else 0.0,
        "Оборачиваемость, недель": settings["stock_qty"] / max(settings["sales_month"], 1.0) * 4.345,
        "Денег в запасе": settings["stock_qty"] * settings["cost"],
    }


def render_full_product_calculator(report: pd.DataFrame) -> None:
    st.subheader("Полная экономика нового товара")
    st.caption("Введите тарифные допущения по вашему кабинету Ozon. Модель раскладывает расходы на одну выкупленную единицу, считает налоги и показывает P&L.")

    eligible = report.loc[(report["net_units"] > 0) & (report["price"] > 0)].copy()
    if not eligible.empty:
        weighted_price = float((eligible["price"] * eligible["net_units"]).sum())
        suggested_price = float(weighted_price / eligible["net_units"].sum())
        suggested_cost = float(eligible["cost_price"].dropna().median()) if eligible["cost_price"].notna().any() else 0.0
        def observed_rate(column: str) -> float:
            if column not in eligible:
                return 0.0
            total = -float(pd.to_numeric(eligible[column], errors="coerce").fillna(0).sum())
            return min(0.95, max(0.0, total / weighted_price))
        suggested_commission = observed_rate("Вознаграждение Ozon")
        suggested_acquiring = observed_rate("Эквайринг")
        suggested_sales = float(eligible["net_units"].sum())
    else:
        suggested_price, suggested_cost, suggested_commission, suggested_acquiring, suggested_sales = 1000.0, 0.0, 0.0, 0.0, 0.0

    with st.expander("Товар, цена и налоги", expanded=True):
        a, b, c, d = st.columns(4)
        with a:
            scheme = st.selectbox("Схема", ["FBO", "FBS"], key="fullcalc_scheme")
            price_mode = st.selectbox("Вводимая цена", ["Предельная цена продавца", "Цена покупателя"], key="fullcalc_price_mode")
            sale_price = st.number_input("Цена, ₽", min_value=0.0, value=float(round(suggested_price, 2)), step=50.0, key="fullcalc_price")
        with b:
            cost = st.number_input("Себестоимость с упаковкой, ₽/шт.", min_value=0.0, value=float(round(suggested_cost, 2)), step=10.0, key="fullcalc_cost")
            coinvest_pct = st.number_input("Баллы / соинвест, %", min_value=0.0, max_value=100.0, value=0.0, step=0.5, key="fullcalc_coinvest") / 100
            volume = st.number_input("Объём товара, л", min_value=0.0, value=1.0, step=0.1, key="fullcalc_volume")
        with c:
            commission_pct = st.number_input("Комиссия Ozon, %", min_value=0.0, max_value=100.0, value=round(suggested_commission * 100, 2), step=0.5, key="fullcalc_commission") / 100
            acquiring_pct = st.number_input("Эквайринг, %", min_value=0.0, max_value=100.0, value=round(suggested_acquiring * 100, 2), step=0.1, key="fullcalc_acquiring") / 100
            deferral_pp = st.number_input("Снижение комиссии за отсрочку, п.п.", min_value=0.0, max_value=100.0, value=0.0, step=0.5, key="fullcalc_deferral") / 100
        with d:
            buyout_pct = st.number_input("Выкуп, %", min_value=1.0, max_value=100.0, value=90.0, step=1.0, key="fullcalc_buyout") / 100
            speed_adjustment_pct = st.number_input("Скидка к комиссии / сбор FBS, %", min_value=-100.0, max_value=100.0, value=0.0, step=0.25, key="fullcalc_speed") / 100
            drr_pct = st.number_input("Реклама, ДРР от заказа, %", min_value=0.0, max_value=100.0, value=0.0, step=0.5, key="fullcalc_drr") / 100

    with st.expander("Логистика, возвраты и поставка", expanded=True):
        st.caption("Укажите тариф ₽/л по направлениям и долю заказов. Сумма долей нормируется автоматически. Заполните тарифы по фактическим кластерам вашего товара.")
        routes_default = pd.DataFrame([
            {"Кластер назначения": "Свой кластер", "Доля заказов, %": 0.0, "Доставка, ₽/л": 0.0, "Обратная логистика, ₽/л": 0.0},
            {"Кластер назначения": "Соседние кластеры", "Доля заказов, %": 0.0, "Доставка, ₽/л": 0.0, "Обратная логистика, ₽/л": 0.0},
            {"Кластер назначения": "Другие кластеры", "Доля заказов, %": 100.0, "Доставка, ₽/л": 0.0, "Обратная логистика, ₽/л": 0.0},
        ])
        routes = st.data_editor(routes_default, num_rows="dynamic", use_container_width=True, hide_index=True, key="fullcalc_routes", column_config={
            "Кластер назначения": st.column_config.TextColumn("Кластер назначения"),
            "Доля заказов, %": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=1.0),
            "Доставка, ₽/л": st.column_config.NumberColumn(min_value=0.0, step=1.0),
            "Обратная логистика, ₽/л": st.column_config.NumberColumn(min_value=0.0, step=1.0),
        })
        x, y, z, q = st.columns(4)
        with x:
            free_intake = st.checkbox("Бесплатная приёмка FBO", value=False, key="fullcalc_free_intake")
            fbs_intake = st.number_input("Обработка отправления FBS, ₽", min_value=0.0, value=0.0, step=1.0, key="fullcalc_fbs_intake")
            fbs_new_rules = st.checkbox("FBS: правила со сбором за время и индексом ошибок", value=False, key="fullcalc_fbs_new_rules")
            fbs_dispatch_hours = st.number_input("Время до отгрузки, ч", min_value=0.0, max_value=24.0, value=6.0, step=0.5, key="fullcalc_fbs_hours")
            fbs_error_pct = st.selectbox("Индекс ошибок FBS", [0.0, 0.01, 0.02, 0.04, 0.08], format_func=lambda value: f"{value * 100:.0f}%", key="fullcalc_fbs_error")
            fbs_on_time = st.checkbox("Отгрузка без просрочки", value=True, key="fullcalc_fbs_on_time")
        with y:
            delivery_pvz_pct = st.number_input("Доставка до ПВЗ, %", min_value=0.0, max_value=100.0, value=0.0, step=0.1, key="fullcalc_pvz") / 100
            repack_tariff = st.number_input("Тариф доупаковки, ₽", min_value=0.0, value=15.0, step=1.0, key="fullcalc_repack")
        with z:
            repack_share = st.number_input("Доля невыкупов с доупаковкой, %", min_value=0.0, max_value=100.0, value=45.0, step=1.0, key="fullcalc_repack_share") / 100
            crossdock_per_liter = st.number_input("Кросс-докинг, ₽/л", min_value=0.0, value=0.0, step=1.0, key="fullcalc_crossdock")
        with q:
            st.write("Приёмка FBO рассчитывается автоматически по объёму: до 1 л — 5 ₽, далее по 1 ₽/л, максимум 60 ₽.")
        if not routes.empty:
            shares_total = pd.to_numeric(routes["Доля заказов, %"], errors="coerce").fillna(0).sum()
            if abs(float(shares_total) - 100.0) > 0.1:
                st.warning(f"Сумма долей кластеров сейчас {shares_total:.1f}%. Для среднего тарифа доли будут нормированы.")

    with st.expander("Налоги, запасы и расходы компании"):
        l, r = st.columns(2)
        with l:
            tax_system = st.selectbox("Налоговый режим", ["Без налога", "УСН Доходы", "УСН Доходы минус расходы", "ОСНО"], key="fullcalc_tax_system")
            tax_rate = st.number_input("Ставка налога, %", min_value=0.0, max_value=100.0, value=6.0, step=0.5, key="fullcalc_tax_rate") / 100
            vat_pct = st.number_input("НДС в цене, %", min_value=0.0, max_value=100.0, value=0.0, step=1.0, key="fullcalc_vat") / 100
            supplier_vat_pct = st.selectbox("НДС поставщика по себестоимости", [0.0, 0.05, 0.07, 0.10, 0.20, 0.22], format_func=lambda value: f"{value * 100:g}%", key="fullcalc_supplier_vat")
            is_ip = st.checkbox("ИП (учитывать страховые взносы)", value=True, key="fullcalc_is_ip")
        with r:
            sales_month = st.number_input("Продаж в месяц, шт.", min_value=0.0, value=float(max(round(suggested_sales), 0)), step=10.0, key="fullcalc_sales_month")
            stock_qty = st.number_input("Остаток на складах, шт.", min_value=0.0, value=0.0, step=10.0, key="fullcalc_stock")
            sku_share = st.number_input("Доля товара в обороте компании, %", min_value=0.0, max_value=100.0, value=100.0, step=1.0, key="fullcalc_share") / 100
            company_monthly_expenses = st.number_input("Общие расходы компании, ₽/мес.", min_value=0.0, value=0.0, step=1000.0, key="fullcalc_company_expenses")
            sku_monthly_expenses = st.number_input("Расходы именно на этот товар, ₽/мес.", min_value=0.0, value=0.0, step=100.0, key="fullcalc_sku_expenses")
        u, v, w = st.columns(3)
        with u:
            fixed_contribution_year = st.number_input("Фиксированные взносы ИП, ₽/год", min_value=0.0, value=57390.0 if is_ip and tax_system != "ОСНО" else 0.0, step=1000.0, key="fullcalc_fixed_contrib")
        with v:
            insurance_on = st.checkbox("Страхование товара Ozon включено (FBO)", value=False, key="fullcalc_insurance_on")
            insurance_daily_pct = st.number_input("Тариф страхования от цены в день, %", min_value=0.0, value=0.0838, step=0.001, key="fullcalc_insurance_rate") / 100
        with w:
            undocumented_expenses = st.number_input("Из расходов выше: без документов, ₽/ед.", min_value=0.0, value=0.0, step=1.0, key="fullcalc_undoc")
            monthly_documented_expenses = st.number_input("Из расходов выше: документированные, ₽/мес.", min_value=0.0, value=0.0, step=100.0, key="fullcalc_doc_expenses")
            monthly_expenses_vat_pct = st.selectbox("Ставка НДС по этим расходам", [0.0, 0.05, 0.07, 0.10, 0.20, 0.22], format_func=lambda value: f"{value * 100:g}%", key="fullcalc_monthly_vat")

    target_margin = st.number_input("Целевая маржинальность для расчёта цены, %", min_value=0.0, max_value=95.0, value=20.0, step=1.0, key="fullcalc_target_margin") / 100
    routes_records = routes.to_dict("records") if not routes.empty else []
    route_settings = [{
        "share": float(pd.to_numeric(pd.Series([row.get("Доля заказов, %", 0)]), errors="coerce").fillna(0).iloc[0]),
        "forward": float(pd.to_numeric(pd.Series([row.get("Доставка, ₽/л", 0)]), errors="coerce").fillna(0).iloc[0]),
        "reverse": float(pd.to_numeric(pd.Series([row.get("Обратная логистика, ₽/л", 0)]), errors="coerce").fillna(0).iloc[0]),
    } for row in routes_records]
    settings = {
        "scheme": scheme, "price_mode": "Цена покупателя" if price_mode == "Цена покупателя" else "Предельная цена продавца",
        "price": sale_price, "cost": cost, "coinvest_pct": coinvest_pct, "volume": volume,
        "commission_pct": commission_pct, "acquiring_pct": acquiring_pct, "deferral_pp": deferral_pp,
        "buyout": buyout_pct, "speed_adjustment_pct": speed_adjustment_pct, "routes": route_settings,
        "fbs_new_rules": fbs_new_rules, "fbs_dispatch_hours": fbs_dispatch_hours,
        "fbs_error_pct": fbs_error_pct, "fbs_on_time": fbs_on_time,
        "free_intake": free_intake, "fbs_intake": fbs_intake, "delivery_pvz_pct": delivery_pvz_pct,
        "repack_tariff": repack_tariff, "repack_share": repack_share, "crossdock_per_liter": crossdock_per_liter,
        "drr_pct": drr_pct, "sales_month": sales_month, "sku_monthly_expenses": sku_monthly_expenses,
        "company_monthly_expenses": company_monthly_expenses, "sku_share": sku_share,
        "stock_qty": stock_qty, "insurance_on": insurance_on, "insurance_daily_pct": insurance_daily_pct,
        "vat_pct": vat_pct, "supplier_vat_pct": supplier_vat_pct,
        "monthly_expenses_vat_pct": monthly_expenses_vat_pct, "ozon_vat_rate": 0.22,
        "tax_system": tax_system, "tax_rate": tax_rate, "is_ip": is_ip,
        "fixed_contribution_year": fixed_contribution_year, "undocumented_expenses": undocumented_expenses,
        "monthly_documented_expenses": monthly_documented_expenses,
    }
    actual = calculate_product_economics(settings)
    current_seller_price = actual["Цена продавца"]
    if actual["Маржинальность"] >= target_margin * 100:
        recommended = current_seller_price
    else:
        lower, upper = current_seller_price, max(current_seller_price * 10.0, cost * 20.0, 1000.0)
        for _ in range(60):
            mid = (lower + upper) / 2.0
            probe = dict(settings, price=mid, price_mode="Предельная цена продавца")
            if calculate_product_economics(probe)["Маржинальность"] >= target_margin * 100:
                upper = mid
            else:
                lower = mid
        recommended = upper
    recommended_settings = dict(settings, price=recommended, price_mode="Предельная цена продавца")
    forecast = calculate_product_economics(recommended_settings)
    target_reachable = forecast["Маржинальность"] + 0.05 >= target_margin * 100

    st.markdown("#### Результат")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Рекомендуемая предельная цена", f"{recommended:,.2f} ₽" if target_reachable else "не достигается")
    m2.metric("Чистая прибыль при введённой цене", f"{actual['Чистая прибыль']:,.2f} ₽")
    m3.metric("Маржинальность", f"{actual['Маржинальность']:.1f}%")
    m4.metric("При целевой цене", f"{forecast['Чистая прибыль']:,.2f} ₽ · {forecast['Маржинальность']:.1f}%")
    pnl = pd.DataFrame({
        "Статья": list(actual.keys()),
        "При введённой цене, ₽": list(actual.values()),
        "При целевой цене, ₽": [forecast.get(key, 0.0) for key in actual.keys()],
    })
    st.dataframe(pnl, use_container_width=True, hide_index=True, height=520, column_config={
        "При введённой цене, ₽": st.column_config.NumberColumn(format="%.2f"),
        "При целевой цене, ₽": st.column_config.NumberColumn(format="%.2f"),
    })
    st.download_button("Скачать P&L нового товара CSV", pnl.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"), "ozon_new_product_pnl.csv", "text/csv")
    if not target_reachable:
        st.error("С такой комиссией, налогами и расходами целевая маржинальность не достигается даже при существенно большей цене. Проверьте тарифы и целевой процент.")
    st.info("Комиссия, тарифы логистики и налоги задаются как ваши параметры расчёта. Соинвест ограничивает погашение услуг Ozon 99%; расходы на отправления пересчитываются на одну выкупленную единицу. Перед публикацией цены сверьте допущения с тарифами и налоговым режимом продавца.")


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
    with st.expander("Открыть полный калькулятор нового товара", expanded=True):
        empty_report = pd.DataFrame({
            "net_units": pd.Series(dtype=float), "price": pd.Series(dtype=float),
            "cost_price": pd.Series(dtype=float),
        })
        render_full_product_calculator(empty_report)
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
            if "scheme" not in report.columns:
                report["scheme"] = "Не указана"
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
            ] + [column for column in OZON_EXPENSE_COLUMNS if column in report.columns]
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
            tab_report, tab_price, tab_new_product, tab_full_product = st.tabs([
                "Юнит-экономика", "Целевая маржинальность", "Быстрый калькулятор",
                "Полный калькулятор товара",
            ])
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
            with tab_new_product:
                st.subheader("Цена для нового товара")
                st.caption(
                    "Калькулятор берёт средневзвешенную комиссию и эквайринг как долю от цены, "
                    "а остальные расходы Ozon — как среднюю сумму на проданную единицу. "
                    "Укажите себестоимость нового товара и желаемую маржинальность."
                )
                available_expense_columns = [
                    column for column in OZON_EXPENSE_COLUMNS if column in report.columns
                ]
                if not available_expense_columns:
                    st.warning("В загруженной таблице нет столбцов с расходами Ozon для расчёта средней доли.")
                else:
                    with st.form("new_product_price_form"):
                        form_scheme, form_cost, form_margin = st.columns(3)
                        available_schemes = sorted(
                            report["scheme"].dropna().astype(str).str.strip().loc[lambda values: values != ""].unique().tolist()
                        )
                        scheme_options = ["Все схемы"] + available_schemes
                        scheme_units = report.groupby("scheme")["net_units"].sum().sort_values(ascending=False)
                        default_scheme = scheme_units.index[0] if not scheme_units.empty else "Все схемы"
                        default_scheme_index = scheme_options.index(default_scheme) if default_scheme in scheme_options else 0
                        with form_scheme:
                            selected_scheme = st.selectbox("Схема продаж", scheme_options, index=default_scheme_index)
                        with form_cost:
                            new_product_cost = st.number_input(
                                "Себестоимость товара, ₽", min_value=0.0, value=0.0,
                                step=50.0, key="new_product_cost",
                            )
                        with form_margin:
                            new_product_margin_pct = st.number_input(
                                "Целевая маржинальность, %", min_value=0.0, max_value=95.0,
                                value=30.0, step=1.0, key="new_product_margin_pct",
                            )
                        st.form_submit_button("Рассчитать цену")

                    sales = report.copy()
                    sales["scheme"] = sales["scheme"].fillna("").astype(str).str.strip()
                    if selected_scheme != "Все схемы":
                        sales = sales.loc[sales["scheme"] == selected_scheme]
                    sales["sales_base"] = sales["price"] * sales["net_units"]
                    eligible_sales = sales.loc[(sales["net_units"] > 0) & (sales["sales_base"] > 0)]
                    weighted_price_base = eligible_sales["sales_base"].sum()
                    available_variable_columns = [
                        column for column in OZON_VARIABLE_EXPENSE_COLUMNS if column in eligible_sales.columns
                    ]
                    available_fixed_columns = [
                        column for column in OZON_FIXED_EXPENSE_COLUMNS if column in eligible_sales.columns
                    ]
                    variable_expenses = -eligible_sales[available_variable_columns].sum().sum()
                    expense_rate = variable_expenses / weighted_price_base if weighted_price_base > 0 else None
                    sold_units = eligible_sales["net_units"].sum()
                    fixed_expenses_per_unit = (
                        -eligible_sales[available_fixed_columns].sum().sum() / sold_units
                        if sold_units > 0 else None
                    )
                    if expense_rate is None:
                        st.warning("Для выбранной схемы нет продаж с положительной ценой и количеством.")
                    elif new_product_cost <= 0:
                        st.info("Введите себестоимость нового товара, чтобы получить рекомендуемую цену.")
                    else:
                        st.caption(
                            f"По схеме «{selected_scheme}»: комиссия и эквайринг в среднем составляют "
                            f"{expense_rate * 100:.2f}% от текущей цены; прочие расходы — "
                            f"{fixed_expenses_per_unit:,.2f} ₽ на проданную единицу."
                        )
                        margin_rate = new_product_margin_pct / 100
                        denominator = 1 - expense_rate - margin_rate
                        if denominator <= 0:
                            st.error(
                                "При такой доле расходов Ozon и целевой маржинальности "
                                "расчётная цена не имеет конечного значения. Снизьте целевую маржинальность "
                                "или выберите другую схему продаж."
                            )
                        else:
                            recommended_new_price = (new_product_cost + fixed_expenses_per_unit) / denominator
                            estimated_variable_expenses = recommended_new_price * expense_rate
                            estimated_ozon_expenses = estimated_variable_expenses + fixed_expenses_per_unit
                            estimated_profit = recommended_new_price - new_product_cost - estimated_ozon_expenses
                            metric_price, metric_expenses, metric_profit, metric_margin = st.columns(4)
                            metric_price.metric("Рекомендуемая цена", f"{recommended_new_price:,.2f} ₽")
                            metric_expenses.metric("Расходы Ozon по оценке", f"{estimated_ozon_expenses:,.2f} ₽")
                            metric_profit.metric("Прибыль после расходов и себестоимости", f"{estimated_profit:,.2f} ₽")
                            metric_margin.metric("Маржинальность", f"{estimated_profit / recommended_new_price * 100:.2f}%")
                            st.caption(
                                "Формула: цена = (себестоимость + средние прочие расходы на единицу) / "
                                "(1 − доля комиссии и эквайринга − целевая маржинальность). "
                                "Это оценка по выбранной схеме и периоду выгрузки; тарифы, логистика и продвижение нового товара могут отличаться."
                            )
            with tab_full_product:
                render_full_product_calculator(report)
        except (RuntimeError, ValueError, requests.RequestException, ImportError) as exc:
            st.error(f"Не удалось обработать файл или загрузить себестоимость: {exc}")
        except Exception as exc:
            st.error(f"При обработке данных возникла непредвиденная ошибка: {exc}")
else:
    st.info("Файл загружен. Нажмите «Рассчитать по таблице».")
    with st.expander("Открыть полный калькулятор нового товара", expanded=True):
        empty_report = pd.DataFrame({
            "net_units": pd.Series(dtype=float), "price": pd.Series(dtype=float),
            "cost_price": pd.Series(dtype=float),
        })
        render_full_product_calculator(empty_report)

st.markdown("---")
st.caption("Источник продаж, начислений Ozon, цены и показателей прибыли — загруженная XLSX-выгрузка юнит-экономики. Историческая средневзвешенная себестоимость загружается из проведённых документов «Приёмка» и «Оприходование» в МоемСкладе.")

