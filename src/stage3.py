"""Stage 3: does a SEBI order cost the firm anything? An event study (runbook Week 7; technical plan §7.1–7.2; paper §6.3).

**Events.** The frozen sample's public actions on fraud firms (``sample_events``): orders naming the firm's company as
the issuer, and interim orders recited in later orders. Each event takes the finality of its orders
(``order_finality``): ``INTERIM`` (any interim order that day, or a recited one), ``CONFIRMATION``, ``FINAL``,
``SETTLEMENT``; takeover exemptions and corrigenda are dropped. The **primary sample** is each firm's earliest public
action, split by its type (later events are contaminated by the first); the first event of each type per firm is the
secondary sample.

**Abnormal returns.** Adjusted daily closes on the firm's main exchange, on the BSE 500 calendar (a day without a trade
has a zero return; its price change lands on the next trade). Market model over days −250 to −30 with a **Dimson
beta** (lagged, same-day and lead index returns summed); the plain OLS beta is the check. Day 0 is the first trading day
on or after the order's date. Windows (−1,+1), (−5,+5), (0,+10), and (−30,−2) for leakage. A firm needs trades on
60% of estimation days. A firm that traded in the estimation window but not on any of days 0 to +10 is **suspended at
the event**: kept out of the averages, counted, and used to bound them.

**Inference.** The cross-sectional *t*, the standardised cross-sectional test of Boehmer, Musumeci and Poulsen (1991),
and the generalised rank test (Corrado 1989, Cowan 1992). A liquid subsample trades on at least 90% of estimation days.

**Cross-section and the reputational gap.** CAR on log penalty, debarment, promoter or director named, issuer a
noticee, event type, category and liquidity (OLS, HC1 errors); with log market cap where ownership gives shares
(FY2016 onward). Value lost = −CAR(0,+10) × market cap at the end of the previous financial year, against the
monetary sanctions of all the firm's orders.

Reads ``sample_events``, ``sample``, ``order_finality``, ``orders``, ``order_entities``, ``entity_matches``,
``universe``, ``prices/``, ``corporate_actions``, ``market_index``, ``panel``. Writes ``derived/stage3/``,
``derived/stage3_numbers.json``, ``paper/tables/stage3_*.tex``, ``paper/figures_stage3.pdf``, ``logs/stage3.log``.
No network.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib
import warnings

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from matplotlib.backends.backend_pdf import PdfPages
from scipy import stats

from src import cache
from src.extract_text import setup_logging
from src.paper_tables import ACCENT, INK, NEUTRAL, NEUTRAL_DARK, n, pct, set_style, tabular

log = logging.getLogger("stage3")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
PAPER_DIR = REPO_ROOT / "paper"
LOG_FILE = REPO_ROOT / "logs" / "stage3.log"

ESTIMATION = (-250, -30)
WINDOWS = {"(-1,+1)": (-1, 1), "(-5,+5)": (-5, 5), "(0,+10)": (0, 10), "(-30,-2)": (-30, -2)}
#: The windows the paper's table shows ((−5,+5) stays in ``tests.csv``).
TABLE_WINDOWS = ("(-1,+1)", "(0,+10)", "(-30,-2)")
PLOT_RANGE = (-30, 30)
MIN_TRADED_SHARE = 0.6
LIQUID_SHARE = 0.9
#: A raw daily return beyond these marks a broken price series (an unadjusted consolidation, a bad print).
JUMP_UP, JUMP_DOWN = 2.0, -0.8
#: CARs are winsorised at these pooled percentiles for the mean-based tests.
WINSOR = (0.01, 0.99)
EVENT_TYPES = ("INTERIM", "CONFIRMATION", "FINAL", "SETTLEMENT")
TYPE_LABELS = {"INTERIM": "Interim order", "CONFIRMATION": "Confirmation or revocation", "FINAL": "Final order",
               "SETTLEMENT": "Settlement order", "ALL": "All first actions"}
FINALITY_TO_TYPE = {"INTERIM": "INTERIM", "CONFIRMATION_OR_EXTENSION": "CONFIRMATION", "FINAL": "FINAL",
                    "SETTLEMENT_FINAL": "SETTLEMENT", "UNKNOWN": "FINAL"}
CRORE = 1e7


# ----- events ------------------------------------------------------------------------------------------------------


def event_type(finalities: list[str], kind: str) -> str | None:
    """The type of an event from the finality of its orders.

    Args:
        finalities (list[str]): the ``finality`` of each order in the event.
        kind (str): ``order`` or ``interim_recited``.

    Returns:
        str | None: one of ``EVENT_TYPES``, or None when every order is ``OTHER`` (not enforcement).
    """
    if kind == "interim_recited":
        return "INTERIM"
    mapped = [FINALITY_TO_TYPE[f] for f in finalities if f in FINALITY_TO_TYPE]
    if not mapped:
        return None
    for candidate in EVENT_TYPES:
        if candidate in mapped:
            return candidate
    return None


def build_events(derived: pathlib.Path) -> pd.DataFrame:
    """Typed events with their sanctions and parties.

    Args:
        derived (pathlib.Path): directory with ``sample_events``, ``order_finality``, ``orders``, ``order_entities``,
            ``entity_matches``.

    Returns:
        pd.DataFrame: one row per event: ``firm_id``, ``event_date``, ``kind``, ``event_type``, ``text_stems`` (list),
            ``first_public`` (the firm's earliest event), ``first_of_type``, ``penalty`` (₹, the event's orders),
            ``issuer_penalty`` (₹, imposed on the firm itself), ``debarred``, ``promoter_named``, ``issuer_noticee``,
            ``category``, ``firm_penalty_total`` (₹, all the firm's events), ``firm_issuer_penalty_total`` (₹, all
            imposed on the firm itself: ``order_entities.penalty_amount`` of the parties matched to the firm, a joint
            amount in full since the firm is liable for it).
    """
    events = pd.read_parquet(derived / "sample_events.parquet")
    events["event_date"] = pd.to_datetime(events.event_date)
    events["stems"] = events.text_stems.map(lambda s: [str(x) for x in json.loads(s)] if isinstance(s, str) else [])
    finality = pd.read_parquet(derived / "order_finality.parquet", columns=["text_stem", "finality"])
    finality_of = dict(zip(finality.text_stem.astype(str), finality.finality))
    events["event_type"] = [event_type([finality_of.get(s, "UNKNOWN") for s in stems], kind)
                            for stems, kind in zip(events.stems, events.kind)]
    dropped = events.event_type.isna().sum()
    events = events[events.event_type.notna()].copy()
    log.info("events             %d typed (%d non-enforcement dropped): %s", len(events), dropped,
             events.event_type.value_counts().to_dict())

    orders = pd.read_parquet(derived / "orders.parquet",
                             columns=["text_stem", "penalty_total", "settlement_amount", "disgorgement_total", "debarred",
                                      "fraud_category"])
    orders["text_stem"] = orders.text_stem.astype(str)
    orders = orders.set_index("text_stem")
    entities = pd.read_parquet(derived / "order_entities.parquet", columns=["text_stem", "entity_role"])
    promoter_stems = set(entities[entities.entity_role.isin(["PROMOTER", "DIRECTOR"])].text_stem.astype(str))
    matches = pd.read_parquet(derived / "entity_matches.parquet", columns=["text_stem", "party_no", "firm_id"])
    matches["text_stem"] = matches.text_stem.astype(str)
    noticee_pairs = set(zip(matches.text_stem, matches.firm_id))
    party_penalty = pd.read_parquet(derived / "order_entities.parquet", columns=["text_stem", "party_no", "penalty_amount"])
    party_penalty["text_stem"] = party_penalty.text_stem.astype(str)
    issuer_penalty = matches.merge(party_penalty, on=["text_stem", "party_no"], how="left")
    issuer_penalty = issuer_penalty.groupby(["text_stem", "firm_id"]).penalty_amount.sum().to_dict()

    def money(stems: list[str]) -> float:
        """Penalties, settlement amounts and disgorgement of some orders.

        Args:
            stems (list[str]): text stems.

        Returns:
            float: ₹ total.
        """
        rows = orders.loc[[s for s in stems if s in orders.index]]
        return float(rows[["penalty_total", "settlement_amount", "disgorgement_total"]].fillna(0).sum().sum())

    events["penalty"] = [money(stems) if kind == "order" else 0.0 for stems, kind in zip(events.stems, events.kind)]
    events["debarred"] = [bool(orders.loc[[s for s in stems if s in orders.index], "debarred"].fillna(False).any())
                          for stems in events.stems]
    events["issuer_penalty"] = [float(sum(issuer_penalty.get((s, firm), 0.0) for s in stems))
                                for stems, firm in zip(events.stems, events.firm_id)]
    events["promoter_named"] = [any(s in promoter_stems for s in stems) for stems in events.stems]
    events["issuer_noticee"] = [any((s, firm) in noticee_pairs for s in stems)
                                for stems, firm in zip(events.stems, events.firm_id)]
    events["category"] = [json.loads(c)[0] if isinstance(c, str) and json.loads(c) else "OTHER"
                          for c in events.categories]
    events = events.sort_values(["firm_id", "event_date"]).reset_index(drop=True)
    events["first_public"] = ~events.duplicated("firm_id", keep="first")
    events["first_of_type"] = ~events.duplicated(["firm_id", "event_type"], keep="first")
    events["firm_penalty_total"] = events.groupby("firm_id").penalty.transform("sum")
    events["firm_issuer_penalty_total"] = events.groupby("firm_id").issuer_penalty.transform("sum")
    return events.drop(columns=["text_stems"]).rename(columns={"stems": "text_stems"})


# ----- returns -----------------------------------------------------------------------------------------------------


def firm_returns(firms: set[str], derived: pathlib.Path) -> tuple[pd.DataFrame, pd.Series]:
    """Adjusted daily closes of the firms on their main exchanges, and the market's daily return.

    Args:
        firms (set[str]): firm ids.
        derived (pathlib.Path): directory with ``universe``, ``prices/``, ``corporate_actions``, ``market_index``.

    Returns:
        tuple[pd.DataFrame, pd.Series]: daily rows (``firm_id``, ``date``, ``adj_close``, ``volume``) and the BSE 500
            daily return indexed by date.
    """
    from src.build_panel import firm_daily, read_prices, security_map
    from src.universe import NSE_SYMBOLCHANGE, _csv, symbol_chain

    universe = pd.read_parquet(derived / "universe.parquet")
    symbolchange = symbol_chain(_csv(cache.get(NSE_SYMBOLCHANGE, offline=True)))
    years = range(2004, 2027)
    prices_dir = derived / "prices"
    nse_isin = [pd.read_parquet(p, columns=["code", "isin"]).dropna().drop_duplicates()
                for p in (prices_dir / f"NSE_{y}.parquet" for y in years) if p.exists()]
    smap = security_map(universe, pd.concat(nse_isin), symbolchange)
    smap = smap[smap.firm_id.isin(firms)]
    codes = {exchange: set(smap[smap.exchange == exchange].code) for exchange in ("NSE", "BSE")}
    firm_bse = universe[universe.exchange == "BSE"].groupby("firm_id").code.agg(list).to_dict()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        daily, _ = firm_daily(read_prices(prices_dir, codes, years), smap,
                              pd.read_parquet(derived / "corporate_actions.parquet"), firm_bse)
    daily = daily[["firm_id", "date", "adj_close", "volume"]].copy()
    daily["date"] = pd.to_datetime(daily.date)
    index = pd.read_parquet(derived / "market_index.parquet", columns=["date", "close"])
    index["date"] = pd.to_datetime(index.date)
    index = index.drop_duplicates("date").set_index("date").close.sort_index()
    return daily, index.pct_change().dropna()


def dimson_fit(r: np.ndarray, m_lag: np.ndarray, m: np.ndarray, m_lead: np.ndarray) -> tuple[float, float, float]:
    """Market-model parameters with a Dimson beta.

    Args:
        r (np.ndarray): the firm's returns over the estimation window.
        m_lag (np.ndarray): the market's return the day before.
        m (np.ndarray): the market's return the same day.
        m_lead (np.ndarray): the market's return the day after.

    Returns:
        tuple[float, float, float]: alpha, Dimson beta, and the plain OLS beta.
    """
    x = np.column_stack([np.ones_like(m), m_lag, m, m_lead])
    coef, *_ = np.linalg.lstsq(x, r, rcond=None)
    beta_d = float(coef[1] + coef[2] + coef[3])
    alpha = float(r.mean() - beta_d * m.mean())
    x1 = np.column_stack([np.ones_like(m), m])
    beta_ols = float(np.linalg.lstsq(x1, r, rcond=None)[0][1])
    return alpha, beta_d, beta_ols


def abnormal_returns(events: pd.DataFrame, daily: pd.DataFrame, market: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Abnormal returns from day −250 to +30 for every event with prices.

    Args:
        events (pd.DataFrame): from ``build_events``.
        daily (pd.DataFrame): from ``firm_returns``.
        market (pd.Series): market daily returns.

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: per event (``event_id`` = row index of ``events``) the estimation statistics
            (``status`` ∈ ``ok, no_prices, thin, suspended, price_jump, out_of_range``, ``traded_share``, ``alpha``, ``beta``,
            ``beta_ols``, ``sigma``, ``day0``, ``n_est``), and the long table of ``event_id``, ``rel``, ``ar``,
            ``ar_ols``, ``traded``.
    """
    calendar = market.index
    m_values = market.to_numpy()
    by_firm = {firm: rows.set_index("date") for firm, rows in daily.groupby("firm_id")}
    stats_rows, ar_rows = [], []
    lo, hi = ESTIMATION[0], PLOT_RANGE[1]
    for event_id, event in events.iterrows():
        record = {"event_id": event_id, "status": "ok"}
        prices = by_firm.get(event.firm_id)
        day0 = calendar.searchsorted(event.event_date)
        if prices is None or prices.empty:
            stats_rows.append({**record, "status": "no_prices"})
            continue
        if day0 + lo - 2 < 0 or day0 + hi + 2 > len(calendar):
            stats_rows.append({**record, "status": "out_of_range"})
            continue
        days = calendar[day0 + lo - 2: day0 + hi + 2]
        close = prices.adj_close.reindex(days)
        traded = (prices.volume.reindex(days).fillna(0) > 0).to_numpy()
        filled = close.ffill()
        r = filled.pct_change().fillna(0).to_numpy()
        m = m_values[day0 + lo - 2: day0 + hi + 2]
        rel = np.arange(lo - 2, hi + 2)
        est = (rel >= ESTIMATION[0]) & (rel <= ESTIMATION[1]) & np.isfinite(filled.to_numpy())
        record.update(day0=calendar[day0], traded_share=float(traded[(rel >= ESTIMATION[0])
                                                                      & (rel <= ESTIMATION[1])].mean()))
        if record["traded_share"] < MIN_TRADED_SHARE or est.sum() < 60:
            stats_rows.append({**record, "status": "thin"})
            continue
        window = (rel >= 0) & (rel <= 10)
        if not traded[window].any():
            record["status"] = "suspended"
        span = (rel >= ESTIMATION[0]) & (rel <= hi)
        if (r[span] > JUMP_UP).any() or (r[span] < JUMP_DOWN).any():
            stats_rows.append({**record, "status": "price_jump"})
            continue
        idx = np.where(est)[0]
        alpha, beta, beta_ols = dimson_fit(r[idx], m[idx - 1], m[idx], m[idx + 1])
        ar = r - alpha - beta * m
        alpha_ols = float(r[idx].mean() - beta_ols * m[idx].mean())
        ar_ols = r - alpha_ols - beta_ols * m
        sigma = float(np.std(ar[idx], ddof=2))
        sigma = sigma if sigma > 0 else math.nan
        record.update(alpha=alpha, beta=beta, beta_ols=beta_ols, sigma=sigma, n_est=int(est.sum()))
        stats_rows.append(record)
        keep = (rel >= lo) & (rel <= hi)
        ar_rows.append(pd.DataFrame({"event_id": event_id, "rel": rel[keep], "ar": ar[keep], "ar_ols": ar_ols[keep],
                                     "traded": traded[keep]}))
    return pd.DataFrame(stats_rows), pd.concat(ar_rows, ignore_index=True)


