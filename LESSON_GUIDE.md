# Lesson Guide: what each step means and how it's done in real life

The notebooks show the code and the results. This guide explains, in plain words, **what each section is doing,
why a dealing / risk operations desk cares, and how you would do the same thing at work** (where you usually have a
log viewer, a bridge admin panel, a platform manager terminal and Excel rather than Python).

Read it side by side with the notebook: the headings below match the notebook sections one to one.

> Notation: in FIX logs the invisible separator character **SOH** (ASCII code 1) is shown as `|`.
> "Pip" = 0.0001 for EURUSD/GBPUSD, and 0.10 USD for XAUUSD in this dataset.

---

## Contents

- [Lesson 00: Market structure primer](#lesson-00-market-structure-primer)
- [Lesson 01: Reading FIX messages](#lesson-01-reading-fix-messages)
- [Lesson 02: FIX session health](#lesson-02-fix-session-health)
- [Lesson 03: Market data and aggregation](#lesson-03-market-data-and-aggregation)
- [Lesson 04: Feed quality, mispricing and price filters](#lesson-04-feed-quality-mispricing-and-price-filters)
- [Lesson 05: Routing and LP execution](#lesson-05-routing-and-lp-execution)
- [Lesson 06: Execution quality / TCA](#lesson-06-execution-quality--tca)
- [Lesson 07: Three-way reconciliation](#lesson-07-three-way-reconciliation)
- [Lesson 08: Monitoring and the incident playbook](#lesson-08-monitoring-and-the-incident-playbook)
- [Glossary of the words that keep coming back](#glossary)

---

## Lesson 00: Market structure primer

**One sentence:** a broker doesn't make prices itself; it buys them from liquidity providers (LPs), merges them in
a bridge, adds a markup, shows them to clients, and either keeps the client's risk (B-book) or passes it to an LP
(A-book).

**Think of it like a currency exchange shop.** Three wholesalers (LP_A, LP_B, LP_C) phone in their buy/sell prices
every second. The shop owner (the bridge) writes the best buy price and the best sell price on the board, adds a
margin (the markup), and serves customers. For some customers the shop simply keeps the trade (B-book). For others
it immediately places the same trade with a wholesaler (A-book), so it carries no risk.

**What a dealing desk watches every day**

| Area | The question | Lesson |
|---|---|---|
| Connections | Are all LP sessions up and healthy? | 02 |
| Prices | Are the prices we show real, fresh and sensible? | 03, 04 |
| Orders | Are LPs filling us fairly and fast? | 05 |
| Clients | Are clients getting the price and spread we promise? Who is "too smart"? | 06 |
| Books | Do our records agree with the LP's records? | 07 |
| Alerts | Would we have noticed all of this without looking? | 08 |

**Real-life tools:** the bridge vendor's admin panel (PrimeXM XCore, oneZero Hub, Centroid, Gold-i…) shows LP
sessions, the aggregated book, routing rules and markups. The trading platform (MT4/MT5 Manager, cTrader) shows
client orders and deals. FIX logs are text files on the bridge server.

---

## Lesson 01: Reading FIX messages

**One sentence:** every price, order and fill between the bridge and an LP is a FIX message; this lesson teaches you
to read one by eye, the way you'd read a log line when an LP says "we never got your order".

### 1. Split a message into tag=value fields

A FIX message is just a line of text made of pairs: `number=value`. The number (the **tag**) says what the value
means. Example (the bridge logging on to LP_B):

```
8=FIX.4.4|9=73|35=A|49=BRIDGE01|56=LP_B|34=1|52=20251015-11:29:58.500|98=0|108=10|141=Y|10=117|
```

Read it pair by pair:

| Field | Tag name | Meaning in plain words |
|---|---|---|
| `8=FIX.4.4` | BeginString | "This is FIX version 4.4" |
| `9=73` | BodyLength | "The message body is 73 characters long" |
| `35=A` | MsgType | "This is a **Logon**" |
| `49=BRIDGE01` | SenderCompID | Who sends it: our bridge |
| `56=LP_B` | TargetCompID | Who receives it: LP_B |
| `34=1` | MsgSeqNum | Message number 1 of this session |
| `52=20251015-11:29:58.500` | SendingTime | When it was sent (UTC, to the millisecond) |
| `98=0` | EncryptMethod | No encryption (standard) |
| `108=10` | HeartBtInt | "If we're quiet, we'll send a heartbeat every 10 seconds" |
| `141=Y` | ResetSeqNumFlag | "Restart message numbering from 1" |
| `10=117` | CheckSum | Integrity check (see section 3) |

**In real life:** you open the log in a text editor or a FIX log viewer (many free ones exist, and most bridge
panels have a built-in one) and search for an order ID. You learn the 20–30 common tags by heart; the rest you look
up on a FIX dictionary website (e.g. "FIX 4.4 tag 59").

### 2. Header, body and trailer

Every message has the same three parts:

- **Header** (tags 8, 9, 35, 49, 56, 34, 52, sometimes 43/122): the envelope. Who, to whom, what type, which
  number, what time.
- **Body**: the content, which depends on the type. For an order: symbol, side, quantity, price…
- **Trailer** (tag 10): the checksum, always last.

It's like a letter: the envelope (header), the letter itself (body), and a seal (trailer).

### 3. Validate BodyLength (9) and CheckSum (10) by hand

These two fields protect the message against corruption. The receiving side recomputes both; if either doesn't
match, the message is thrown away.

**BodyLength (tag 9): count the characters between `9=73|` and `10=`.**
Start counting right after the separator that ends `9=73`, and stop right before `10=`. Every separator counts as
one character.

| Field (with its separator) | Characters |
|---|---|
| `35=A|` | 5 |
| `49=BRIDGE01|` | 12 |
| `56=LP_B|` | 8 |
| `34=1|` | 5 |
| `52=20251015-11:29:58.500|` | 25 |
| `98=0|` | 5 |
| `108=10|` | 7 |
| `141=Y|` | 6 |
| **Total** | **73** ✔ matches `9=73` |

**CheckSum (tag 10): add up the ASCII code of every character before `10=`, then keep the remainder after
dividing by 256.**
Each character has a number (e.g. `8` = 56, `=` = 61, `F` = 70, SOH = 1). For this message all the characters from
`8=FIX.4.4` up to the separator before `10=` add up to **4,469**. 4,469 ÷ 256 = 17 remainder **117**, written as
three digits → `10=117` ✔.

Important detail: the checksum is computed on the **real SOH byte (value 1)**, not on the `|` you see in logs
(`|` is 124). That's why the notebook replaces `|` with SOH before computing.

**The tamper demo:** the notebook changes one price in an order (adds a `9` to tag 44). BodyLength goes from 148
to 149 and the checksum from 218 to 019, so both checks fail. A real counterparty would answer with a
session-level **Reject (35=3)** and the order would never be executed.

**In real life:** you almost never compute these by hand. The FIX engine (QuickFIX, Onix, the bridge's own engine)
does it automatically. You need to understand them because:
- when an LP onboarding test fails with "invalid checksum" or "incorrect BodyLength", it usually means someone
  edited or built messages wrongly (wrong separator, extra space, encoding issue);
- when you copy a message from a log into an email or a ticket, you'll notice that the `|` version is "invalid"
  as raw FIX, which is normal.

### 4. Decode the message: tag names and enum values

Many values are codes (an **enum**). You must translate them:

| Tag | Code → meaning |
|---|---|
| 54 Side | 1 = Buy, 2 = Sell |
| 40 OrdType | 1 = Market, 2 = Limit |
| 59 TimeInForce | 3 = IOC (Immediate or Cancel: fill now what you can, cancel the rest), 1 = GTC |
| 150 ExecType | 0 = New, F = Trade (fill), 8 = Rejected, 4 = Canceled |
| 39 OrdStatus | 0 = New, 1 = Partially filled, 2 = Filled, 4 = Canceled, 8 = Rejected |

So `54=2|38=490|40=2|59=3` on XAUUSD reads: **sell 490 ounces of gold, limit order, IOC**.

**In real life:** the bridge GUI decodes these for you, but LPs and support teams talk in raw tags
("you sent 59=1 instead of 59=3"), so you need to know the common ones.

### 5. Build a message yourself

The notebook builds a message from scratch with `fix.build()`. The point is to show that the header, the body
length and the checksum are **derived**: you choose the content, the rest follows. Real-life equivalent: the
LP's onboarding certification, where both sides exchange test messages in a UAT (test) environment before going
live.

### 6. Repeating groups: a market-data snapshot (35=W)

A price update from an LP contains **several price levels** in one message. FIX does this with a **repeating
group**: a counter tag, then the same set of tags repeated.

The first gold snapshot from LP_A in the log (body only, spaced out for reading):

```
55=XAUUSD | 268=4 | 269=0|270=4085.32|271=600|290=1 | 269=0|270=4085.29|271=1200|290=2
                  | 269=1|270=4085.52|271=600|290=1 | 269=1|270=4085.55|271=1200|290=2
```

- `268=4` → "4 entries follow"
- each entry: `269` type (0 = bid, 1 = ask/offer), `270` price, `271` size, `290` level (1 = best)

So LP_A says: *I buy at 4085.32 (up to 600 oz) or 4085.29 (another 1,200 oz); I sell at 4085.52 (600 oz) or
4085.55 (1,200 oz).* Spread at level 1 = 4085.52 − 4085.32 = 0.20 USD = 2 pips.

**In real life:** this is what you look at when an LP's price "looks wrong" on the platform: you go to the raw 35=W
message to prove whether the LP sent it or the bridge produced it.

### 7. Message types in a whole session log

Counting messages by type gives you the **shape** of a session: thousands of 35=W (prices), a few hundred D and 8
(orders and fills), a handful of 0 (heartbeats), one A (logon)… Anything unusual (a 35=2 ResendRequest, a 35=5
Logout in the middle of the day) is a reason to dig.

**In real life:** `grep -c "35=W" logfile` or the bridge's statistics page. A sudden drop in 35=W count for one LP
means its feed stopped.

### 8. Follow one order end to end (D → 8 → re-route → 8)

This is the most common real investigation: *"Client says his order was rejected / filled at a bad price. What
happened?"*

The notebook's example, client order **700335** (buy 218,000 EURUSD = 2.18 lots):

| Time | Direction | What happens |
|---|---|---|
| 11:57:09.410 | OUT `35=D` to LP_A | Bridge sends `11=BR700335-1` (ClOrdID = our reference), buy 218,000 at limit 1.16280, IOC |
| 11:57:09.417 | IN `35=8` from LP_A | `150=8` **Rejected**, `58=Internal error` (7 ms later) |
| 11:57:09.418 | OUT `35=D` to LP_B | Bridge re-routes 1 ms later as attempt `-2`: `11=BR700335-2` |
| 11:57:09.451 | IN `35=8` from LP_B | `150=F` **Trade**: `31=1.16280` (price), `32=218000` (qty), plus an **ExecID** `17=` (the LP's trade reference) |

So the client was filled at the price he asked for, 41 ms after the first attempt, by the second LP.

**How to link them:** the ClOrdID. Our bridge builds it as `BR<platform order number>-<attempt number>`, so all
attempts of one client order share the same prefix. The LP returns our ClOrdID in its reply.

**In real life:** you take the order number from the platform (MT5 Manager), search for it in the bridge's
order log or the FIX log, and reconstruct the timeline: time sent, which LP, answer, re-route, final fill. That
timeline is what you send back to the client support team or the LP.

### 9. Partial fills

If you ask for more than the LP has at that price, it fills part (`150=F`, `39=1` partially filled, `32` LastQty,
`151` LeavesQty), and because the order is IOC the rest is cancelled (`150=4`, `58=IOC remainder cancelled`). The
bridge then sends the remainder to the next LP. The notebook's example, client order **700004** (sell 9.4 lots
of gold = 940 oz):

| Attempt | LP | Sent | Filled | Then |
|---|---|---|---|---|
| `BR700004-1` | LP_C | 9.4 | 1.5 | rest cancelled |
| `BR700004-2` | LP_A | 790 oz | 300 oz | rest cancelled |
| `BR700004-3` | LP_B | 490 oz | 200 oz | rest cancelled |

Two things to notice:
1. One client order became **three LP trades** at three slightly different prices (4085.76 / 4085.73 / 4085.67).
   The client's fill is the volume-weighted average, and reconciliation must match one deal to several ExecIDs.
2. Look at the first line: LP_A and LP_B receive gold in **ounces** (790, 490), but LP_C received **9.4**. That's
   the quantity mapping bug (S10) already visible in the very first gold order of the day: LP_C was sent 9.4 oz
   instead of 940 oz. Lesson 05 §8 and lesson 07 find it properly.

**In real life:** this matters for averaging the client's fill price and for reconciliation (one client deal may
match **several** LP trades).

**Interview line:** *"I can read raw FIX logs: follow an order from 35=D to the execution reports by ClOrdID,
decode Side/OrdType/TIF/ExecType, explain partial fills and re-routes, and I know what BodyLength and CheckSum
protect against."*

---

## Lesson 02: FIX session health

**One sentence:** before worrying about prices and fills, check that the "phone line" with each LP is healthy:
alive, in order, not losing messages, and with clocks you can trust.

### 1. Session events timeline

A FIX session has a life: **Logon (A)** → requests for market data (V) → normal traffic → **Logout (5)**. The
notebook lists every session-level event per LP on a timeline. Anything between logon and the end of day other than
heartbeats is worth a look.

**In real life:** the bridge panel shows each LP session as green/red with "connected since". Ops teams check it at
the start of every shift and after every alert.

### 2. Heartbeats and silence detection

When there's no traffic, each side sends a **Heartbeat (35=0)** every `HeartBtInt` seconds (here 10s), just to
say "I'm still here". If nothing arrives for longer than that, the other side sends a **TestRequest (35=1)**:
"are you there? answer with this ID". No answer → disconnect.

**What happened (S06):** LP_C went silent for **151 seconds**. At 13:00:12 the bridge sent a TestRequest, got no
answer, logged out at 13:00:22, and LP_C came back at 13:02:30 with a new Logon with `141=Y` (numbering restarts
at 1).

**Why it matters:** for a market-data session, silence means **frozen prices**. For an order session, orders sent
during the silence get **no answer** (3 LP_C orders here) and you don't know if they were filled.

**In real life:** the bridge raises a "session disconnected" alarm; the dealer calls/emails the LP's support desk,
checks for orders in an unknown state, and may disable the LP in routing until it's stable.

### 3. Sequence numbers: detect gaps

Every message carries a number (tag 34) that goes up by one: 1, 2, 3… Each side checks that the next number is
exactly the previous +1. If you expect 14,124 and receive 14,129, **messages 14,124–14,128 were lost**: that's a
**gap**.

It's like numbered pages of a fax: if page 5 arrives after page 3, you know page 4 is missing.

**In real life:** the FIX engine detects this automatically and logs "MsgSeqNum too high, expecting X but received
Y". Ops sees it in the engine log.

### 4. The recovery, and the bug in it

When a gap is detected, the receiver sends a **ResendRequest (35=2)**: `7=BeginSeqNo`, `16=EndSeqNo` (0 = "up to
the latest"). The sender then resends the missing messages with **PossDupFlag `43=Y`** ("this may be a
duplicate") and the original time in `122`, or sends a **SequenceReset-GapFill (35=4, 123=Y)** for messages not
worth resending (like old heartbeats).

**The bug (S05):** the bridge asked from **14,121** instead of 14,124 (3 too early). So messages it **already had**
came back again, including a fill (ExecID **LP_B-E000075**) marked `43=Y`. The bridge didn't check PossDup / ExecID
and passed it on → the platform booked the same fill **twice** (lesson 07 finds the duplicate deals).

**Rule to remember:** any message with `43=Y` must be checked against what you already processed (by ExecID for
fills). If seen already → ignore it.

**In real life:** this is a classic production incident. The fix is in the bridge configuration / vendor code; the
immediate action is to find and cancel the duplicate deal on the platform and confirm the true position with the
LP.

### 5. Clocks: can we trust SendingTime?

To measure latency you compare timestamps from **two different machines**. If their clocks disagree, the latency is
wrong. LP_C's clock was **25 ms ahead**, which made its market data look like it arrived **before it was sent**
(negative latency of about −20 ms), which is impossible.

**How to estimate the offset:** use a round trip. The bridge sends Logon at time T1 (our clock); LP_C answers with
its own timestamp; we receive at T2 (our clock). The true LP time should be about the middle of T1 and T2. If the
LP's timestamp is 25 ms later than that midpoint, its clock is ~25 ms ahead. (Logon round trip here was 10 ms, so
the estimate is accurate to a few ms.)

**Round-trip latency per LP (order sent → answer):** LP_A ~7 ms, LP_B ~38 ms, LP_C ~134 ms (LP_C holds orders
longer: last look).

**In real life:** servers are synced with NTP or PTP. Regulated firms (MiFID II RTS 25) must keep clocks within set
tolerances of UTC. If an LP's timestamps look off, you raise it with them; meanwhile you only trust latencies
measured with **your own** clock on both ends (send and receive).

### 6. Session health report

A one-table summary per LP: uptime, disconnects, gaps, resends, heartbeats missed, clock offset, median round
trip. This is the kind of table you'd paste into a daily ops report or an LP review meeting.

**Interview line:** *"I monitor FIX sessions for heartbeats/TestRequests, sequence gaps and resend handling, I know
why PossDup messages must be de-duplicated by ExecID, and I check clock offsets before trusting latency numbers."*

---

## Lesson 03: Market data and aggregation

**One sentence:** turn thousands of raw LP price messages into the single best price the clients see, and
understand who drives it.

### 1. How often does each LP update?

Count price updates per second per LP. A healthy LP updates often when the market moves. A rate that suddenly
drops to zero = frozen feed (lesson 04). A rate that jumps = news (section 7).

**In real life:** the bridge's "market data statistics" screen, or counting 35=W per minute in the logs.

### 2. Spreads by LP

Spread = ask − bid, in pips. Here median EURUSD spreads were **LP_A 0.4, LP_B 0.5, LP_C 0.3 pips**. LP_C looks
the cheapest… lesson 05 shows whether that's true after its rejects.

**In real life:** LPs are compared in monthly reviews on spread, but **spread alone means nothing** without fill
ratio and rejects.

### 3. Build the aggregated book

At every moment, keep the **latest quote from each LP**, then take the **highest bid** and the **lowest ask** across
LPs. That's the aggregated top of book. Example: LP_A bid 1.16248, LP_B bid 1.16249, LP_C bid 1.16247 → best bid
1.16249 (LP_B).

**Check against the platform:** we rebuilt the book ourselves and compared to what the platform streamed to the
RAW group (no markup). **100% match**, which proves we understand exactly how the bridge builds prices.

**In real life:** you do this when a client disputes a price ("there was never such a price!"). You rebuild the book
at that millisecond from the LP feeds and show which LP was best.

### 4. Who sets the price? Time-weighted share of the top of book

For what % of the time was each LP the best bid / best ask? An LP that is "best" 70% of the time but rejects a lot
of orders is a problem: it attracts the flow, then refuses it.

### 5. Aggregated spread vs single-LP spreads

The aggregated spread is narrower than any single LP's, because the best bid and best ask can come from different
LPs. That's the whole value of aggregation.

**But** if the book is **crossed** (best bid > best ask) or **locked** (equal), something is wrong: usually one LP's
price is stale. Here: crossed EURUSD 0.89% of the time, GBPUSD 0.27%, XAUUSD 2.63%, clustered around **12:05**
(LP_B gold frozen, S02) and **13:00** (LP_C down, S06).

**In real life:** most bridges have a "crossed book protection" setting (drop the stale side, or don't stream).

### 6. Depth: how much size is available at the top?

Size at the best price matters as much as the price. If the top has only 500k and a client wants 2M, the order will
be partially filled or move through several levels.

### 7. News event deep-dive (S03, 12:30:00)

Typical behaviour at news: updates per second jump (**2.9 → 12/s**), spreads widen (**EURUSD 0.4 → 1.4 pips**),
top-of-book size falls (**1.5M → 525k**), and the price jumps ~20 pips in 2 seconds.

**In real life:** desks prepare before scheduled news (NFP, CPI, central banks): widen markups, reduce max order
size, switch some flow to A-book or vice versa, warn support. After the news they review exactly these metrics.

**Interview line:** *"I can rebuild the aggregated book from LP feeds, validate it against what clients saw, and
explain spreads, top-of-book share, depth and crossed books, including how they behave around news."*

---

## Lesson 04: Feed quality, mispricing and price filters

**One sentence:** find prices that were not real (stale or wrong), measure what they cost the broker, and design
the filters that would have blocked them.

### 1. A book that remembers quote ages

Quote age = "how long since this LP last updated this symbol". A price from 0.5 s ago is fine; a price from 60 s ago
in a moving market is a **ghost**: it may still be the best price on the board, but nobody would trade there.

### 2. Stale quotes at the top of the book

How do we pick a threshold? Look at normal update intervals: median **0.67 s**, 99th percentile **4.6 s**, 99.9th
**7 s**. A threshold of **6 s** catches real freezes without false alarms.

**What happened (S02):** LP_B gold stopped updating for **90 seconds** while gold rallied. Its old (low) ask stayed
the best ask → crossed book, and **B-book clients bought gold at a price that no longer existed**.

**In real life:** a bridge "max quote age" / "stale feed" filter per LP and symbol. When it trips, that LP is
removed from the book until it updates again.

### 3. Bad ticks: compare each LP with the consensus

A **bad tick** is a single quote far from where the market is. To spot it, compare each LP's mid price with the
**consensus** (the median of all fresh LPs). Thresholds: 10 pips for EUR/GBP, 30 pips for gold.

**What happened (S01):** LP_C sent EURUSD **~50 pips below the market** for 200 ms, then corrected it.

**Why median and not average?** One crazy price pulls the average a lot but barely moves the median.

**In real life:** "spike filter" / "deviation filter" in the bridge. Some LPs also send a correction; the damage
happens in the milliseconds before.

### 4. The damage: client trades at prices that weren't real

Find client fills that happened at those ghost prices and value them at the real market price:
- S01: **1 EURUSD fill, ≈ $9,970 loss** (client C1007, B-book, bought 20 lots within ~90 ms).
- S02: **23 gold B-book fills, 4 clients, ≈ $31,683**.

The A-book attempt during S01 was rejected by LP_C itself (it didn't honour its own bad price) – the B-book had no
such protection.

**In real life:** this becomes an **incident report** and often a **price correction / trade cancellation**
decision. Most brokers' client agreements allow cancelling trades done on "manifest error" prices, but it must be
handled carefully and consistently (compliance involved).

### 5. Replay: what would the filters have prevented?

Re-run the whole session **with** the stale filter and spike filter turned on, and compare: crossed gold book time
drops from **574 to 123 quote-updates**, the bad tick disappears. This is how you justify a config change to your
manager: "here is what it would have saved, and here is what it would have wrongly blocked".

### 6. Streaming smoothness

Check the client price stream: gaps without updates, jumps, too many updates per second. Clients notice "freezing
then jumping" prices, and toxic clients exploit them.

**Interview line:** *"I measure quote age and consensus deviation to catch stale and bad prices, quantify the B-book
loss from mispriced fills, and back-test price filter settings before proposing them."*

---

## Lesson 05: Routing and LP execution

**One sentence:** score each LP on what really matters – does it fill our orders, how fast, and does it treat us
fairly?

### 1. Order lifecycle table from FIX

Turn all the D and 8 messages into one table: one row per order attempt with LP, time sent, time answered, result
(fill / reject / partial / no answer), fill price. Everything after builds on this table.

**In real life:** the bridge's "orders" or "execution report" export, usually to CSV/Excel.

### 2. LP scorecard (first look)

| LP | Orders | Result |
|---|---|---|
| LP_A | 390 | 99.7% filled |
| LP_B | 142 | 16.2% rejected |
| LP_C | 331 | 27.8% rejected, 3 never answered |

### 3. Is the last look symmetric? (S08)

Last look = the LP holds your order a few ms and can reject it if the price moved. Fair (**symmetric**): reject if
it moved a lot **either way**. Unfair (**asymmetric**): reject only when the move is bad for the LP (good for the
client), fill when it's good for the LP.

**The test:** for each rejected order, check the market a moment after (here +300 ms): did it move in the
client's favour or against? Symmetric LP → about half and half. Result: **LP_B 57%** in client's favour (roughly
fair), **LP_C 91%** (clearly asymmetric).

**In real life:** this is a standard LP review analysis. The FX Global Code (Principle 17) says last look should
not be used to profit from price moves; you'd raise it with the LP or reduce its routing share.

### 4. What does a reject cost? Is the "tightest" LP really the cheapest?

When LP_C rejects, the order is re-routed, usually at a worse price. Average cost of LP_C rejects after re-route:
**0.32 pips**, versus a spread advantage of only **0.05 pips**. So the "cheapest" LP is actually the most expensive.

### 5. Partial fills and liquidity sweeps

How often orders needed several LPs to complete, and for which sizes. Large orders → more partials → more slippage.
Used to set max order sizes per symbol.

### 6. Latency: did any LP degrade? (S07)

LP_A's round trip went from **~7 ms to ~416 ms** for 10 minutes (13:10–13:20). Because we measure send and receive
with our own clock, we know it's the LP side, not our bridge.

**In real life:** you send the LP the timestamps and ask what happened; meanwhile you may lower its routing priority.

### 7. Orders that never got an answer (S06)

3 orders were sent to LP_C just before it went down and got no reply. These are dangerous: **you don't know if you
have a position**. Lesson 07 shows one of them was actually filled by LP_C.

**In real life:** immediately ask the LP for the status of these ClOrdIDs ("order status request" or by email/chat).

### 8. Are we sending the right quantity? (S10)

Different LPs use different units: lots, ounces, units of currency. Gold: 1 lot = 100 oz. The bridge config for
LP_C/XAUUSD had multiplier **1 instead of 100**, so a 0.85-lot order was sent as **0.85 oz** instead of 85 oz:
**1/100 of the size**. Our hedge was 100× too small.

**In real life:** quantity/contract-size mapping is checked on every new LP or symbol setup. A wrong multiplier is
one of the most expensive config mistakes because it silently leaves positions unhedged.

**Interview line:** *"I build LP scorecards from FIX (fill ratio, rejects, hold time, latency), test last look for
asymmetry, compute the true cost of rejects, and check quantity mapping per LP."*

---

## Lesson 06: Execution quality / TCA

**One sentence:** TCA (transaction cost analysis) checks, from the **client's** side, whether execution is fair and
as advertised, and from the **broker's** side, who is costing money.

### 1. Slippage by book: is it symmetric? (S09)

Slippage = fill price vs requested price, signed so positive = better for the client. If the market moves during
execution, clients should sometimes get better prices and sometimes worse.

- A-book: **4% positive, 25% negative** – two-sided, follows the real market.
- B-book: **0% positive, 3.6% negative** – the platform passes on bad moves but keeps good ones.

That asymmetry is a regulatory problem (FCA, ESMA, CySEC all expect symmetric price slippage). The notebook
estimates **~$198** withheld from clients in this session.

**In real life:** check the platform's execution settings (e.g. "max deviation" and how positive slippage is
handled) and fix the configuration.

### 2. Latency decomposition (A-book)

The orders file has a timestamp at each hop: client → server → bridge → LP → back. Subtract them to see **where** the
time goes. At the news (12:30) the **bridge's own processing** went from ~1 ms to ~160 ms (a queue); during S07 the
**LP round trip** was ~386 ms. Same symptom ("slow fills"), different culprit.

### 3. Does latency cost money? It depends on who carries the risk

In A-book, latency mostly hurts the client (price moves before the LP fills). In B-book with fills at the requested
price, latency hurts the **broker** (fast clients pick off old prices).

### 4. Markups: are clients getting the spread we think? (S04)

Compare the price shown to each group with the raw aggregated price; the difference is the actual markup. GBPUSD
for the STD group was **8.0 pips instead of 0.8** from 12:40 to 12:59. The **bridge audit log** shows who changed it
(user **dealer2**). Impact: **26 trades, 21 clients, ≈ $3,738 overcharged** → refund list.

**In real life:** every config change should be logged and reviewed (four-eyes). A daily markup check catches typos.

### 5. Markouts: who is trading on information? (S11)

Markout = how much the market moves in the client's favour **after** their trade (100 ms, 1 s, 5 s, 30 s, 60 s).
Normal clients average ~0. Toxic / latency-arbitrage clients consistently win right after trading. Top 5-second
markouts: **C1023 +1.33 pips, C1007 +0.65, C1041 +0.61** – the same accounts that hit the stale/bad prices.

**In real life:** dealing desks review these clients and may move them to A-book, add delays, widen their spread
group or review their trades, according to company policy and the client agreement.

### 6. Execution quality summary

A one-page best-execution style report: fill rates, slippage symmetry, latency percentiles, markups, markouts. This
is what risk, compliance or management actually reads.

**Interview line:** *"I run TCA: slippage symmetry per book, per-hop latency decomposition, markup audits against
the audit log, and markouts to identify toxic flow."*

---

## Lesson 07: Three-way reconciliation

**One sentence:** make sure the **platform**, the **bridge** and the **LP** all agree on every trade and every
position, because if they don't, the broker has risk it doesn't know about.

### 1. Choose the matching key and the common unit

- **Key:** the **ExecID** (the LP's unique trade ID). It's in the FIX fill, in the LP statement, and stored on the
  platform deal. ClOrdID isn't enough (one order can have several fills).
- **Unit:** convert everything to **lots** using the official contract size (EURUSD 100,000; gold 100 oz). Never
  compare "as sent" quantities: the bug in S10 is exactly a unit problem.

### 2. Recon 1: bridge (FIX) vs LP statement

Every LP sends an end-of-day trade file. Match it to the fills in our FIX logs.
- **Missing fill:** LP_C's statement has ExecID **LP_C-E000189** (order BR701128-1, 0.03 lots) that our bridge never
  received – sent right before the session died (S06). The client (C1003) was told "LP timeout", but **LP_C did
  fill it**. We have an LP position with no client position: an orphan hedge.
- **Quantity mismatches:** **97** LP_C gold trades where we booked e.g. 85.21 lots-equivalent vs 0.85 at the LP (S10).

### 3. Recon 2: platform deals vs bridge fills

- **Duplicate deals:** deals **901028 and 901029** for order 700965 (client C1001) both point to the same ExecID
  **LP_B-E000075**: the PossDup resend from S05 was booked twice.

### 4. Position reconciliation by LP and symbol

Sum net positions per LP and symbol on both sides. Breaks: **LP_B EURUSD +2 lots (~$233k)** from the duplicate,
**LP_C XAUUSD −2.08 lots (~−$851k)** from the quantity bug. Trade-level recon tells you *why*, position recon tells
you *how much risk*.

### 5. Break register

Every break gets a line: ID, date, type, amount, root cause, owner, action, status (e.g. RB-1 duplicate deal → cancel
deal 901029; RB-2 missing fill → book or close with LP; RB-3 quantity mapping → fix multiplier, rebalance hedge).

**In real life:** done daily, often in Excel or a recon tool (e.g. Duco, SmartStream TLM, or in-house SQL). Breaks
must be explained and closed quickly; old open breaks are an audit finding.

**Interview line:** *"I reconcile platform vs bridge vs LP statements on ExecID in normalised units, classify breaks
(duplicates, missing fills, quantity mapping), quantify position breaks and track them in a break register."*

---

## Lesson 08: Monitoring and the incident playbook

**One sentence:** turn everything from lessons 02–07 into automatic rules so the desk finds problems in seconds,
not the next morning.

### 1. From alerts to incidents

17 rules (session drop, silent LP, frozen symbol, spike, crossed book, wide spread, clock offset, unanswered orders,
reject rate, LP latency, bridge latency, quantity mapping, duplicate deals, markup check, slippage asymmetry, toxic
clients, LP recon). They raised **43 alerts**. One problem triggers several rules, so alerts close in time (within
11 minutes) for the same LP/symbol are grouped into **30 incidents**. People read incidents, not raw alerts.

### 2. Score the monitor: did it catch everything, and how fast?

All **12/12** incidents were detected. Detection delay: bad tick and session drop **instantly**, stale gold
**5.7 s**, markup **3.6 s**, news **30 s**, LP latency **2 min**, asymmetric last look **10 min** (needs enough
orders), B-book slippage and toxic clients only at **end of session** (they are statistical, you need a sample).

**Lesson:** some problems are detectable per message (real-time), others only over many trades (daily/weekly
reports). A good monitoring setup has both.

### 3. One root cause, many alerts: the LP_C outage (S06)

The LP_C drop fired silence, session, crossed-book, unanswered-order and recon alerts. Recognising they share one
cause saves time and avoids five people chasing five "different" problems.

### 4. Incident playbook

For each incident type: **how it's detected → first checks → immediate action → who to tell → follow-up**. Example
for a stale feed: confirm in the bridge panel → disable the LP for that symbol → check fills during the window →
notify the LP and risk → review filter settings.

**In real life:** alerts go to email, Slack/Teams or a dashboard (Grafana, the bridge's own alerts, ITRS Geneos…).
The playbook lives in the team wiki and is updated after every incident (post-mortem).

### 5. Where to go from here

Ideas to extend: real-time streaming, dashboards, more statistical tests, connecting to a real FIX test session.

**Interview line:** *"I designed monitoring rules covering sessions, prices, execution, markups, flow toxicity and
reconciliation, grouped alerts into incidents, measured detection delays, and wrote an incident playbook."*

---

## Glossary

| Term | Plain meaning |
|---|---|
| **LP (liquidity provider)** | A bank or market maker that gives the broker prices and fills its hedge orders |
| **Bridge / aggregator** | Software connecting the platform to LPs: merges prices, adds markups, routes orders |
| **A-book / B-book** | Pass the client's trade to an LP / keep it in-house as the counterparty |
| **FIX** | The text protocol LPs and bridges use to talk |
| **ClOrdID (11)** | Our order reference |
| **ExecID (17)** | The LP's trade (fill) reference: unique per fill, the key for reconciliation |
| **IOC** | Immediate or Cancel: fill what's possible now, cancel the rest |
| **Last look** | LP's right to reject an order after receiving it if the price moved |
| **Stale quote** | A price that hasn't updated for too long |
| **Bad tick / spike** | A single price far from the market |
| **Crossed book** | Best bid above best ask: a sign something is wrong |
| **Markup** | What the broker adds to the LP spread for a client group |
| **Slippage** | Fill price vs requested price |
| **Markout** | Market move after a trade: shows informed / toxic flow |
| **Break** | A difference between two records that should agree |
| **PossDup (43=Y)** | "This message may already have been sent": de-duplicate it |
