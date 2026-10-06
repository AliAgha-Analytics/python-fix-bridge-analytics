"""
Simulates one trading session (2 hours) of a retail FX/CFD broker's liquidity bridge and writes
every artefact a dealing / risk-operations team would look at:

    data/fix_logs/BRIDGE_LP_A.log ...   raw FIX 4.4 session logs (market data, orders, session msgs)
    data/platform/orders.csv            client orders as seen by the trading platform (with hop times)
    data/platform/deals.csv             deals booked on the platform
    data/platform/client_quotes.csv     prices streamed to clients per group (after markup)
    data/lp_statements/LP_*_trades.csv  end-of-day trade confirmations sent by each LP
    data/reference/*                    symbols, bridge configuration, bridge audit log

ARCHITECTURE (mirrors a real broker set-up)
    3 liquidity providers --FIX market data--> BRIDGE / AGGREGATOR --markup--> trading platform --> clients
    clients --orders--> platform --B-book: filled internally
                                --A-book: bridge --FIX NewOrderSingle--> best-priced LP (last look) --> fills

INCIDENTS deliberately planted (each lesson shows how to detect and explain them):
    S01 11:52:13  LP_C sends a bad EURUSD tick 50 pips off market (no spike filter on the bridge)
    S02 12:05:00  LP_B XAUUSD feed goes stale for 90 s while gold rallies -> crossed book, latency arbitrage
    S03 12:30:00  News event: price jump, spreads widen, depth shrinks, quote rate surges, bridge queue
    S04 12:40-13:00  Fat-finger markup on GBPUSD Standard group (8.0 pips instead of 0.8)
    S05 12:45:10  LP_B sequence gap -> buggy ResendRequest -> PossDup fill processed twice (duplicate deal)
    S06 13:00:00  LP_C stops sending (incl. heartbeats) -> TestRequest -> Logout -> reconnect with seq reset;
                  a fill sent by LP_C during the outage never reaches the platform (missing fill)
    S07 13:10-13:20  LP_A order latency spike (+200 ms)
    S08 all day   LP_C asymmetric last look (rejects only when price moves in the client's favour)
    S09 all day   B-book asymmetric slippage (negative slippage passed on, positive slippage kept)
    S10 all day   XAUUSD quantity mapping bug for LP_C (bridge sends ounces = lots x 1 instead of x 100)
    S11 all day   Latency-arbitrage clients with toxic flow (positive markouts)
    S12 all day   LP_C clock runs 25 ms ahead (negative one-way latencies if you trust SendingTime)
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import pandas as pd

from . import fix

SEED = 15
T0 = pd.Timestamp("2025-10-15 11:30:00")
DUR_MS = 2 * 3600 * 1000
OUT = "data"


def fq(x: float) -> str:
    """Quantity formatting for FIX: 2000000 -> '2000000', 1.5 -> '1.5' (never scientific notation)."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def ms(hhmmss: str) -> int:
    """'12:30:00.000' -> ms since T0."""
    return int((pd.Timestamp(f"2025-10-15 {hhmmss}") - T0) / pd.Timedelta(milliseconds=1))