def cars(ars: pd.DataFrame, est_stats: pd.DataFrame, column: str = "ar") -> pd.DataFrame:
    """Cumulative abnormal returns per event and window, with the standardised CAR.

    Args:
        ars (pd.DataFrame): the long AR table.
        est_stats (pd.DataFrame): per-event estimation statistics (``sigma``).
        column (str): ``ar`` (Dimson) or ``ar_ols``.

    Returns:
        pd.DataFrame: ``event_id``, ``window``, ``car``, ``scar`` (CAR / (σ√L)).
    """
    sigma = est_stats.set_index("event_id").sigma
    rows = []
    for name, (start, end) in WINDOWS.items():
        part = ars[(ars.rel >= start) & (ars.rel <= end)].groupby("event_id")[column].sum()
        length = end - start + 1
        rows.append(pd.DataFrame({"event_id": part.index, "window": name, "car": part.to_numpy(),
                                  "scar": part.to_numpy() / (sigma.reindex(part.index).to_numpy() * math.sqrt(length))}))
    return pd.concat(rows, ignore_index=True)


def rank_test(ars: pd.DataFrame, ids: list, start: int, end: int) -> float:
    """The generalised rank test (Corrado 1989; Cowan 1992) for a multi-day window.

    Args:
        ars (pd.DataFrame): the long AR table.
        ids (list): the events in the group.
        start (int): first day of the window.
        end (int): last day.

    Returns:
        float: the z statistic (NaN with fewer than five events).
    """
    part = ars[ars.event_id.isin(ids) & (ars.rel >= ESTIMATION[0]) & (ars.rel <= max(end, 10))]
    if part.event_id.nunique() < 5:
        return math.nan
    part = part.assign(u=part.groupby("event_id").ar.rank() / (part.groupby("event_id").ar.transform("count") + 1)
                       - 0.5)
    by_day = part.groupby("rel").u.mean()
    sd = math.sqrt((by_day ** 2).mean())
    window = by_day.loc[start:end]
    return float(window.sum() / (math.sqrt(len(window)) * sd)) if sd > 0 else math.nan


