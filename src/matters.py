"""Matter grouping (D1): one row per underlying case, the unit of analysis of Stage 1.

SEBI issues one order per noticee in mass matters, so the order table over-counts cases: the
Illiquid Stock Options (ISO) wave alone is ~2,400 orders sharing one window. This stage groups
orders into **matters** and gives each matter the quantities Stage 1 needs:

* the suspected violation window (earliest start to latest end of its orders' windows);
* the **first public action** (the earliest order, or SCN / interim / confirmatory date recited
  in any of its orders), so the detection lag is window end → first action;
* the latest order (the resolution lag runs first action → latest order);
* the matter's fraud category, detection channel, parties and issuer.

**How orders are grouped** (rules fixed before any lag was looked at, user decision D1,
2026-11-17):

1. Every ISO order is one matter (``ISO_RE``, broader than ``paper_tables``' flag: it also
   catches "illiquid stocks options", "ill-liquid", "ISO at BSE").
2. Otherwise an order carries *keys*: its matched listed firm (``issuer_firm_id``), its
   normalised issuer name (``issuer_guess``) and its normalised matter name (``matter_raw``,
   generic headings such as "ADJUDICATION PROCEEDINGS" dropped). Orders sharing any key are
   in one component (union–find), so a firm matched in one order and named differently in
   another still joins.
3. Within a component, windowed orders whose windows overlap or lie within
   ``TOLERANCE_DAYS`` of each other form one matter (interval merging). Two separate
   frauds at one firm with windows years apart stay separate matters.
4. A windowless order joins the matter of its component whose window started on or before the
   order and ended last before it (an interim or final order that does not restate the period).
   It contributes its order date, and recited prior actions dated on or after the matter's
   window start, to the first action. Windowless orders with no such matter form windowless
   matters, which carry no lag.
5. An order with no key at all is its own matter.

The matter's channel is the channel of the **earliest order stating one** (rule before model,
then higher confidence), else UNCLEAR (paper §6.1: "the earliest stated across its orders").
Its category is the plurality of its orders' categories, ties broken by the protocol's
precedence.

Reads ``orders``, ``order_issuers`` and ``order_entities``. Writes ``derived/matters.parquet``
and ``derived/order_matters.parquet``. No network; seconds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pathlib
import re
from collections import Counter

import numpy as np
import pandas as pd

from src import cache
from src.classify_fraud import PRECEDENCE
from src.extract_text import setup_logging
from src.names import norm

log = logging.getLogger("matters")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOG_FILE = REPO_ROOT / "logs" / "matters.log"

#: Windows of one component within this many days of each other are one matter.
TOLERANCE_DAYS = 90
YEAR_DAYS = 365.25
#: Every Illiquid Stock Options order, however the matter is spelt.
ISO_RE = re.compile(r"(?:illiquid|ill-\s?liquid)\s+(?:stocks?\s+)?options?\b"
                    r"|\bISO\s+(?:at|on)\s+(?:the\s+)?(?:BSE|Bombay)", re.I)
#: Matter headings that name no case.
GENERIC_MATTER = re.compile(
    r"^(?:ADJUDICATION(?: PROCEEDINGS?)?|ENQUIRY|INQUIRY|ENQUIRY PROCEEDINGS|ORDER|SHOW CAUSE NOTICE|"
    r"SETTLEMENT(?: APPLICATION)?|INVESTIGATION|INSPECTION|THE MATTER|VARIOUS SCRIPS|"
    r"VIOLATION OF .*|NON FILING .*|DELAYED FILING .*|UNREGISTERED INVESTMENT ADVISORY SERVICES|"
    r"INVESTMENT ADVISORY SERVICES|.*REGULATIONS|DIRECTIONS?)$")
#: Headings that name a kind of violation committed by many unrelated companies.
GENERIC_WORDS = re.compile(r"\b(?:REDRESSAL|GRIEVANCES?|SCORES|MINIMUM PUBLIC SHAREHOLDING|NON COMPLIANCE|"
                           r"FAILURE TO|NON OBTAINING|NON SUBMISSION|NON PAYMENT|LISTING FEES?|"
                           r"CORPORATE GOVERNANCE)\b")
#: The Illiquid Stock Options wave is BSE's; an NSE matter in illiquid options is a different case.
NSE_RE = re.compile(r"\bNSE\b|National Stock Exchange", re.I)
#: A matter heading often runs on into the order's first section.
MATTER_TAIL = re.compile(r"\s(?:FACTS OF THE CASE|BRIEF BACKGROUND|BACKGROUND|BRIEF FACTS|INTRODUCTION)\b.*$",
                         re.I)
MATTER_LEAD = re.compile(r"^(?:ITS\s+)?(?:ALLEGED\s+)?(?:DEALINGS?|TRADING(?: ACTIVITIES)?|INVESTIGATIONS?|"
                         r"MANIPULATION)\s+(?:IN|OF|BY)\s+(?:THE\s+)?(?:SCRIPS?|SHARES)?\s*(?:OF\s+)?", re.I)
CHANNEL_METHOD_RANK = {"rule": 0, "model": 1, "none": 2}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def is_iso(title: pd.Series, matter: pd.Series, raw: pd.Series) -> pd.Series:
    """Flag Illiquid Stock Options orders (BSE's wave) from the title, matter heading or window sentence.

    Args:
        title (pd.Series): SEBI's titles (str or null).
        matter (pd.Series): ``matter_raw`` (str or null).
        raw (pd.Series): ``violation_raw`` (str or null).

    Returns:
        pd.Series: bool per row.
    """
    flag = pd.Series(False, index=title.index)
    for column in (title, matter, raw):
        flag |= column.fillna("").astype(str).str.contains(ISO_RE)
    nse = (title.fillna("").astype(str).str.contains(NSE_RE)
           | matter.fillna("").astype(str).str.contains(NSE_RE))
    return flag & ~nse


def name_key(name: object) -> str | None:
    """A normalised issuer or matter name usable as a grouping key.

    Args:
        name (object): an issuer guess or matter heading (str, or null).

    Returns:
        str | None: ``"N:<normalised>"``, or None when the name is missing, generic or too short.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    text = re.sub(r"[’'`]", "", name)
    text = MATTER_TAIL.sub("", text)
    text = MATTER_LEAD.sub("", text.strip())
    normalised = norm(text)
    if len(normalised) < 4 or GENERIC_MATTER.match(normalised) or GENERIC_WORDS.search(normalised):
        return None
    return "N:" + normalised


class UnionFind:
    """Union–find over hashable items, for linking orders that share a key.

    Attributes:
        parent (dict[str, str]): each item's parent; a root is its own parent.
    """

    def __init__(self) -> None:
        """Start empty.

        Returns:
            None.
        """
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        """The root of ``item``'s set (adding it if new), with path halving.

        Args:
            item (str): any key.

        Returns:
            str: the set's root.
        """
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: str, second: str) -> None:
        """Merge the sets of two items.

        Args:
            first (str): a key.
            second (str): another key.

        Returns:
            None.
        """
        root_a, root_b = self.find(first), self.find(second)
        if root_a != root_b:
            self.parent[max(root_a, root_b)] = min(root_a, root_b)


