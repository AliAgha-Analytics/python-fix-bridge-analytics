"""
Reusable loaders and analytics. Lessons 01-02 build these steps by hand first; later lessons
import them so each notebook can focus on its own topic.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from . import fix

DATA = "data"
LPS = ("LP_A", "LP_B", "LP_C")


def _path(*p):
    """Resolve a data path whether the notebook runs from the repo root or from lessons/."""
    for root in (DATA, os.path.join("..", DATA)):
        cand = os.path.join(root, *p)
        if os.path.exists(cand) or glob.glob(cand):
            return cand
    return os.path.join(DATA, *p)


# ----------------------------------------------------------------------------- FIX logs
def load_fix_messages(lps=LPS) -> pd.DataFrame:
    """Every FIX message of every LP session as one row (header + the most useful body tags)."""
    rows = []
    keep = {11: "cl_ord_id", 17: "exec_id", 37: "order_id", 150: "exec_type", 39: "ord_status", 55: "symbol",
            54: "side", 38: "order_qty", 44: "price", 31: "last_px", 32: "last_qty", 14: "cum_qty", 151: "leaves_qty",
            58: "text", 60: "transact_time", 43: "poss_dup", 122: "orig_sending_time", 7: "begin_seq", 16: "end_seq",
            36: "new_seq", 123: "gap_fill", 141: "reset_seq", 112: "test_req_id", 108: "heartbeat_int"}
    for lp in lps:
        for line_no, (log_time, direction, raw) in enumerate(fix.read_log(_path("fix_logs", f"BRIDGE_{lp}.log"))):
            m = fix.parse(raw)
            r = dict(lp=lp, line=line_no, log_time=log_time, direction=direction, msg_type=m[35],
                     msg_name=fix.MSG_TYPES.get(m[35], "?"), seq=int(m[34]), sending_time=m[52], raw=raw)
            for tag, name in keep.items():
                if tag in m:
                    r[name] = m[tag]
            if m[35] == "W":
                g = m["groups"]
                bids = [e for e in g if e[269] == "0"]
                asks = [e for e in g if e[269] == "1"]
                r.update(bid1=float(bids[0][270]), bsize1=float(bids[0][271]), ask1=float(asks[0][270]),
                         asize1=float(asks[0][271]), bid2=float(bids[1][270]), ask2=float(asks[1][270]))
            rows.append(r)
    df = pd.DataFrame(rows)
    df["log_time"] = pd.to_datetime(df.log_time, format="%Y%m%d-%H:%M:%S.%f")
    df["sending_time"] = pd.to_datetime(df.sending_time, format="%Y%m%d-%H:%M:%S.%f")
    for c in ("order_qty", "price", "last_px", "last_qty", "cum_qty", "leaves_qty"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # stable order: by time, then by position in the log file (several messages can share a millisecond)
    return df.sort_values(["log_time", "lp", "line"], kind="stable").reset_index(drop=True)


def lp_quotes(msgs: pd.DataFrame) -> pd.DataFrame:
    """Market-data snapshots (35=W) as a quote table: one row per LP update."""
    q = msgs[msgs.msg_type == "W"][["lp", "symbol", "sending_time", "log_time", "seq", "bid1", "ask1",
                                    "bsize1", "asize1", "bid2", "ask2"]].copy()
    q = q.rename(columns={"log_time": "recv_time"})
    return q.sort_values("recv_time").reset_index(drop=True)


def aggregated_book(quotes: pd.DataFrame, symbol: str, drop_lp_windows: dict | None = None) -> pd.DataFrame:
    """Best bid / best ask across LPs at every update, as the bridge saw it (last known quote per LP).
    drop_lp_windows: {lp: [(start, end), ...]} periods when an LP's quotes are not usable."""
    q = quotes[quotes.symbol == symbol].sort_values("recv_time")
    wide = {}
    for lp, g in q.groupby("lp"):
        s = g.set_index("recv_time")[["bid1", "ask1"]]
        s = s[~s.index.duplicated(keep="last")]
        wide[lp] = s
    idx = q.recv_time.drop_duplicates().sort_values()
    out = pd.DataFrame(index=idx)
    for lp, s in wide.items():
        aligned = s.reindex(idx, method="ffill")
        if drop_lp_windows and lp in drop_lp_windows:
            for a, b in drop_lp_windows[lp]:
                aligned.loc[(aligned.index >= a) & (aligned.index < b)] = np.nan
        out[f"{lp}_bid"] = aligned.bid1
        out[f"{lp}_ask"] = aligned.ask1
    bids = out[[c for c in out if c.endswith("_bid")]]
    asks = out[[c for c in out if c.endswith("_ask")]]
    out["best_bid"] = bids.max(axis=1)
    out["best_ask"] = asks.min(axis=1)
    out["bid_lp"] = bids.idxmax(axis=1).str.replace("_bid", "")
    out["ask_lp"] = asks.idxmin(axis=1).str.replace("_ask", "")
    out["mid"] = (out.best_bid + out.best_ask) / 2
    out.index.name = "time"
    return out


# ----------------------------------------------------------------------------- platform data
def load_platform():
    orders = pd.read_csv(_path("platform", "orders.csv"))
    for c in [c for c in orders if c.endswith("_time")]:
        orders[c] = pd.to_datetime(orders[c])
    deals = pd.read_csv(_path("platform", "deals.csv"), parse_dates=["time"])
    return orders, deals


def load_client_quotes():
    return pd.read_csv(_path("platform", "client_quotes.csv"), parse_dates=["time"])


def load_lp_statements():
    frames = []
    for lp in LPS:
        p = _path("lp_statements", f"{lp}_trades.csv")
        if os.path.exists(p):
            d = pd.read_csv(p, parse_dates=["trade_time"])
            d.insert(0, "lp", lp)
            frames.append(d)
    return pd.concat(frames, ignore_index=True)


def load_reference():
    import json
    sym = pd.read_csv(_path("reference", "symbols.csv")).set_index("symbol")
    with open(_path("reference", "bridge_config.json")) as f:
        cfg = json.load(f)
    audit = pd.read_csv(_path("reference", "bridge_audit_log.csv"))
    clients = pd.read_csv(_path("reference", "clients.csv"))
    return sym, cfg, audit, clients


def mid_series(quotes: pd.DataFrame, symbol: str) -> pd.Series:
    """Robust reference mid: median of the LPs' latest mids (ignores one bad LP)."""
    q = quotes[quotes.symbol == symbol].sort_values("recv_time")
    q = q.assign(mid=(q.bid1 + q.ask1) / 2)
    wide = q.pivot_table(index="recv_time", columns="lp", values="mid", aggfunc="last").ffill()
    return wide.median(axis=1).rename("ref_mid")


def value_at(series: pd.Series, times) -> np.ndarray:
    """Last value of a time-indexed series at each requested time (as-of lookup)."""
    s = series.sort_index()
    pos = np.searchsorted(s.index.values, pd.to_datetime(pd.Series(times)).values, side="right") - 1
    vals = s.values[np.clip(pos, 0, None)]
    return np.where(pos >= 0, vals, np.nan)