def test_group(group: pd.DataFrame, ars: pd.DataFrame, window: str) -> dict:
    """Mean and median CAR with three tests, for one group of events and one window.

    The mean and the *t* use ``car_w`` and the BMP test ``scar_w`` (winsorised) when the group carries them; the median
    and the rank test use the raw abnormal returns.

    Args:
        group (pd.DataFrame): CAR rows of the group (one window).
        ars (pd.DataFrame): the long AR table (for the rank test).
        window (str): the window's name.

    Returns:
        dict: ``n``, ``mean`` (winsorised when ``car_w`` is given), ``mean_raw``, ``median``, ``neg_share``, ``t``,
            ``p_t``, ``bmp``, ``p_bmp``, ``rank``, ``p_rank``.
    """
    car = group.car.dropna()
    car_w = group.car_w.dropna() if "car_w" in group else car
    scar = (group.scar_w if "scar_w" in group else group.scar).replace([np.inf, -np.inf], np.nan).dropna()
    count = len(car)
    if count < 5:
        return {"n": count}
    t = car_w.mean() / (car_w.std(ddof=1) / math.sqrt(count))
    bmp = scar.mean() / (scar.std(ddof=1) / math.sqrt(len(scar)))
    start, end = WINDOWS[window]
    rank = rank_test(ars, group.event_id.tolist(), start, end)
    return {"n": count, "mean": car_w.mean(), "mean_raw": car.mean(), "median": car.median(),
            "neg_share": float((car < 0).mean()),
            "t": t, "p_t": 2 * stats.norm.sf(abs(t)), "bmp": bmp, "p_bmp": 2 * stats.norm.sf(abs(bmp)),
            "rank": rank, "p_rank": 2 * stats.norm.sf(abs(rank)) if np.isfinite(rank) else math.nan}