def order_keys(frame: pd.DataFrame) -> pd.Series:
    """The grouping keys of each order (rule 2 of the module docstring).

    Args:
        frame (pd.DataFrame): orders with ``text_stem``, ``iso``, ``issuer_firm_id``,
            ``issuer_guess`` and ``matter_raw``.

    Returns:
        pd.Series: list[str] per row: ``["ISO"]`` for ISO orders, else the firm key
            (``"F:<firm_id>"``) and name keys (``"N:<name>"``) present; empty when none.
    """
    keys = []
    for row in frame.itertuples(index=False):
        if row.iso:
            keys.append(["ISO"])
            continue
        found = []
        if isinstance(row.issuer_firm_id, str) and row.issuer_firm_id:
            found.append("F:" + row.issuer_firm_id)
        for name in (row.issuer_guess, row.matter_raw):
            key = name_key(name)
            if key and key not in found:
                found.append(key)
        keys.append(found)
    return pd.Series(keys, index=frame.index)


def drop_shared_names(keys: pd.Series, max_firms: int = 1) -> tuple[pd.Series, set[str]]:
    """Drop name keys that orders tie to more than one listed firm: they name a kind of case, not a case.

    "Non-compliance with minimum public shareholding by listed companies" heads orders against 33
    firms; linking through it would chain them into one matter.

    Args:
        keys (pd.Series): list[str] per order, from ``order_keys``.
        max_firms (int): a name tied to more distinct firms than this is dropped.

    Returns:
        tuple[pd.Series, set[str]]: the key lists without the shared names, and the names dropped.
    """
    firms_of: dict[str, set[str]] = {}
    for found in keys:
        firms = {key for key in found if key.startswith("F:")}
        for key in found:
            if key.startswith("N:"):
                firms_of.setdefault(key, set()).update(firms)
    shared = {name for name, firms in firms_of.items() if len(firms) > max_firms}
    return keys.map(lambda found: [key for key in found if key not in shared]), shared


