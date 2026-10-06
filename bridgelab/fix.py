"""
Minimal FIX 4.4 toolkit: build, parse, validate and decode tag=value messages.

A FIX message is a sequence of  tag=value  fields separated by the SOH character (ASCII 1).
Log files usually display SOH as '|', which is what this project does; the functions here
accept either separator.

    8=FIX.4.4 | 9=BodyLength | 35=MsgType | ...header... | ...body... | 10=CheckSum
    - BodyLength (9) = number of characters AFTER the 9=...<SOH> field, up to and including the
      SOH that precedes 10=
    - CheckSum  (10) = sum of all bytes before "10=" (including every SOH) modulo 256, 3 digits
"""
from __future__ import annotations

import re
from collections import OrderedDict

SOH = "\x01"

# ----------------------------------------------------------------------------- dictionary
TAGS = {
    1: "Account", 6: "AvgPx", 7: "BeginSeqNo", 8: "BeginString", 9: "BodyLength", 10: "CheckSum",
    11: "ClOrdID", 14: "CumQty", 15: "Currency", 16: "EndSeqNo", 17: "ExecID", 31: "LastPx",
    32: "LastQty", 34: "MsgSeqNum", 35: "MsgType", 36: "NewSeqNo", 37: "OrderID", 38: "OrderQty",
    39: "OrdStatus", 40: "OrdType", 43: "PossDupFlag", 44: "Price", 45: "RefSeqNum", 49: "SenderCompID",
    52: "SendingTime", 54: "Side", 55: "Symbol", 56: "TargetCompID", 58: "Text", 59: "TimeInForce",
    60: "TransactTime", 97: "PossResend", 98: "EncryptMethod", 103: "OrdRejReason", 108: "HeartBtInt",
    112: "TestReqID", 117: "QuoteID", 122: "OrigSendingTime", 123: "GapFillFlag", 141: "ResetSeqNumFlag",
    146: "NoRelatedSym", 150: "ExecType", 151: "LeavesQty", 262: "MDReqID", 263: "SubscriptionRequestType",
    264: "MarketDepth", 265: "MDUpdateType", 268: "NoMDEntries", 269: "MDEntryType", 270: "MDEntryPx",
    271: "MDEntrySize", 290: "MDEntryPositionNo", 371: "RefTagID", 372: "RefMsgType", 373: "SessionRejectReason",
}
NAME_TO_TAG = {v: k for k, v in TAGS.items()}

MSG_TYPES = {
    "0": "Heartbeat", "1": "TestRequest", "2": "ResendRequest", "3": "Reject", "4": "SequenceReset",
    "5": "Logout", "8": "ExecutionReport", "9": "OrderCancelReject", "A": "Logon", "D": "NewOrderSingle",
    "F": "OrderCancelRequest", "V": "MarketDataRequest", "W": "MarketDataSnapshotFullRefresh",
    "X": "MarketDataIncrementalRefresh", "Y": "MarketDataRequestReject", "j": "BusinessMessageReject",
}
ENUMS = {
    54: {"1": "Buy", "2": "Sell"},
    40: {"1": "Market", "2": "Limit", "D": "PreviouslyQuoted"},
    59: {"0": "Day", "1": "GTC", "3": "IOC", "4": "FOK"},
    150: {"0": "New", "4": "Canceled", "8": "Rejected", "F": "Trade", "I": "OrderStatus"},
    39: {"0": "New", "1": "PartiallyFilled", "2": "Filled", "4": "Canceled", "8": "Rejected"},
    269: {"0": "Bid", "1": "Offer"},
    263: {"0": "Snapshot", "1": "Subscribe", "2": "Unsubscribe"},
    43: {"Y": "PossibleDuplicate", "N": "Original"},
    123: {"Y": "GapFill", "N": "Reset"},
    141: {"Y": "ResetSequenceNumbers", "N": "KeepSequenceNumbers"},
}
HEADER_TAGS = (8, 9, 35, 49, 56, 34, 43, 97, 52, 122)
REPEATING_GROUP_START = {268: 269, 146: 55}          # NoXXX tag -> first tag of each group entry