def winsorise(table: pd.DataFrame) -> pd.DataFrame:
    """Add ``car_w`` and ``scar_w``: CAR and standardised CAR clipped at the pooled ``WINSOR`` percentiles per window.

    Args:
        table (pd.DataFrame): CAR rows (``window``, ``car``, ``scar``).

    Returns:
        pd.DataFrame: the table with the two columns.
    """
    out = table.copy()
    for column in ("car", "scar"):
        values = out[column].replace([np.inf, -np.inf], np.nan)
        low = values.groupby(out.window).transform(lambda v: v.quantile(WINSOR[0]))
        high = values.groupby(out.window).transform(lambda v: v.quantile(WINSOR[1]))
        out[f"{column}_w"] = values.clip(lower=low, upper=high)
    return out


def run_tests(events: pd.DataFrame, car_table: pd.DataFrame, ars: pd.DataFrame, est_stats: pd.DataFrame) -> pd.DataFrame:
    """Every group × window: primary sample by type, first-of-type, liquid subsample, OLS-beta check.

    Args:
        events (pd.DataFrame): typed events.
        car_table (pd.DataFrame): Dimson CARs.
        ars (pd.DataFrame): the long AR table.
        est_stats (pd.DataFrame): estimation statistics.

    Returns:
        pd.DataFrame: one row per (sample, group, window) with ``test_group``'s columns.
    """
    ok = est_stats[est_stats.status == "ok"].set_index("event_id")
    frame = winsorise(car_table[car_table.event_id.isin(ok.index)]).merge(
        events[["event_type", "first_public", "first_of_type"]], left_on="event_id", right_index=True)
    frame["liquid"] = frame.event_id.map(ok.traded_share) >= LIQUID_SHARE
    ols = winsorise(cars(ars[ars.event_id.isin(ok.index)], est_stats, "ar_ols"))
    ols = ols.merge(events[["event_type", "first_public"]], left_on="event_id", right_index=True)
    samples = {
        "First public action": frame[frame.first_public],
        "First public action, liquid": frame[frame.first_public & frame.liquid],
        "First of each type": frame[frame.first_of_type],
        "First public action, OLS beta": ols[ols.first_public],
    }
    rows = []
    for sample, data in samples.items():
        for window in WINDOWS:
            part = data[data.window == window]
            rows.append({"sample": sample, "group": "ALL", "window": window, **test_group(part, ars, window)})
            for kind in EVENT_TYPES:
                rows.append({"sample": sample, "group": kind, "window": window,
                             **test_group(part[part.event_type == kind], ars, window)})
    return pd.DataFrame(rows)