def components(frame: pd.DataFrame, keys: pd.Series) -> pd.Series:
    """Connected components of orders that share a key (rule 2).

    Args:
        frame (pd.DataFrame): orders with ``text_stem``.
        keys (pd.Series): list[str] per row, from ``order_keys``.

    Returns:
        pd.Series: str component id per row (a key root, or ``"S:<text_stem>"`` for an order
            with no key).
    """
    forest = UnionFind()
    for found in keys:
        for key in found[1:]:
            forest.union(found[0], key)
    out = []
    for stem, found in zip(frame.text_stem, keys):
        out.append(forest.find(found[0]) if found else "S:" + str(stem))
    return pd.Series(out, index=frame.index)


def merge_windows(starts: np.ndarray, ends: np.ndarray, tolerance_days: int = TOLERANCE_DAYS) -> np.ndarray:
    """Interval merging: windows that overlap or lie within the tolerance share a group (rule 3).

    Args:
        starts (np.ndarray): datetime64 window starts.
        ends (np.ndarray): datetime64 window ends, same length.
        tolerance_days (int): largest gap between one group's latest end and the next start.

    Returns:
        np.ndarray: int group number per window (0, 1, …, in order of start).
    """
    order = np.argsort(starts, kind="stable")
    groups = np.empty(len(starts), dtype=int)
    tolerance = np.timedelta64(tolerance_days, "D")
    group, reach = -1, None
    for index in order:
        if reach is None or starts[index] > reach + tolerance:
            group += 1
            reach = ends[index]
        else:
            reach = max(reach, ends[index])
        groups[index] = group
    return groups


def assign_matters(frame: pd.DataFrame, tolerance_days: int = TOLERANCE_DAYS) -> pd.DataFrame:
    """Give every order a matter (rules 1–5).

    Args:
        frame (pd.DataFrame): orders with ``text_stem``, ``iso``, ``issuer_firm_id``,
            ``issuer_guess``, ``matter_raw``, ``order_date``, ``violation_start``,
            ``violation_end`` (datetimes; NaT when no window).
        tolerance_days (int): see ``merge_windows``.

    Returns:
        pd.DataFrame: ``text_stem``, ``component``, ``matter_key`` (component plus window group),
            ``link`` ∈ ``windowed`` (its window defines the matter), ``attached`` (windowless,
            joined to a windowed matter), ``windowless`` (no windowed matter to join).
    """
    work = frame[["text_stem", "order_date", "violation_start", "violation_end"]].copy()
    keys, shared = drop_shared_names(order_keys(frame))
    if shared:
        log.info("generic names      %d dropped as links (tied to several firms): %s", len(shared),
                 sorted(shared)[:6])
    work["component"] = components(frame, keys).to_numpy()
    work["has_window"] = work.violation_start.notna() & work.violation_end.notna()
    work["matter_key"] = None
    work["link"] = None
    for component, rows in work.groupby("component", sort=False):
        windowed = rows[rows.has_window]
        spans: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
        if len(windowed):
            groups = merge_windows(windowed.violation_start.to_numpy(), windowed.violation_end.to_numpy(),
                                   tolerance_days)
            for group in np.unique(groups):
                members = windowed[groups == group]
                key = f"{component}#{group}"
                work.loc[members.index, "matter_key"] = key
                work.loc[members.index, "link"] = "windowed"
                spans.append((key, members.violation_start.min(), members.violation_end.max()))
        for index, row in rows[~rows.has_window].iterrows():
            started = [span for span in spans if pd.notna(row.order_date) and span[1] <= row.order_date]
            if started:
                ended_before = [span for span in started if span[2] <= row.order_date]
                pick = max(ended_before or started, key=lambda span: span[2])
                work.at[index, "matter_key"] = pick[0]
                work.at[index, "link"] = "attached"
            else:
                work.at[index, "matter_key"] = f"{component}#none"
                work.at[index, "link"] = "windowless"
    return work[["text_stem", "component", "matter_key", "link"]]


