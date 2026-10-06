"""
Rule-based monitoring for a liquidity bridge: every check from lessons 02-07 as a function that returns alerts.
Each alert = time, severity, rule, lp, symbol, detail. Windowed (statistical) rules are time-stamped at the END of
their window, i.e. when the alert could really have fired. `run_all()` runs every rule and `group_incidents()` merges
consecutive alerts of the same rule/LP/symbol into incidents.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import analysis as A

SEV = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "INFO": 0}


def _alert(t, sev, rule, lp, symbol, detail):
    return dict(time=pd.Timestamp(t), severity=sev, rule=rule, lp=lp, symbol=symbol, detail=detail)


# ----------------------------------------------------------------------------- session / feed
def session_events(msgs):
    out = []
    for r in msgs[msgs.msg_type.isin(["1", "2", "5"]) | ((msgs.msg_type == "A") & (msgs.log_time > msgs.log_time.min() + pd.Timedelta(minutes=1)))].itertuples():
        sev = {"1": "HIGH", "2": "HIGH", "5": "CRITICAL", "A": "HIGH"}[r.msg_type]
        out.append(_alert(r.log_time, sev, f"session: {r.msg_name}", r.lp, None,
                          f"{r.direction} seq={r.seq}" + (f" text='{r.text}'" if isinstance(r.text, str) else "")
                          + (" ResetSeqNumFlag=Y" if r.reset_seq == "Y" else "")))
    for r in msgs[(msgs.poss_dup == "Y") & (msgs.msg_type == "8")].itertuples():
        out.append(_alert(r.log_time, "CRITICAL", "session: PossDup execution received", r.lp, r.symbol,
                          f"ExecID {r.exec_id} re-sent (43=Y): verify it is not booked twice"))
    return out


def lp_silence(msgs, max_gap_s=2.0):
    out = []
    inc = msgs[msgs.direction == "IN"].sort_values(["log_time", "line"])
    for lp, g in inc.groupby("lp"):
        gap = g.log_time.diff().dt.total_seconds()
        for t, gs in zip(g.log_time[gap > max_gap_s * 3], gap[gap > max_gap_s * 3]):
            out.append(_alert(t - pd.Timedelta(seconds=gs) + pd.Timedelta(seconds=max_gap_s), "CRITICAL",
                              "feed: LP silent", lp, None, f"no message for {gs:.1f} s"))
    return out


def symbol_freeze(quotes, threshold_s=6.0):
    out = []
    for (lp, s), g in quotes.groupby(["lp", "symbol"]):
        gap = g.recv_time.diff().dt.total_seconds()
        for t, gs in zip(g.recv_time[gap > threshold_s * 3], gap[gap > threshold_s * 3]):
            start = t - pd.Timedelta(seconds=gs)
            other = quotes[(quotes.lp == lp) & (quotes.symbol != s) & quotes.recv_time.between(start, t)]
            if len(other) > 0.5 * gs:                                  # session alive, only this symbol frozen
                out.append(_alert(start + pd.Timedelta(seconds=threshold_s), "CRITICAL", "feed: symbol frozen", lp, s,
                                  f"no {s} update for {gs:.0f} s while {len(other)} other updates arrived"))
    return out


def spikes(quotes, pip, limits):
    out = []
    for s in quotes.symbol.unique():
        q = quotes[quotes.symbol == s]
        mids = q.assign(mid=(q.bid1 + q.ask1) / 2).pivot_table(index="recv_time", columns="lp", values="mid", aggfunc="last").ffill()
        cons = mids.median(axis=1)
        for lp in mids:
            own = q[q.lp == lp].drop_duplicates("recv_time", keep="last").set_index("recv_time")
            dev = ((own.bid1 + own.ask1) / 2 - cons.reindex(own.index)) / pip[s]
            for t, d in dev[dev.abs() > limits[s]].items():
                out.append(_alert(t, "CRITICAL", "price: spike vs other LPs", lp, s, f"{d:+.1f} pips from consensus"))
    return out


def session_down_windows(msgs):
    """{lp: [(logout_time, next_logon_time)]}: periods when the bridge had dropped an LP session."""
    out = {}
    for lp, g in msgs[msgs.msg_type.isin(["5", "A"]) & (msgs.direction == "OUT")].sort_values("log_time").groupby("lp"):
        down = None
        for r in g.itertuples():
            if r.msg_type == "5":
                down = r.log_time
            elif down is not None:
                out.setdefault(lp, []).append((down, r.log_time + pd.Timedelta(seconds=2)))
                down = None
    return out


def crossed_book(quotes, pip, min_duration_s=1.0, min_depth_pips=None, down=None):
    """Crossed aggregated book lasting at least min_duration_s and deeper than min_depth_pips (ignores tiny,
    momentary crosses caused by LPs repricing a few hundred ms apart)."""
    min_depth_pips = min_depth_pips or {"EURUSD": 2, "GBPUSD": 2, "XAUUSD": 10}
    out = []
    for s in quotes.symbol.unique():
        b = A.aggregated_book(quotes, s, drop_lp_windows=down)
        x = b.best_bid > b.best_ask
        grp = (x != x.shift()).cumsum()[x]
        for _, idx in grp.groupby(grp).groups.items():
            dur = (idx[-1] - idx[0]).total_seconds()
            worst = ((b.loc[idx, "best_ask"] - b.loc[idx, "best_bid"]) / pip[s]).min()
            if dur >= min_duration_s and worst <= -min_depth_pips[s]:
                out.append(_alert(idx[0], "HIGH", "price: crossed book", None, s,
                                  f"bid > ask for {dur:.1f} s, worst {worst:.1f} pips"))
    return out


def spread_regime(quotes, pip, factor=3.0):
    out = []
    q = quotes.assign(sp=(quotes.ask1 - quotes.bid1) / quotes.symbol.map(pip))
    for s, g in q.groupby("symbol"):
        base = g.sp.median()
        m = g.set_index("recv_time").sp.resample("30s").median()
        for t, v in m[m > factor * base].items():
            out.append(_alert(t + pd.Timedelta("30s"), "INFO", "liquidity: spreads widened", None, s, f"median spread {v:.1f} pips = {v / base:.1f}x normal"))
    return out


def clock_offset(msgs, max_ms=10):
    out = []
    lg = msgs[msgs.msg_type == "A"].sort_values(["log_time", "line"]).groupby(["lp", "direction"]).first()
    for lp in msgs.lp.unique():
        o, i = lg.loc[(lp, "OUT")], lg.loc[(lp, "IN")]
        one_way = (i.log_time - o.log_time).total_seconds() * 500
        off = (i.sending_time - o.log_time).total_seconds() * 1000 - one_way
        if abs(off) > max_ms:
            out.append(_alert(i.log_time, "MEDIUM", "session: clock offset", lp, None, f"LP clock {off:+.0f} ms vs ours"))
    return out


# ----------------------------------------------------------------------------- orders / execution
def lp_orders(msgs):
    d = msgs[msgs.msg_type == "D"][["lp", "cl_ord_id", "log_time", "symbol", "order_qty"]].rename(columns={"log_time": "sent"})
    er = msgs[(msgs.msg_type == "8") & (msgs.poss_dup != "Y")]
    ans = er.groupby("cl_ord_id").agg(answered=("log_time", "min"),
                                      rejected=("exec_type", lambda s: "8" in set(s))).reset_index()
    life = d.merge(ans, on="cl_ord_id", how="left")
    life["rtt_ms"] = (life.answered - life.sent).dt.total_seconds() * 1000
    return life


def unanswered(life, timeout_ms=1000):
    return [_alert(r.sent + pd.Timedelta(milliseconds=timeout_ms), "CRITICAL", "orders: no LP response", r.lp, r.symbol,
                   f"{r.cl_ord_id} unanswered: execution status UNKNOWN") for r in life[life.answered.isna()].itertuples()]


def reject_rate(life, window="10min", limit=0.25, min_orders=10):
    out = []
    for lp, g in life.dropna(subset=["answered"]).groupby("lp"):
        r = g.set_index("sent").rejected.astype(float).resample(window)
        rate, n = r.mean(), r.count()
        for t in rate.index[(rate > limit) & (n >= min_orders)]:
            out.append(_alert(t + pd.Timedelta(window), "MEDIUM", "LP: high reject rate", lp, None, f"{rate[t]:.0%} of {int(n[t])} orders rejected"))
    return out


def lp_latency(life, window="2min", factor=5.0):
    out = []
    for lp, g in life.dropna(subset=["rtt_ms"]).groupby("lp"):
        base = g.rtt_ms.median()
        m = g.set_index("sent").rtt_ms.resample(window).median()
        for t, v in m[m > factor * base].items():
            out.append(_alert(t + pd.Timedelta(window), "HIGH", "LP: latency degraded", lp, None, f"median round trip {v:.0f} ms vs normal {base:.0f} ms"))
    return out


def bridge_latency(orders, limit_ms=50):
    a = orders[orders.book == "A"].dropna(subset=["lp_send_time"])
    proc = (a.lp_send_time - a.bridge_recv_time).dt.total_seconds() * 1000
    m = proc.groupby(a.lp_send_time.dt.floor("10s")).median()
    return [_alert(t + pd.Timedelta("10s"), "HIGH", "bridge: processing delay", None, None, f"median {v:.0f} ms between bridge receipt and LP send")
            for t, v in m[m > limit_ms].items()]


def quantity_mapping(life, orders, contract):
    lots = orders.set_index("order_id").lots
    l = life.copy()
    l["order_id"] = l.cl_ord_id.str.extract(r"BR(\d+)-")[0].astype(int)
    first = l[l.cl_ord_id.str.endswith("-1")]
    ratio = first.order_qty / first.order_id.map(lots)
    bad = first[~np.isclose(ratio, first.symbol.map(contract))]
    out = []
    for (lp, s), g in bad.groupby(["lp", "symbol"]):
        out.append(_alert(g.sent.min(), "CRITICAL", "orders: quantity mapping", lp, s,
                          f"{len(g)} orders sent with {ratio[g.index].median():g} units per lot (expected {contract[s]:g})"))
    return out


def duplicate_deals(deals):
    d = deals[deals.lp_exec_id.notna() & deals.duplicated("lp_exec_id", keep="first")]
    return [_alert(r.time, "CRITICAL", "booking: duplicate deal", r.lp, r.symbol, f"ExecID {r.lp_exec_id} booked twice (order {r.order_id})")
            for r in d.itertuples()]


def markup_check(cq, pip, configured, tolerance=0.5):
    w = cq.assign(spread=cq.ask - cq.bid).pivot_table(index=["time", "symbol"], columns="group", values="spread").reset_index()
    w["mk"] = (w.STD - w.RAW) / 2 / w.symbol.map(pip)
    w["cfg"] = w.symbol.map(configured)
    bad = w[(w.mk - w.cfg).abs() > tolerance]
    out = []
    for s, g in bad.groupby("symbol"):
        out.append(_alert(g.time.min(), "HIGH", "pricing: markup deviates from config", None, s,
                          f"STD markup {g.mk.median():.1f} pips vs configured {configured[s]} until {g.time.max():%H:%M:%S}"))
    return out


def slippage_asymmetry(orders, pip):
    out = []
    f = orders[orders.filled_lots > 0]
    sl = (f.requested_price - f.fill_price) * np.where(f.side == "BUY", 1, -1) / f.symbol.map(pip)
    for book, g in sl.groupby(f.book):
        pos, neg = (g > 0.05).mean(), (g < -0.05).mean()
        if neg > 0.02 and pos < 0.1 * neg:
            out.append(_alert(f.server_fill_time.max(), "HIGH", "conduct: asymmetric slippage", None, None,
                              f"{book}-book: {pos:.1%} positive vs {neg:.1%} negative slippage"))
    return out


def toxic_clients(orders, quotes, pip, horizon_s=5, min_fills=20, limit_pips=0.5):
    f = orders[orders.filled_lots > 0].copy()
    f["sign"] = np.where(f.side == "BUY", 1, -1)
    f["mo"] = np.nan
    for s in f.symbol.unique():
        mid = A.mid_series(quotes, s)
        m = f.symbol == s
        f.loc[m, "mo"] = (A.value_at(mid, f.loc[m, "server_fill_time"] + pd.Timedelta(seconds=horizon_s))
                          - A.value_at(mid, f.loc[m, "server_fill_time"])) * f.loc[m, "sign"] / pip[s]
    g = f.groupby("client_id").agg(n=("mo", "size"), mo=("mo", "mean"), book=("book", "first"), last_fill=("server_fill_time", "max"))
    return [_alert(r.last_fill, "MEDIUM", "flow: toxic client", None, None,
                   f"{cid} ({r.book}-book): avg {horizon_s}s markout {r.mo:+.2f} pips over {r.n} fills")
            for cid, r in g[(g.n >= min_fills) & (g.mo > limit_pips)].iterrows()]


def lp_reconciliation(msgs, lp_stmt):
    got = set(msgs[(msgs.msg_type == "8")].exec_id.dropna())
    miss = lp_stmt[~lp_stmt.exec_id.isin(got)]
    return [_alert(r.trade_time, "CRITICAL", "recon: LP execution missing at bridge", r.lp, r.symbol,
                   f"{r.exec_id} for {r.cl_ord_id} on LP statement only") for r in miss.itertuples()]


# ----------------------------------------------------------------------------- orchestration
def run_all():
    msgs = A.load_fix_messages()
    quotes = A.lp_quotes(msgs)
    orders, deals = A.load_platform()
    cq = A.load_client_quotes()
    sym, cfg, audit, clients = A.load_reference()
    pip, contract = sym.pip_size.to_dict(), sym.contract_size.to_dict()
    life = lp_orders(msgs)
    alerts = (session_events(msgs) + lp_silence(msgs) + symbol_freeze(quotes) +
              spikes(quotes, pip, {"EURUSD": 10, "GBPUSD": 10, "XAUUSD": 30}) + crossed_book(quotes, pip, down=session_down_windows(msgs)) +
              spread_regime(quotes, pip) + clock_offset(msgs) + unanswered(life) + reject_rate(life) +
              lp_latency(life) + bridge_latency(orders) + quantity_mapping(life, orders, contract) +
              duplicate_deals(deals) + markup_check(cq, pip, cfg["markups_pips_per_side"]["STD"]) +
              slippage_asymmetry(orders, pip) + toxic_clients(orders, quotes, pip) +
              lp_reconciliation(msgs, A.load_lp_statements()))
    df = pd.DataFrame(alerts).sort_values("time").reset_index(drop=True)
    df["sev_rank"] = df.severity.map(SEV)
    return df


def group_incidents(alerts, gap="11min"):
    a = alerts.copy()
    a["key"] = a.rule + "|" + a.lp.fillna("-") + "|" + a.symbol.fillna("-")
    a = a.sort_values(["key", "time"])
    a["new"] = a.groupby("key").time.diff().gt(pd.Timedelta(gap)) | a.groupby("key").cumcount().eq(0)
    a["incident"] = a.new.cumsum()
    inc = a.groupby("incident").agg(start=("time", "min"), end=("time", "max"), rule=("rule", "first"), lp=("lp", "first"),
                                    symbol=("symbol", "first"), severity=("severity", "first"), alerts=("time", "size"),
                                    first_detail=("detail", "first"))
    return inc.sort_values("start").reset_index(drop=True)