# ----- suspensions, cross-section, the gap -------------------------------------------------------------------------


def suspension_bounds(events: pd.DataFrame, car_table: pd.DataFrame, est_stats: pd.DataFrame) -> pd.DataFrame:
    """Suspended firms: counts by type and the mean CAR(0,+10) under assumptions about their returns.

    Args:
        events (pd.DataFrame): typed events.
        car_table (pd.DataFrame): CARs.
        est_stats (pd.DataFrame): estimation statistics.

    Returns:
        pd.DataFrame: per type (first public actions): ``n_ok``, ``n_suspended``, ``mean_observed``,
            ``mean_if_zero``, ``mean_if_p10``, ``mean_if_minus50``.
    """
    status = est_stats.set_index("event_id").status
    first = events[events.first_public]
    car = car_table[car_table.window == "(0,+10)"].set_index("event_id").car_w
    rows = []
    for kind in ("ALL",) + EVENT_TYPES:
        part = first if kind == "ALL" else first[first.event_type == kind]
        ids_ok = [i for i in part.index if status.get(i) == "ok" and i in car.index]
        n_susp = sum(status.get(i) == "suspended" for i in part.index)
        observed = car.loc[ids_ok]
        if observed.empty:
            continue
        p10 = observed.quantile(0.10)

        def mean_with(value: float) -> float:
            """Mean CAR with the suspended events set to a value.

            Args:
                value (float): the assumed CAR of a suspended event.

            Returns:
                float: the mean.
            """
            return float((observed.sum() + value * n_susp) / (len(observed) + n_susp))

        rows.append({"group": kind, "n_ok": len(observed), "n_suspended": n_susp, "mean_observed": observed.mean(),
                     "mean_if_zero": mean_with(0.0), "mean_if_p10": mean_with(p10), "mean_if_minus50": mean_with(-0.5)})
    return pd.DataFrame(rows)


def market_caps(events: pd.DataFrame, derived: pathlib.Path) -> pd.Series:
    """Market capitalisation (₹) at the end of the financial year before each event.

    Args:
        events (pd.DataFrame): typed events.
        derived (pathlib.Path): directory with ``panel``.

    Returns:
        pd.Series: ₹ per event id (NaN where the panel has no market cap).
    """
    panel = pd.read_parquet(derived / "panel.parquet", columns=["firm_id", "fy", "market_cap"])
    caps = panel.dropna(subset=["market_cap"]).set_index(["firm_id", "fy"]).market_cap
    fy = events.event_date.dt.year + (events.event_date.dt.month >= 4).astype(int)
    keys = list(zip(events.firm_id, fy - 1))
    return pd.Series([caps.get(key, np.nan) * CRORE for key in keys], index=events.index)