def matter_id(stems: list[str]) -> str:
    """A stable id from a matter's member orders.

    Args:
        stems (list[str]): the member orders' text stems.

    Returns:
        str: ``"M"`` + 10 hex characters of the SHA-1 of the sorted stems.
    """
    digest = hashlib.sha1("|".join(sorted(map(str, stems))).encode()).hexdigest()
    return "M" + digest[:10]


def matter_category(categories: pd.Series) -> str | None:
    """The plurality category of a matter's orders, ties broken by protocol precedence.

    Args:
        categories (pd.Series): the orders' ``fraud_category`` (str or null).

    Returns:
        str | None: a category, or None when no order has one.
    """
    counts = Counter(categories.dropna())
    if not counts:
        return None
    best = max(counts.values())
    return min((c for c, k in counts.items() if k == best), key=PRECEDENCE.index)


def matter_channel(rows: pd.DataFrame) -> tuple[str | None, str | None, str | None]:
    """The channel of the earliest order stating one; UNCLEAR when none does.

    Args:
        rows (pd.DataFrame): a matter's orders with ``order_date``, ``detection_channel``,
            ``detection_detail``, ``detection_method``, ``detection_confidence``, ``text_stem``.

    Returns:
        tuple[str | None, str | None, str | None]: channel, detail and the deciding order's
            text stem (channel None when no order has text).
    """
    has = rows[rows.detection_channel.notna()]
    if has.empty:
        return None, None, None
    stated = has[has.detection_channel != "UNCLEAR"]
    if stated.empty:
        first = has.sort_values("order_date").iloc[0]
        return "UNCLEAR", first.detection_detail, first.text_stem
    ranked = stated.assign(
        _m=stated.detection_method.map(CHANNEL_METHOD_RANK).fillna(3),
        _c=stated.detection_confidence.map(CONFIDENCE_RANK).fillna(3))
    first = ranked.sort_values(["order_date", "_m", "_c", "text_stem"]).iloc[0]
    return first.detection_channel, first.detection_detail, first.text_stem


def first_action(rows: pd.DataFrame, window_start: pd.Timestamp | None) -> tuple[pd.Timestamp | None, str | None]:
    """The matter's first public action: the earliest order or recited prior action.

    Windowed members contribute their ``first_action_date``; attached windowless orders their
    order date and any recited prior action on or after the matter's window start (rule 4).

    Args:
        rows (pd.DataFrame): a matter's orders with ``link``, ``order_date``, ``order_type``,
            ``first_action_date``, ``first_action_source``, ``prior_action_date``,
            ``prior_action_kind``.
        window_start (pd.Timestamp | None): the matter's window start (None for windowless).

    Returns:
        tuple[pd.Timestamp | None, str | None]: the date, and its kind (``scn``, ``interim``,
            ``confirmatory`` for a recited action; ``order:<type>`` for an order).
    """
    candidates: list[tuple[pd.Timestamp, str]] = []
    for row in rows.itertuples(index=False):
        if pd.notna(row.order_date):
            candidates.append((row.order_date, f"order:{row.order_type}"))
        prior = row.prior_action_date
        if pd.notna(prior) and (row.link == "windowed" or window_start is None or prior >= window_start):
            candidates.append((prior, str(row.prior_action_kind)))
    if not candidates:
        return None, None
    return min(candidates, key=lambda item: (item[0], item[1]))


def party_counts(entities: pd.DataFrame) -> pd.DataFrame:
    """Per-order party names and whether an intermediary is a noticee.

    Args:
        entities (pd.DataFrame): ``order_entities`` with ``text_stem``, ``entity_name_clean``,
            ``entity_role``, ``sebi_reg_no``.

    Returns:
        pd.DataFrame: one row per order: ``text_stem``, ``party_names`` (set[str]),
            ``intermediary`` (bool).
    """
    work = entities.assign(
        _inter=(entities.entity_role == "INTERMEDIARY") | entities.sebi_reg_no.notna())
    grouped = work.groupby("text_stem").agg(
        party_names=("entity_name_clean", lambda s: set(s.dropna())),
        intermediary=("_inter", "any"))
    return grouped.reset_index()


def years(later: pd.Series, earlier: pd.Series) -> pd.Series:
    """Years between two date columns.

    Args:
        later (pd.Series): datetimes.
        earlier (pd.Series): datetimes.

    Returns:
        pd.Series: float years (NaN where either is missing).
    """
    return (later - earlier).dt.days / YEAR_DAYS