def ts(t_ms: float) -> str:
    """ms since T0 -> FIX UTCTimestamp '20251015-11:30:00.123'."""
    t = T0 + pd.Timedelta(milliseconds=int(round(t_ms)))
    return t.strftime("%Y%m%d-%H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def iso(t_ms: float) -> str:
    t = T0 + pd.Timedelta(milliseconds=int(round(t_ms)))
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


# ----------------------------------------------------------------------------- reference data
SYMBOLS = {
    #          start     pip     digits  daily_vol  news_jump  contract  lp_qty_unit
    "EURUSD": (1.16250, 0.0001, 5,      0.0018,    +0.0022,   100000,   "units"),
    "GBPUSD": (1.33480, 0.0001, 5,      0.0022,    +0.0018,   100000,   "units"),
    "XAUUSD": (4085.40, 0.10,   2,      0.0040,    +6.00,     100,      "ounces"),
}
LPS = {
    # spread in pips by symbol, net latency one-way (ms), last look hold (ms), L1 size (contract units), style
    "LP_A": dict(spread={"EURUSD": 0.4, "GBPUSD": 0.7, "XAUUSD": 2.2}, net=2.5, hold=0, size_l1=(2e6, 3e6, 300),
                 clock=0, style="firm"),
    "LP_B": dict(spread={"EURUSD": 0.5, "GBPUSD": 0.8, "XAUUSD": 2.5}, net=8.0, hold=20, size_l1=(1e6, 2e6, 200),
                 clock=0, style="symmetric last look"),
    "LP_C": dict(spread={"EURUSD": 0.3, "GBPUSD": 0.6, "XAUUSD": 1.9}, net=4.0, hold=120, size_l1=(1e6, 1e6, 100),
                 clock=25, style="asymmetric last look"),
}
MARKUP_PIPS = {"STD": {"EURUSD": 0.8, "GBPUSD": 0.8, "XAUUSD": 2.0}, "RAW": {"EURUSD": 0.0, "GBPUSD": 0.0, "XAUUSD": 0.0}}
QTY_MULT = {(lp, s): SYMBOLS[s][5] for lp in LPS for s in SYMBOLS}
QTY_MULT[("LP_C", "XAUUSD")] = 1                                    # S10 mapping bug

NEWS = ms("12:30:00")
BAD_TICK = ms("11:52:13.400")
STALE = (ms("12:05:00"), ms("12:06:30"))
MARKUP_BUG = (ms("12:40:00"), ms("13:00:00"))
GAP_T = ms("12:45:10")
LPC_DOWN = ms("13:00:00")
LPC_TESTREQ = ms("13:00:12")
LPC_DROP = ms("13:00:22")
LPC_UP = ms("13:02:30")
LPA_SLOW = (ms("13:10:00"), ms("13:20:00"))
CONGEST = (ms("12:30:00"), ms("12:30:30"))
ORDER_TIMEOUT = 1000


class World:
    def __init__(self, seed=SEED):
        self.rng = np.random.default_rng(seed)
        self.grid = np.arange(0, DUR_MS + 1, 100)
        self.mid = {s: self._mid_path(s) for s in SYMBOLS}

    # ------------------------------------------------------------------ true market
    def _mid_path(self, s):
        start, pip, digits, vol, jump, *_ = SYMBOLS[s]
        n = len(self.grid)
        step_sd = vol / math.sqrt(86400 * 10)
        mult = np.ones(n)
        after = self.grid >= NEWS
        mult[after] += 4.0 * np.exp(-(self.grid[after] - NEWS) / 90000)
        rets = self.rng.normal(0, step_sd, n) * mult
        path = start * np.exp(np.cumsum(rets))
        j = np.clip((self.grid - NEWS) / 2000, 0, 1)                    # 2-second news jump
        path = path + jump * j
        if s == "XAUUSD":                                                # rally during the LP_B stale window
            r = np.clip((self.grid - STALE[0]) / (STALE[1] - STALE[0]), 0, 1)
            path = path + 4.5 * r
        return path

    def mid_at(self, s, t):
        return float(np.interp(t, self.grid, self.mid[s]))

    def widen(self, t):
        if NEWS - 30000 <= t < NEWS:
            return 1.6
        if t >= NEWS:
            return 1 + 5.0 * math.exp(-(t - NEWS) / 45000)
        return 1.0

    # ------------------------------------------------------------------ LP price streams
    def lp_quotes(self):
        rows = []
        for lp, cfg in LPS.items():
            for s, (start, pip, digits, *_r) in SYMBOLS.items():
                t, times = 0.0, []
                while t < DUR_MS:
                    rate = 1.0 / 1000 * (4 if NEWS <= t < NEWS + 60000 else 1)
                    t += self.rng.exponential(1 / rate)
                    times.append(t)
                times = np.array([x for x in times if x < DUR_MS])
                if lp == "LP_C":
                    times = times[(times < LPC_DOWN) | (times > LPC_UP + 1500)]
                if lp == "LP_B" and s == "XAUUSD":
                    times = times[(times < STALE[0]) | (times >= STALE[1])]
                for tt in times:
                    rows.append(self._quote(lp, s, tt))
                if lp == "LP_C" and s == "EURUSD":
                    q = self._quote(lp, s, BAD_TICK)
                    for k in ("bid1", "bid2", "ask1", "ask2"):
                        q[k] = round(q[k] - 0.0050, digits)
                    rows.append(q)
                    rows.append(self._quote(lp, s, BAD_TICK + 200))           # LP corrects 200 ms later
        q = pd.DataFrame(rows).sort_values("t_send").reset_index(drop=True)
        return q

    def _quote(self, lp, s, t):
        cfg = LPS[lp]
        start, pip, digits, *_r = SYMBOLS[s]
        w = self.widen(t)
        m = self.mid_at(s, t) + self.rng.normal(0, 0.03 * pip)
        sp = cfg["spread"][s] * pip * w * self.rng.uniform(0.9, 1.15)
        size_l1 = {"EURUSD": cfg["size_l1"][0], "GBPUSD": cfg["size_l1"][1], "XAUUSD": cfg["size_l1"][2]}[s]
        depth = 0.35 if t >= NEWS and t < NEWS + 90000 else 1.0
        l1 = size_l1 * depth * self.rng.choice([1, 1, 1.5, 2])
        net = cfg["net"] + self.rng.exponential(0.8)
        return dict(lp=lp, symbol=s, t_send=t, t_recv=t + net,
                    bid1=round(m - sp / 2, digits), ask1=round(m + sp / 2, digits),
                    bid2=round(m - sp / 2 - 0.3 * pip * w, digits), ask2=round(m + sp / 2 + 0.3 * pip * w, digits),
                    bsize1=l1, asize1=l1, bsize2=l1 * 2, asize2=l1 * 2)


def generate(out_dir: str = OUT, seed: int = SEED):
    w = World(seed)
    rng = w.rng
    quotes = w.lp_quotes()

    # ---------------------------------------------------------------- bridge book (by bridge receive time)
    book_idx = {}                     # (lp, symbol) -> (recv times array, row indices)
    for (lp, s), g in quotes.groupby(["lp", "symbol"]):
        g = g.sort_values("t_recv")
        book_idx[(lp, s)] = (g.t_recv.to_numpy(), g.index.to_numpy())
    send_idx = {}
    for (lp, s), g in quotes.groupby(["lp", "symbol"]):
        g = g.sort_values("t_send")
        send_idx[(lp, s)] = (g.t_send.to_numpy(), g.index.to_numpy())

    def connected(lp, t):
        return not (lp == "LP_C" and LPC_DROP <= t < LPC_UP + 1500)

    def bridge_view(s, t):
        """Latest quote per connected LP as known by the bridge at time t."""
        view = {}
        for lp in LPS:
            if not connected(lp, t):
                continue
            times, idx = book_idx[(lp, s)]
            k = np.searchsorted(times, t, side="right") - 1
            if k >= 0:
                view[lp] = quotes.loc[idx[k]]
        return view

    def lp_own_quote(lp, s, t):
        times, idx = send_idx[(lp, s)]
        k = np.searchsorted(times, t, side="right") - 1
        return quotes.loc[idx[max(k, 0)]]

    def markup(group, s, t):
        m = MARKUP_PIPS[group][s]
        if group == "STD" and s == "GBPUSD" and MARKUP_BUG[0] <= t < MARKUP_BUG[1]:
            m = 8.0
        return m * SYMBOLS[s][1]

    # ---------------------------------------------------------------- client quotes (platform feed)
    agg_rows = []
    for s in SYMBOLS:
        times = np.sort(np.concatenate([book_idx[(lp, s)][0] for lp in LPS]))
        for t in times:
            v = bridge_view(s, t)
            if not v:
                continue
            bb = max(v.items(), key=lambda kv: kv[1].bid1)
            ba = min(v.items(), key=lambda kv: kv[1].ask1)
            for g in ("STD", "RAW"):
                mk = markup(g, s, t)
                d = SYMBOLS[s][2]
                agg_rows.append(dict(time=iso(t), t=t, symbol=s, group=g,
                                     bid=round(bb[1].bid1 - mk, d), ask=round(ba[1].ask1 + mk, d),
                                     bid_lp=bb[0], ask_lp=ba[0]))
    cq = pd.DataFrame(agg_rows).sort_values(["t", "symbol", "group"]).reset_index(drop=True)
    cq_idx = {(s, g): (gq.t.to_numpy(), gq.index.to_numpy()) for (s, g), gq in cq.groupby(["symbol", "group"])}

    def client_quote(s, g, t):
        times, idx = cq_idx[(s, g)]
        k = np.searchsorted(times, t, side="right") - 1
        return cq.loc[idx[max(k, 0)]]

    # ---------------------------------------------------------------- clients and order flow
    clients = []
    arb = {"C1007": "B", "C1023": "B", "C1041": "A"}
    for i in range(60):
        cid = f"C{1001 + i}"
        book = arb.get(cid) or ("A" if rng.random() < 0.55 else "B")
        group = "RAW" if cid in arb or rng.random() < 0.35 else "STD"
        clients.append(dict(client_id=cid, group=group, book=book, arb=cid in arb))
    cl = pd.DataFrame(clients).set_index("client_id")

    orders = []
    t = 0.0
    normal = [c for c in cl.index if not cl.loc[c, "arb"]]
    while t < DUR_MS:
        rate = 0.17 / 1000 * (5 if NEWS <= t < NEWS + 60000 else 1)
        t += rng.exponential(1 / rate)
        if t >= DUR_MS - 5000:
            break
        cid = str(rng.choice(normal))
        s = str(rng.choice(list(SYMBOLS), p=[0.45, 0.2, 0.35]))
        lots = float(np.clip(round(rng.lognormal(0, 1.0), 2), 0.01, 60))
        orders.append(dict(client_id=cid, symbol=s, side=str(rng.choice(["BUY", "SELL"])), lots=lots, t_send=t))
    for cid in arb:                                             # toxic clients: trade ahead of short-term moves
        t = rng.uniform(0, 60000)
        while t < DUR_MS - 5000:
            s = str(rng.choice(["EURUSD", "XAUUSD"]))
            fut = w.mid_at(s, t + 1500) - w.mid_at(s, t)
            side = ("BUY" if fut > 0 else "SELL") if rng.random() < 0.75 else str(rng.choice(["BUY", "SELL"]))
            orders.append(dict(client_id=cid, symbol=s, side=side, lots=float(round(rng.uniform(2, 10), 1)), t_send=t))
            t += rng.exponential(120000)
    for k, cid in enumerate(["C1007", "C1023", "C1041"]):     # S02: buy gold on the stale LP_B ask
        for t in np.arange(STALE[0] + 8000 + k * 1700, STALE[1] - 2000, 6000):
            orders.append(dict(client_id=cid, symbol="XAUUSD", side="BUY", lots=5.0, t_send=float(t)))
    orders.append(dict(client_id="C1007", symbol="EURUSD", side="BUY", lots=20.0, t_send=BAD_TICK + 60))   # S01
    orders.append(dict(client_id="C1041", symbol="EURUSD", side="BUY", lots=10.0, t_send=BAD_TICK + 75))
    a_normals = [c for c in normal if cl.loc[c, "book"] == "A"]
    orders.append(dict(client_id=a_normals[0], symbol="EURUSD", side="BUY", lots=2.0,
                       t_send=GAP_T - 250, force_lp="LP_B"))                                         # S05
    orders.append(dict(client_id=a_normals[1], symbol="XAUUSD", side="SELL", lots=3.0,
                       t_send=LPC_DOWN - 110, force_lp="LP_C"))                                      # S06

    od = pd.DataFrame(orders).sort_values("t_send").reset_index(drop=True)
    od["order_id"] = np.arange(700001, 700001 + len(od))

    # ---------------------------------------------------------------- execution
    msgs = {lp: [] for lp in LPS}            # (t_send_sender_clock, t_log, direction, msgtype, body, extra_header)
    lp_trades, deals, out_orders = [], [], []
    exec_counter = {lp: 0 for lp in LPS}
    gap_fill_info = {}

    def lp_net(lp, t):
        base = LPS[lp]["net"] + rng.exponential(1.0)
        if lp == "LP_A" and LPA_SLOW[0] <= t < LPA_SLOW[1]:
            base += rng.uniform(170, 240)
        return base

    for _, o in od.iterrows():
        c = cl.loc[o.client_id]
        s, side, lots = o.symbol, o.side, o.lots
        sgn = 1 if side == "BUY" else -1
        pip, digits = SYMBOLS[s][1], SYMBOLS[s][2]
        q_seen = client_quote(s, c.group, o.t_send - rng.uniform(20, 60))     # what the client saw
        requested = q_seen.ask if side == "BUY" else q_seen.bid
        t_srv = o.t_send + rng.uniform(15, 70)
        rec = dict(order_id=o.order_id, client_id=o.client_id, group=c.group, book=c.book, symbol=s, side=side,
                   lots=lots, client_send_time=iso(o.t_send), requested_price=requested, server_recv_time=iso(t_srv))
        if c.book == "B":                                                        # internalised
            t_fill = t_srv + rng.uniform(1, 4)
            q = client_quote(s, c.group, t_fill)
            market = q.ask if side == "BUY" else q.bid
            worse = (market - requested) * sgn > 0
            price = market if worse else requested                              # S09 asymmetric slippage
            rec.update(bridge_recv_time=None, lp_send_time=None, lp_response_time=None, server_fill_time=iso(t_fill),
                       client_recv_time=iso(t_fill + rng.uniform(15, 70)), status="FILLED", filled_lots=lots,
                       fill_price=round(price, digits), lp=None, attempts=0, reject_reason=None)
            deals.append(dict(order_id=o.order_id, client_id=o.client_id, symbol=s, side=side, lots=lots,
                              price=round(price, digits), time=iso(t_fill), book="B", lp=None, lp_exec_id=None))
            out_orders.append(rec)
            continue

        # ---- A-book: bridge routing with last look, partial fills and re-routing
        t_b = t_srv + rng.uniform(1, 3)
        proc = rng.uniform(0.5, 2) + (rng.uniform(80, 250) if CONGEST[0] <= t_b < CONGEST[1] else 0)
        t_now = t_b + proc
        remaining = lots
        fills, tried, attempts, reason = [], set(), 0, None
        first_send, last_resp = None, None
        while remaining > 1e-9 and attempts < 3:
            view = {lp: q for lp, q in bridge_view(s, t_now).items() if lp not in tried}
            if not view:
                reason = reason or "No liquidity"
                break
            if attempts == 0 and isinstance(o.get("force_lp"), str):
                lp = o.force_lp
                quote = bridge_view(s, t_now)[lp]
            else:
                lp, quote = (min(view.items(), key=lambda kv: kv[1].ask1) if side == "BUY"
                             else max(view.items(), key=lambda kv: kv[1].bid1))
            tried.add(lp)
            attempts += 1
            limit = quote.ask1 if side == "BUY" else quote.bid1
            lp_qty = round(remaining * QTY_MULT[(lp, s)], 2)
            clord = f"BR{o.order_id}-{attempts}"
            body = [(11, clord), (1, "BRIDGE01"), (55, s), (54, 1 if side == "BUY" else 2), (60, ts(t_now)),
                    (38, fq(lp_qty)), (40, 2), (44, f"{limit:.{digits}f}"), (59, 3)]
            msgs[lp].append((t_now, t_now, "OUT", "D", body, None))
            first_send = first_send or t_now
            if lp == "LP_C" and LPC_DOWN <= t_now < LPC_UP:                    # session dead: LP never gets it
                last_resp = None
                t_now += ORDER_TIMEOUT
                reason = "LP timeout"
                break
            t_lp = t_now + lp_net(lp, t_now)
            t_dec = t_lp + (LPS[lp]["hold"] * rng.uniform(0.8, 1.25) if LPS[lp]["hold"] else rng.uniform(0.2, 0.6))
            cur = lp_own_quote(lp, s, t_dec)                                    # LP's latest streamed quote (sizes)
            half = LPS[lp]["spread"][s] * pip * w.widen(t_dec) / 2
            cur_px = w.mid_at(s, t_dec) + sgn * half                            # LP's live internal price
            moved = (cur_px - limit) * sgn / pip                                # >0 = moved in client's favour? no:
            # moved > 0 means the LP's price is now WORSE for the client (market went the client's way)
            style = LPS[lp]["style"]
            if style == "firm":
                reject = rng.random() < 0.005
                rtext = "Internal error"
            elif style == "symmetric last look":
                reject = abs(moved) > {"XAUUSD": 1.5}.get(s, 0.3) or rng.random() < 0.01
                rtext = "Price moved"
            else:
                reject = moved > {"XAUUSD": 0.4}.get(s, 0.1) or rng.random() < 0.01
                rtext = "Off quotes"
            l1 = cur.asize1 if side == "BUY" else cur.bsize1
            avail_lots = l1 / SYMBOLS[s][5]
            oid = f"{lp}-O{o.order_id}{attempts}"
            lp_clock = LPS[lp]["clock"]
            t_back = t_dec + lp_net(lp, t_dec)
            if lp == "LP_C" and LPC_DOWN <= t_dec < LPC_UP:                     # S06 fill lost in the outage
                exec_counter[lp] += 1
                lp_trades.append(dict(lp=lp, exec_id=f"{lp}-E{exec_counter[lp]:06d}", cl_ord_id=clord, symbol=s, side=side,
                                      qty=lp_qty, price=limit, trade_time=iso(t_dec + lp_clock)))
                t_now = t_now + ORDER_TIMEOUT
                reason = "LP timeout"
                break
            if reject:
                body = [(37, oid), (11, clord), (17, f"{lp}-X{o.order_id}{attempts}"), (150, 8), (39, 8), (55, s),
                        (54, 1 if side == "BUY" else 2), (38, fq(lp_qty)), (14, 0), (151, 0), (6, 0), (58, rtext),
                        (60, ts(t_dec + lp_clock))]
                msgs[lp].append((t_dec + lp_clock, t_back, "IN", "8", body, None))
                reason, t_now, last_resp = rtext, t_back + rng.uniform(0.3, 1.0), t_back
                continue
            fill_lots = min(remaining, max(avail_lots, 0.01))
            fill_qty = round(fill_lots * QTY_MULT[(lp, s)], 2)
            exec_counter[lp] += 1
            eid = f"{lp}-E{exec_counter[lp]:06d}"
            partial = fill_lots < remaining - 1e-9
            body = [(37, oid), (11, clord), (17, eid), (150, "F"), (39, 1 if partial else 2), (55, s),
                    (54, 1 if side == "BUY" else 2), (38, fq(lp_qty)), (31, f"{limit:.{digits}f}"),
                    (32, fq(fill_qty)), (14, fq(fill_qty)), (151, fq(max(lp_qty - fill_qty, 0))),
                    (6, f"{limit:.{digits}f}"), (60, ts(t_dec + lp_clock))]
            msgs[lp].append((t_dec + lp_clock, t_back, "IN", "8", body, None))
            if partial:                                                         # IOC remainder cancelled
                body2 = [(37, oid), (11, clord), (17, f"{lp}-C{o.order_id}{attempts}"), (150, 4), (39, 4), (55, s),
                         (54, 1 if side == "BUY" else 2), (38, fq(lp_qty)), (14, fq(fill_qty)), (151, 0),
                         (6, f"{limit:.{digits}f}"), (58, "IOC remainder cancelled"), (60, ts(t_dec + lp_clock + 1))]
                msgs[lp].append((t_dec + lp_clock + 1, t_back + 1, "IN", "8", body2, None))
            lp_trades.append(dict(lp=lp, exec_id=eid, cl_ord_id=clord, symbol=s, side=side, qty=fill_qty,
                                  price=limit, trade_time=iso(t_dec + lp_clock)))
            fills.append(dict(lp=lp, lots=fill_lots, price=limit, exec_id=eid, t=t_back))
            remaining -= fill_lots
            t_now, last_resp = t_back + rng.uniform(0.3, 1.0), t_back

        filled = sum(f["lots"] for f in fills)
        mk = markup(c.group, s, t_now) * sgn
        if filled > 0:
            vwap = sum(f["lots"] * f["price"] for f in fills) / filled
            price = round(vwap + mk, digits)
            status = "FILLED" if remaining <= 1e-9 else "PARTIAL"
            t_fill = max(f["t"] for f in fills) + rng.uniform(1, 3)
            for f in fills:
                deals.append(dict(order_id=o.order_id, client_id=o.client_id, symbol=s, side=side, lots=f["lots"],
                                  price=round(f["price"] + mk, digits), time=iso(f["t"] + 2), book="A", lp=f["lp"],
                                  lp_exec_id=f["exec_id"]))
        else:
            price, status, t_fill = None, "REJECTED", t_now + rng.uniform(1, 3)
        rec.update(bridge_recv_time=iso(t_b), lp_send_time=iso(first_send) if first_send else None,
                   lp_response_time=iso(last_resp) if last_resp else None, server_fill_time=iso(t_fill),
                   client_recv_time=iso(t_fill + rng.uniform(15, 70)), status=status, filled_lots=round(filled, 2),
                   fill_price=price, lp=",".join(f["lp"] for f in fills) or None, attempts=attempts,
                   reject_reason=None if filled > 0 else reason)
        out_orders.append(rec)

    # ---------------------------------------------------------------- market data messages
    for _, q in quotes.iterrows():
        d = SYMBOLS[q.symbol][2]
        body = [(262, f"MD-{q.lp}-{q.symbol}"), (55, q.symbol), (268, 4),
                (269, 0), (270, f"{q.bid1:.{d}f}"), (271, fq(q.bsize1)), (290, 1),
                (269, 0), (270, f"{q.bid2:.{d}f}"), (271, fq(q.bsize2)), (290, 2),
                (269, 1), (270, f"{q.ask1:.{d}f}"), (271, fq(q.asize1)), (290, 1),
                (269, 1), (270, f"{q.ask2:.{d}f}"), (271, fq(q.asize2)), (290, 2)]
        clock = LPS[q.lp]["clock"]
        msgs[q.lp].append((q.t_send + clock, q.t_recv, "IN", "W", body, None))

    # ---------------------------------------------------------------- session layer + write logs
    os.makedirs(f"{out_dir}/fix_logs", exist_ok=True)
    resent_exec_ids = []
    for lp in LPS:
        resent_exec_ids += _write_session(lp, msgs[lp], rng, f"{out_dir}/fix_logs/BRIDGE_{lp}.log")

    # duplicate deal from the PossDup resend (S05): find the LP_B fill just before the gap
    ob = pd.DataFrame(out_orders)
    dd = pd.DataFrame(deals)
    for eid in resent_exec_ids:                     # the bridge books the PossDup re-sent fill a second time
        dup = dd[dd.lp_exec_id == eid].iloc[0].to_dict()
        dup["time"] = iso(GAP_T + 16)
        deals.append(dup)

    # ---------------------------------------------------------------- write data files
    os.makedirs(f"{out_dir}/platform", exist_ok=True)
    os.makedirs(f"{out_dir}/lp_statements", exist_ok=True)
    os.makedirs(f"{out_dir}/reference", exist_ok=True)
    ob.to_csv(f"{out_dir}/platform/orders.csv", index=False, lineterminator="\n")
    dd = pd.DataFrame(deals).sort_values("time").reset_index(drop=True)
    dd.insert(0, "deal_id", np.arange(900001, 900001 + len(dd)))
    dd.to_csv(f"{out_dir}/platform/deals.csv", index=False, lineterminator="\n")
    cq.drop(columns="t").to_csv(f"{out_dir}/platform/client_quotes.csv", index=False, lineterminator="\n")
    lt = pd.DataFrame(lp_trades)
    for lp, g in lt.groupby("lp"):
        g.drop(columns="lp").sort_values("trade_time").to_csv(f"{out_dir}/lp_statements/{lp}_trades.csv",
                                                             index=False, lineterminator="\n")
    pd.DataFrame([dict(symbol=s, pip_size=v[1], digits=v[2], contract_size=v[5], lp_quantity_unit=v[6])
                  for s, v in SYMBOLS.items()]).to_csv(f"{out_dir}/reference/symbols.csv", index=False,
                                                       lineterminator="\n")
    cfg = {
        "bridge": "BRIDGE01",
        "routing": {"A_book": "best price across connected LPs, IOC limit at LP quote, up to 3 attempts on reject",
                    "order_timeout_ms": ORDER_TIMEOUT, "price_filters": "NONE", "stale_quote_filter": "NONE"},
        "sessions": {lp: {"heartbeat_interval_s": 10, "md_depth": 2} for lp in LPS},
        "markups_pips_per_side": MARKUP_PIPS,
        "lp_quantity_multiplier": {f"{lp}:{s}": QTY_MULT[(lp, s)] for lp in LPS for s in SYMBOLS},
        "b_book_execution": {"slippage_mode": "client_adverse_only"},
    }
    with open(f"{out_dir}/reference/bridge_config.json", "w") as f:
        json.dump(cfg, f, indent=2)
    audit = pd.DataFrame([
        dict(time="2025-10-14 16:20:05.000", user="admin", object="LP_C session",
             change="lp_quantity_multiplier XAUUSD set to 1", ticket="CHG-2291 (new LP onboarding)"),
        dict(time=iso(MARKUP_BUG[0] - 3200), user="dealer2", object="Group STD / GBPUSD",
             change="markup 0.8 -> 8.0 pips", ticket="none"),
        dict(time=iso(MARKUP_BUG[1] + 400), user="dealer1", object="Group STD / GBPUSD",
             change="markup 8.0 -> 0.8 pips", ticket="INC-5521 client complaints"),
    ])
    audit.to_csv(f"{out_dir}/reference/bridge_audit_log.csv", index=False, lineterminator="\n")
    pd.DataFrame([dict(client_id=c, group=r.group, book=r.book) for c, r in cl.iterrows()]).to_csv(
        f"{out_dir}/reference/clients.csv", index=False, lineterminator="\n")
    return dict(quotes=len(quotes), orders=len(ob), deals=len(dd), lp_trades=len(lt))


def _write_session(lp, items, rng, path):
    """Adds the session layer (logon, MD subscription, heartbeats, test request, logout, sequence gap and
    resend) to the application messages, assigns sequence numbers and writes the log."""
    clock = LPS[lp]["clock"]
    ev = list(items)
    net = LPS[lp]["net"]
    syms = list(SYMBOLS)

    def logon_pair(t, reset):
        ev.append((t, t, "OUT", "A", [(98, 0), (108, 10)] + ([(141, "Y")] if reset else []), None))
        ev.append((t + net + 1 + clock, t + 2 * net + 2, "IN", "A", [(98, 0), (108, 10)] + ([(141, "Y")] if reset else []), None))
        body = [(262, f"MD-{lp}"), (263, 1), (264, 2), (265, 0), (146, len(syms))] + [(55, s) for s in syms]
        ev.append((t + 2 * net + 5, t + 2 * net + 5, "OUT", "V", body, None))

    logon_pair(-1500.0, True)
    if lp == "LP_C":
        ev.append((LPC_TESTREQ, LPC_TESTREQ, "OUT", "1", [(112, "TEST-130012")], None))
        ev.append((LPC_DROP, LPC_DROP, "OUT", "5", [(58, "Heartbeat timeout: no response to TestRequest")], None))
        logon_pair(float(LPC_UP), True)
    # heartbeats: each side sends one after 10 s of silence in its own direction
    def silent(direction, t):
        if lp != "LP_C":
            return False
        if direction == "IN":
            return LPC_DOWN <= t < LPC_UP
        return LPC_DROP <= t < LPC_UP
    for direction in ("OUT", "IN"):
        own = sorted(e[1] for e in ev if e[2] == direction) + [DUR_MS]
        last = -1500.0
        for nxt in own:
            while nxt - last > 10000:
                last += 10000
                if not silent(direction, last):
                    if direction == "OUT":
                        ev.append((last, last, "OUT", "0", [], None))
                    else:
                        ev.append((last + clock, last + net, "IN", "0", [], None))
            last = max(last, nxt)

    ev.sort(key=lambda e: (e[1], 0 if e[2] == "OUT" else 1))
    seq = {"OUT": 0, "IN": 0}
    lines, gap_done, resend_items, resent_exec = [], False, [], []
    sender = {"OUT": "BRIDGE01", "IN": lp}
    target = {"OUT": lp, "IN": "BRIDGE01"}
    history_in = []                                   # (seq, msgtype, body, original sending time)
    for e in ev:
        t_sender, t_log, direction, mtype, body, extra = e
        if mtype == "A" and dict(body).get(141) == "Y":
            seq[direction] = 0
        if lp == "LP_B" and direction == "IN" and not gap_done and t_log >= GAP_T:
            gap_done = True
            expected = seq["IN"] + 1                  # first sequence number that will be lost
            seq["IN"] += 5                            # S05: five messages lost in transit
            current = seq["IN"] + 1                   # sequence number of the message that reveals the gap
            begin = expected - 3                      # BUG: bridge asks from 3 messages before the gap
            seq["OUT"] += 1
            rr = fix.build("2", "BRIDGE01", lp, seq["OUT"], ts(t_log + 1), [(7, begin), (16, 0)], sep="|")
            lines.append((t_log + 1, "OUT", rr))
            # LP answer: application messages are re-sent with PossDupFlag=Y, everything else is gap-filled
            run_start = None
            items = [h for h in history_in if h[0] >= begin] + [(n, "LOST", None, None) for n in range(expected, current)]
            for s_no, m_type, m_body, m_time in items:
                if m_type == "8":
                    if run_start is not None:
                        raw = fix.build("4", lp, "BRIDGE01", run_start, ts(t_log + 9 + clock), [(123, "Y"), (36, s_no)],
                                        extra_header=[(43, "Y")], sep="|")
                        resend_items.append((t_log + 14, "IN", raw))
                        run_start = None
                    raw = fix.build("8", lp, "BRIDGE01", s_no, ts(t_log + 9 + clock), m_body,
                                    extra_header=[(43, "Y"), (122, m_time)], sep="|")
                    resend_items.append((t_log + 14, "IN", raw))
                    resent_exec.append(dict(m_body)[17])
                elif run_start is None:
                    run_start = s_no
            if run_start is not None:
                raw = fix.build("4", lp, "BRIDGE01", run_start, ts(t_log + 9 + clock), [(123, "Y"), (36, current)],
                                extra_header=[(43, "Y")], sep="|")
                resend_items.append((t_log + 15, "IN", raw))
        seq[direction] += 1
        raw = fix.build(mtype, sender[direction], target[direction], seq[direction], ts(t_sender), body, sep="|")
        lines.append((t_log, direction, raw))
        if direction == "IN":
            history_in.append((seq[direction], mtype, body, ts(t_sender)))
    lines += resend_items
    lines.sort(key=lambda x: x[0])
    with open(path, "w", encoding="ascii", newline="\n") as f:
        for t_log, direction, raw in lines:
            f.write(f"{ts(t_log)} {direction:<3} {raw}\n")
    return resent_exec