# ----------------------------------------------------------------------------- build
def checksum(raw_without_checksum: str) -> str:
    """Sum of all bytes modulo 256, as 3 digits."""
    return f"{sum(raw_without_checksum.encode('ascii')) % 256:03d}"


def build(msg_type: str, sender: str, target: str, seq: int, sending_time: str, body: list[tuple[int, object]],
          extra_header: list[tuple[int, object]] | None = None, sep: str = SOH) -> str:
    """Assemble a complete message with correct BodyLength and CheckSum."""
    header = [(35, msg_type), (49, sender), (56, target), (34, seq)] + (extra_header or []) + [(52, sending_time)]
    payload = "".join(f"{t}={v}{SOH}" for t, v in header + list(body))
    head = f"8=FIX.4.4{SOH}9={len(payload)}{SOH}"
    raw = head + payload
    raw += f"10={checksum(raw)}{SOH}"
    return raw.replace(SOH, sep)


# ----------------------------------------------------------------------------- parse
def split_fields(raw: str) -> list[tuple[int, str]]:
    """'8=FIX.4.4|9=..|' -> [(8,'FIX.4.4'), (9,'..'), ...] keeping order and repeated tags."""
    raw = raw.strip().replace("|", SOH)
    out = []
    for part in raw.split(SOH):
        if part:
            tag, _, val = part.partition("=")
            out.append((int(tag), val))
    return out


def parse(raw: str) -> dict:
    """Flat dict of a message. Repeating-group tags are collected into lists under 'groups'."""
    fields = split_fields(raw)
    msg, groups, current, in_group = {}, [], None, None
    for tag, val in fields:
        if tag in REPEATING_GROUP_START:
            in_group = REPEATING_GROUP_START[tag]
            msg[tag] = val
            continue
        if in_group is not None and tag != 10:
            if tag == in_group:
                current = {}
                groups.append(current)
            if current is not None and tag not in HEADER_TAGS:
                current[tag] = val
                continue
        msg[tag] = val
    if groups:
        msg["groups"] = groups
    return msg


def validate(raw: str) -> dict:
    """Recompute BodyLength and CheckSum. Returns {'body_length_ok', 'checksum_ok', ...}."""
    s = raw.strip().replace("|", SOH)
    m = re.match(r"8=[^\x01]*\x019=(\d+)\x01", s)
    if not m:
        return {"valid": False, "error": "missing BeginString/BodyLength"}
    declared_len = int(m.group(1))
    cs_pos = s.rfind(SOH + "10=")
    body = s[m.end(): cs_pos + 1]
    declared_cs = s[cs_pos + 4: cs_pos + 7]
    calc_cs = checksum(s[: cs_pos + 1])
    res = {"body_length_declared": declared_len, "body_length_actual": len(body),
           "checksum_declared": declared_cs, "checksum_actual": calc_cs}
    res["body_length_ok"] = declared_len == len(body)
    res["checksum_ok"] = declared_cs == calc_cs
    res["valid"] = res["body_length_ok"] and res["checksum_ok"]
    return res


def explain(raw: str) -> list[dict]:
    """Human-readable breakdown: one row per field with tag name and decoded enum value."""
    rows = []
    for tag, val in split_fields(raw):
        meaning = ""
        if tag == 35:
            meaning = MSG_TYPES.get(val, "?")
        elif tag in ENUMS:
            meaning = ENUMS[tag].get(val, "?")
        rows.append({"tag": tag, "name": TAGS.get(tag, "?"), "value": val, "meaning": meaning})
    return rows


# ----------------------------------------------------------------------------- logs
LOG_LINE = re.compile(r"^(\d{8}-\d{2}:\d{2}:\d{2}\.\d{3}) (IN |OUT) (8=FIX.*)$")


def read_log(path: str) -> list[tuple[str, str, str]]:
    """Read a session log: each line is '<log time> <IN|OUT> <raw message>'."""
    out = []
    with open(path, encoding="ascii") as f:
        for line in f:
            m = LOG_LINE.match(line.rstrip("\n"))
            if m:
                out.append((m.group(1), m.group(2).strip(), m.group(3)))
    return out


def fix_time(ts: str):
    """'20251015-11:30:00.123' -> pandas Timestamp."""
    import pandas as pd
    return pd.to_datetime(ts, format="%Y%m%d-%H:%M:%S.%f")