def build_matters(orders: pd.DataFrame, entities: pd.DataFrame,
                  tolerance_days: int = TOLERANCE_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Group orders into matters and describe each matter.

    Args:
        orders (pd.DataFrame): ``orders.parquet`` joined to ``order_issuers`` (``issuer_firm_id``),
            dates as datetimes.
        entities (pd.DataFrame): ``order_entities`` (see ``party_counts``).
        tolerance_days (int): see ``merge_windows``.

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: ``matters`` (one row per matter) and
            ``order_matters`` (one row per order: ``text_stem``, ``matter_id``, ``link``,
            ``component``).
    """
    frame = orders.copy()
    frame["iso"] = is_iso(frame.title, frame.matter_raw, frame.violation_raw)
    assigned = assign_matters(frame, tolerance_days)
    frame = frame.merge(assigned, on="text_stem", how="left", validate="one_to_one")
    parties = party_counts(entities)
    frame = frame.merge(parties, on="text_stem", how="left", validate="one_to_one")
    frame["intermediary"] = frame.intermediary.astype("boolean").fillna(False).astype(bool)

    ids = frame.groupby("matter_key").text_stem.apply(lambda s: matter_id(list(s)))
    frame["matter_id"] = frame.matter_key.map(ids)

    records = []
    for key, rows in frame.groupby("matter_key", sort=False):
        windowed = rows[rows.link == "windowed"]
        start = windowed.violation_start.min() if len(windowed) else pd.NaT
        end = windowed.violation_end.max() if len(windowed) else pd.NaT
        action_date, action_kind = first_action(rows, None if pd.isna(start) else start)
        channel, detail, channel_stem = matter_channel(rows)
        names: set[str] = set()
        for found in rows.party_names.dropna():
            names |= found
        firms = rows.issuer_firm_id.dropna()
        types = Counter(rows.order_type)
        methods = set(windowed.violation_method.dropna())
        kinds = set(windowed.window_kind.dropna())
        stated_channels = sorted(set(rows.detection_channel.dropna()) - {"UNCLEAR"})
        records.append({
            "matter_id": rows.matter_id.iat[0],
            "component": rows.component.iat[0],
            "key_kind": ("iso" if rows.component.iat[0] == "ISO" else
                         "firm" if rows.component.iat[0].startswith("F:") else
                         "name" if rows.component.iat[0].startswith("N:") else "single"),
            "matter_name": rows.matter_raw.dropna().iat[0] if rows.matter_raw.notna().any() else None,
            "issuer_firm_id": firms.mode().iat[0] if len(firms) else None,
            "iso": bool(rows.iso.any()),
            "n_orders": len(rows),
            "n_windowed": len(windowed),
            "n_attached": int((rows.link == "attached").sum()),
            "n_wtm": types.get("WTM", 0), "n_ao": types.get("AO", 0),
            "n_settlement": types.get("SETTLEMENT", 0), "n_ed_cgm": types.get("ED_CGM", 0),
            "window_start": start,
            "window_end": end,
            "window_end_min": windowed.violation_end.min() if len(windowed) else pd.NaT,
            "window_methods": ",".join(sorted(methods)) or None,
            "window_kinds": ",".join(sorted(kinds)) or None,
            "first_action_date": action_date,
            "first_action_kind": action_kind,
            "first_order_date": rows.order_date.min(),
            "last_order_date": rows.order_date.max(),
            "fraud_category": matter_category(rows.fraud_category),
            "detection_channel": channel,
            "detection_detail": detail,
            "channel_stem": channel_stem,
            "channels_stated": json.dumps(stated_channels),
            "n_parties": len(names) if names else int(rows.n_parties.fillna(0).max()),
            "intermediary_noticee": bool(rows.intermediary.any()),
            "penalty_total": float(rows.penalty_total.fillna(0).sum()),
            "settlement_total": float(rows.settlement_amount.fillna(0).sum()),
            "debarred": bool(rows.debarred.fillna(False).astype(bool).any()),
            "text_stems": json.dumps(sorted(map(str, rows.text_stem))),
        })
    matters = pd.DataFrame.from_records(records)
    for column in ("window_start", "window_end", "window_end_min", "first_action_date",
                   "first_order_date", "last_order_date"):
        matters[column] = pd.to_datetime(matters[column])
    matters["has_window"] = matters.window_end.notna()
    matters["t_detect"] = years(matters.first_action_date, matters.window_end)
    matters["t_resolve"] = years(matters.last_order_date, matters.first_action_date)
    matters["window_years"] = years(matters.window_end, matters.window_start)
    matters = matters.sort_values(["window_end", "matter_id"], na_position="last").reset_index(drop=True)
    order_matters = frame[["text_stem", "matter_id", "link", "component"]].copy()
    assert order_matters.text_stem.is_unique and len(order_matters) == len(orders)
    assert matters.matter_id.is_unique
    return matters, order_matters


def load_inputs(derived: pathlib.Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the order table (with the issuer match) and the parties.

    Args:
        derived (pathlib.Path): directory holding ``orders``, ``order_issuers``, ``order_entities``.

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: orders (dates parsed, ``issuer_firm_id`` joined) and
            entities.
    """
    orders = pd.read_parquet(derived / "orders.parquet")
    issuers = pd.read_parquet(derived / "order_issuers.parquet", columns=["text_stem", "issuer_firm_id"])
    frame = orders.merge(issuers, on="text_stem", how="left", validate="one_to_one")
    assert len(frame) == len(orders), "the issuer join changed the row count"
    for column in ("order_date", "violation_start", "violation_end", "first_action_date", "prior_action_date"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("penalty_total", "settlement_amount", "n_parties"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    entities = pd.read_parquet(derived / "order_entities.parquet",
                               columns=["text_stem", "entity_name_clean", "entity_role", "sebi_reg_no"])
    return frame, entities


def summarise(matters: pd.DataFrame, order_matters: pd.DataFrame) -> None:
    """Log the counts a reader needs to judge the grouping.

    Args:
        matters (pd.DataFrame): from ``build_matters``.
        order_matters (pd.DataFrame): from ``build_matters``.

    Returns:
        None.
    """
    windowed = matters[matters.has_window]
    log.info("orders             %d: %s", len(order_matters), order_matters.link.value_counts().to_dict())
    log.info("matters            %d; with a window %d; windowless %d", len(matters), len(windowed),
             (~matters.has_window).sum())
    log.info("windowed matters   by key %s; more than one order %d; ISO orders %d in %d matter(s)",
             windowed.key_kind.value_counts().to_dict(), (windowed.n_orders > 1).sum(),
             int(matters.loc[matters.iso, "n_orders"].sum()), int(matters.iso.sum()))
    log.info("largest matters    %s", windowed.nlargest(8, "n_orders")[["matter_name", "n_orders"]]
             .assign(matter_name=lambda d: d.matter_name.str[:40]).values.tolist())
    log.info("first action       kinds %s", windowed.first_action_kind.value_counts().head(8).to_dict())
    log.info("detection lag      median %.2f y (IQR %.2f–%.2f); non-positive %d (%.1f%%)",
             windowed.t_detect.median(), windowed.t_detect.quantile(.25), windowed.t_detect.quantile(.75),
             (windowed.t_detect <= 0).sum(), 100 * (windowed.t_detect <= 0).mean())
    log.info("resolution lag     median %.2f y", windowed.t_resolve.median())
    log.info("channels           %s", windowed.detection_channel.value_counts().to_dict())


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.matters``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR, help="where the inputs are")
    parser.add_argument("--out", type=pathlib.Path, default=None, help="where to write (default: --derived)")
    parser.add_argument("--tolerance-days", type=int, default=TOLERANCE_DAYS)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Build ``matters.parquet`` and ``order_matters.parquet``.

    Args:
        argv (list[str] | None): command-line arguments (None = ``sys.argv``).

    Returns:
        int: exit status (0).
    """
    args = build_arg_parser().parse_args(argv)
    out = args.out or args.derived
    setup_logging(args.log_level, LOG_FILE if out == DERIVED_DIR else out / "matters.log")
    orders, entities = load_inputs(args.derived)
    matters, order_matters = build_matters(orders, entities, args.tolerance_days)
    out.mkdir(parents=True, exist_ok=True)
    matters.to_parquet(out / "matters.parquet", index=False)
    order_matters.to_parquet(out / "order_matters.parquet", index=False)
    summarise(matters, order_matters)
    log.info("wrote %s and %s (tolerance %d days)", out / "matters.parquet", out / "order_matters.parquet",
             args.tolerance_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