def cross_section(events: pd.DataFrame, car_table: pd.DataFrame, est_stats: pd.DataFrame,
                  caps: pd.Series) -> pd.DataFrame:
    """OLS of CAR on the sanction and the firm (first public actions), HC1 errors.

    Args:
        events (pd.DataFrame): typed events.
        car_table (pd.DataFrame): winsorised CARs (``car_w``).
        est_stats (pd.DataFrame): estimation statistics.
        caps (pd.Series): market caps (₹) per event.

    Returns:
        pd.DataFrame: ``model``, ``window``, ``term``, ``coef``, ``se``, ``p``, ``n``, ``r2``.
    """
    ok = est_stats[est_stats.status == "ok"].set_index("event_id")
    first = events[events.first_public & events.index.isin(ok.index)].copy()
    first["log_penalty"] = np.log1p(first.penalty / 1e5)
    first["log_firm_penalty"] = np.log1p(first.firm_penalty_total / 1e5)
    first["interim"] = (first.event_type == "INTERIM").astype(int)
    first["settlement"] = (first.event_type == "SETTLEMENT").astype(int)
    first["liquidity"] = ok.traded_share.reindex(first.index)
    first["log_mcap"] = np.log(caps.reindex(first.index) / CRORE)
    for category in ("MARKET_MANIPULATION", "DISCLOSURE_FAILURE", "INSIDER_TRADING"):
        first[f"cat_{category}"] = (first.category == category).astype(int)
    for column in ("debarred", "promoter_named", "issuer_noticee"):
        first[column] = first[column].astype(int)
    base = ["log_penalty", "debarred", "promoter_named", "issuer_noticee", "interim", "settlement", "liquidity",
            "cat_MARKET_MANIPULATION", "cat_DISCLOSURE_FAILURE", "cat_INSIDER_TRADING"]
    specs = {"(1) Sanction and firm": base, "(2) + log market cap": base + ["log_mcap"]}
    rows = []
    for window in ("(-1,+1)", "(0,+10)"):
        car = car_table[car_table.window == window].set_index("event_id").car_w
        data = first.assign(car=car.reindex(first.index))
        for model, terms in specs.items():
            sample = data[["car"] + terms].dropna()
            if len(sample) < 30:
                continue
            fit = sm.OLS(sample.car, sm.add_constant(sample[terms])).fit(cov_type="HC1")
            for term in fit.params.index:
                rows.append({"model": model, "window": window, "term": term, "coef": fit.params[term],
                             "se": fit.bse[term], "p": fit.pvalues[term], "n": int(fit.nobs), "r2": fit.rsquared})
    return pd.DataFrame(rows)


def reputational_gap(events: pd.DataFrame, car_table: pd.DataFrame, est_stats: pd.DataFrame,
                     caps: pd.Series) -> pd.DataFrame:
    """Market value lost around the first public action against the monetary sanctions.

    Args:
        events (pd.DataFrame): typed events.
        car_table (pd.DataFrame): CARs.
        est_stats (pd.DataFrame): estimation statistics.
        caps (pd.Series): market caps (₹) per event.

    Returns:
        pd.DataFrame: per type: ``n``, ``median_loss`` / ``mean_loss`` (₹ crore, value lost = −CAR(0,+10) × cap),
            ``median_sanction`` (₹ crore, all the firm's orders), ``loss_sum``, ``sanction_sum``, ``ratio_of_sums``,
            ``share_loss_exceeds``, and the same against sanctions on the firm itself (``median_issuer_sanction``,
            ``issuer_sanction_sum``, ``share_issuer_sanctioned``, ``share_loss_exceeds_issuer``).
    """
    ok = set(est_stats[est_stats.status == "ok"].event_id)
    car = car_table[car_table.window == "(0,+10)"].set_index("event_id").car_w
    first = events[events.first_public & events.index.isin(ok)].copy()
    first["cap"] = caps.reindex(first.index)
    first["loss"] = -car.reindex(first.index) * first.cap / CRORE
    first["sanction"] = first.firm_penalty_total / CRORE
    first["issuer_sanction"] = first.firm_issuer_penalty_total / CRORE
    first = first.dropna(subset=["loss"])
    rows = []
    for kind in ("ALL",) + EVENT_TYPES:
        part = first if kind == "ALL" else first[first.event_type == kind]
        if len(part) < 5:
            continue
        rows.append({"group": kind, "n": len(part), "median_loss": part.loss.median(), "mean_loss": part.loss.mean(),
                     "median_sanction": part.sanction.median(), "loss_sum": part.loss.sum(),
                     "sanction_sum": part.sanction.sum(),
                     "ratio_of_sums": part.loss.sum() / part.sanction.sum() if part.sanction.sum() else np.nan,
                     "share_loss_exceeds": float((part.loss > part.sanction).mean()),
                     "median_issuer_sanction": part.issuer_sanction.median(),
                     "issuer_sanction_sum": part.issuer_sanction.sum(),
                     "share_issuer_sanctioned": float((part.issuer_sanction > 0).mean()),
                     "share_loss_exceeds_issuer": float((part.loss > part.issuer_sanction).mean())})
    return pd.DataFrame(rows)


# ----- outputs -----------------------------------------------------------------------------------------------------


def stars(p: float) -> str:
    """Significance stars.

    Args:
        p (float): p-value.

    Returns:
        str: ``***``, ``**``, ``*`` or empty.
    """
    if not np.isfinite(p):
        return ""
    return "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""


def table_cars(tests: pd.DataFrame) -> str:
    """LaTeX: mean CARs (%) with the BMP and rank tests, first public actions by type, and the checks.

    Each cell is the mean CAR in percent with stars from the BMP test and daggers from the rank test.

    Args:
        tests (pd.DataFrame): from ``run_tests``.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    for sample in ("First public action", "First public action, liquid", "First of each type",
                   "First public action, OLS beta"):
        rows.append(sample)
        part = tests[tests["sample"] == sample]
        for group in ("ALL",) + EVENT_TYPES:
            cells, count = [], None
            for window in TABLE_WINDOWS:
                hit = part[(part.group == group) & (part.window == window)]
                if hit.empty or "mean" not in hit or pd.isna(hit["mean"].iloc[0]):
                    cells.append("--")
                    continue
                row = hit.iloc[0]
                count = row.n
                daggers = stars(row.p_rank).replace("*", "\\dagger")
                cells.append(f"{100 * row['mean']:.2f}{stars(row.p_bmp)}" + (f"$^{{{daggers}}}$" if daggers else ""))
            if count is not None:
                rows.append([TYPE_LABELS[group] if group != "ALL" else "All", n(count)] + cells)
    header = ["", "Events"] + [f"CAR{w}" for w in TABLE_WINDOWS]
    return tabular("r" * (len(header) - 1), header, rows)


def table_cross(cross: pd.DataFrame) -> str:
    """LaTeX: the cross-section of CARs.

    Args:
        cross (pd.DataFrame): from ``cross_section``.

    Returns:
        str: the table body.
    """
    labels = {"log_penalty": "Log(1 + sanction, ₹ lakh)".replace("₹", "Rs"), "debarred": "Debarment",
              "promoter_named": "Promoter or director named", "issuer_noticee": "Issuer a noticee",
              "interim": "Interim order", "settlement": "Settlement order", "liquidity": "Share of days traded",
              "cat_MARKET_MANIPULATION": "Market manipulation", "cat_DISCLOSURE_FAILURE": "Disclosure failure",
              "cat_INSIDER_TRADING": "Insider trading", "log_mcap": "Log market cap", "const": "Constant"}
    columns = [(m, w) for w in ("(-1,+1)", "(0,+10)") for m in ("(1) Sanction and firm", "(2) + log market cap")
               if not cross[(cross.model == m) & (cross.window == w)].empty]
    rows: list[list[str] | str] = []
    for term, label in labels.items():
        line = [label]
        for model, window in columns:
            hit = cross[(cross.model == model) & (cross.window == window) & (cross.term == term)]
            line.append("" if hit.empty else f"{100 * hit.coef.iloc[0]:.2f}{stars(hit.p.iloc[0])} "
                                             f"({100 * hit.se.iloc[0]:.2f})")
        rows.append(line)
    rows.append("Sample")
    rows.append(["Events"] + [n(cross[(cross.model == m) & (cross.window == w)].n.iloc[0]) for m, w in columns])
    rows.append(["$R^2$"] + [f"{cross[(cross.model == m) & (cross.window == w)].r2.iloc[0]:.3f}" for m, w in columns])
    header = [""] + [f"CAR{w} {m.split(' ')[0]}" for m, w in columns]
    return tabular("r" * len(columns), header, rows)


def table_suspension(bounds: pd.DataFrame) -> str:
    """LaTeX: firms suspended at the event, and the mean CAR(0,+10) under assumptions about their returns.

    Args:
        bounds (pd.DataFrame): from ``suspension_bounds``.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    for row in bounds[bounds.n_ok >= 5].itertuples(index=False):
        rows.append([TYPE_LABELS.get(row.group, "All"), n(row.n_ok), n(row.n_suspended),
                     f"{100 * row.mean_observed:.2f}", f"{100 * row.mean_if_zero:.2f}",
                     f"{100 * row.mean_if_p10:.2f}", f"{100 * row.mean_if_minus50:.2f}"])
    header = ["", "Traded", "Suspended", "Observed", "Suspended at 0", "At 10th pct.", "At $-50\\%$"]
    return tabular("rrrrrr", header, rows)


def table_gap(gap: pd.DataFrame) -> str:
    """LaTeX: market value lost (₹ crore) against monetary sanctions (₹ crore).

    Args:
        gap (pd.DataFrame): from ``reputational_gap``.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    for row in gap.itertuples(index=False):
        rows.append([TYPE_LABELS.get(row.group, "All"), n(row.n), f"{row.median_loss:.2f}",
                     f"{row.median_sanction:.2f}", pct(row.share_loss_exceeds, 1),
                     pct(row.share_issuer_sanctioned, 1), pct(row.share_loss_exceeds_issuer, 1)])
    header = ["", "Events", "Median loss", "Median sanction", "Loss $>$ all", "Firm fined", "Loss $>$ firm's"]
    return tabular("rrrrrr", header, rows)


def figures(events: pd.DataFrame, ars: pd.DataFrame, est_stats: pd.DataFrame, path: pathlib.Path) -> None:
    """Cumulative average abnormal returns around first public actions, by type (daily ARs clipped at 0.5/99.5%).

    Args:
        events (pd.DataFrame): typed events.
        ars (pd.DataFrame): the long AR table.
        est_stats (pd.DataFrame): estimation statistics.
        path (pathlib.Path): the PDF to write.

    Returns:
        None.
    """
    set_style()
    ok = set(est_stats[est_stats.status == "ok"].event_id)
    first = events[events.first_public & events.index.isin(ok)]
    window = ars[(ars.rel >= PLOT_RANGE[0]) & (ars.rel <= PLOT_RANGE[1]) & ars.event_id.isin(ok)].copy()
    low, high = window.ar.quantile([0.005, 0.995])
    window["ar"] = window.ar.clip(lower=low, upper=high)
    with PdfPages(path) as pdf:
        fig, ax = plt.subplots(figsize=(6.3, 3.0))
        palette = {"INTERIM": ACCENT, "FINAL": INK, "SETTLEMENT": NEUTRAL_DARK, "CONFIRMATION": NEUTRAL}
        for kind, colour in palette.items():
            ids = first[first.event_type == kind].index
            if len(ids) < 10:
                continue
            caar = window[window.event_id.isin(ids)].groupby("rel").ar.mean().cumsum()
            ax.plot(caar.index, 100 * caar.to_numpy(), color=colour, label=f"{TYPE_LABELS[kind]} ({len(ids)})")
        ax.axvline(0, color=NEUTRAL, linewidth=0.6)
        ax.axhline(0, color=NEUTRAL, linewidth=0.6)
        ax.set_xlabel("Trading days relative to the order (first public action)")
        ax.set_ylabel("Cumulative average abnormal return (%)")
        ax.legend(loc="lower left", fontsize=7)
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.stage3``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=None, help="scratch directory (everything goes there)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Stage 3 end to end.

    Args:
        argv (list[str] | None): command-line arguments (None = ``sys.argv``).

    Returns:
        int: exit status (0).
    """
    args = build_arg_parser().parse_args(argv)
    scratch = args.out is not None
    out_dir = args.out if scratch else args.derived / "stage3"
    tables_dir = out_dir if scratch else PAPER_DIR / "tables"
    figure_path = (out_dir if scratch else PAPER_DIR) / "figures_stage3.pdf"
    numbers_path = out_dir / "stage3_numbers.json" if scratch else args.derived / "stage3_numbers.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log_level, out_dir / "stage3.log" if scratch else LOG_FILE)
    warnings.filterwarnings("ignore", category=FutureWarning)

    events = build_events(args.derived)
    daily, market = firm_returns(set(events.firm_id), args.derived)
    est_stats, ars = abnormal_returns(events, daily, market)
    log.info("estimation         %s (all events); first public actions: %s",
             est_stats.status.value_counts().to_dict(),
             est_stats[est_stats.event_id.isin(events[events.first_public].index)].status.value_counts().to_dict())
    car_table = cars(ars, est_stats)
    tests = run_tests(events, car_table, ars, est_stats)
    ok_ids = set(est_stats[est_stats.status == "ok"].event_id)
    car_table = winsorise(car_table[car_table.event_id.isin(ok_ids)])
    bounds = suspension_bounds(events, car_table, est_stats)
    caps = market_caps(events, args.derived)
    cross = cross_section(events, car_table, est_stats, caps)
    gap = reputational_gap(events, car_table, est_stats, caps)

    events.assign(text_stems=events.text_stems.map(json.dumps)).to_parquet(out_dir / "events.parquet")
    est_stats.to_parquet(out_dir / "estimation.parquet", index=False)
    car_table.to_parquet(out_dir / "cars.parquet", index=False)
    for name, table in (("tests", tests), ("suspension_bounds", bounds), ("cross_section", cross), ("gap", gap)):
        table.to_csv(out_dir / f"{name}.csv", index=False)
    (tables_dir / "stage3_cars.tex").write_text(table_cars(tests))
    (tables_dir / "stage3_cross.tex").write_text(table_cross(cross))
    (tables_dir / "stage3_gap.tex").write_text(table_gap(gap))
    (tables_dir / "stage3_suspension.tex").write_text(table_suspension(bounds))
    figures(events, ars, est_stats, figure_path)

    primary = tests[tests["sample"] == "First public action"].drop(columns="sample").set_index(["group", "window"])
    numbers = {
        "events_typed": int(len(events)), "event_types": events.event_type.value_counts().to_dict(),
        "first_public_types": events[events.first_public].event_type.value_counts().to_dict(),
        "estimation_status_first": est_stats[est_stats.event_id.isin(events[events.first_public].index)]
        .status.value_counts().to_dict(),
        "car": {f"{g}|{w}": {k: (None if pd.isna(v) else float(v)) for k, v in primary.loc[(g, w)].items()}
                for g, w in primary.index if "mean" in primary.columns and pd.notna(primary.loc[(g, w), "mean"])},
        "gap": gap.to_dict(orient="records"), "suspension": bounds.to_dict(orient="records"),
    }
    numbers_path.write_text(json.dumps(numbers, indent=2, default=float))
    for row in tests[(tests["sample"] == "First public action")].itertuples(index=False):
        if pd.notna(getattr(row, "mean", np.nan)):
            log.info("CAR %-9s %-12s n=%4d mean %+.2f%% median %+.2f%% t %.2f BMP %.2f rank %.2f", row.window, row.group,
                     row.n, 100 * row.mean, 100 * row.median, row.t, row.bmp, row.rank)
    log.info("gap\n%s", gap.round(3).to_string(index=False))
    log.info("suspension\n%s", bounds.round(4).to_string(index=False))
    log.info("wrote %s, %s, %s and the stage3_*.tex tables", out_dir, figure_path, numbers_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
