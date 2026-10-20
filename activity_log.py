"""Bounded diagnostic history, separate from financial bookkeeping and execution."""
import json
import math
import re
import time
from datetime import datetime, timezone

import ai_decisions


def record(runtime, uid, snapshot, mode, code, message):
    # Logging must never turn a failed observation into a financial retry or
    # prevent an otherwise permitted risk exit.
    try:
        with runtime.ledger.db() as db:
            inserted = db.execute("INSERT OR IGNORE INTO custody_activity_events VALUES(?,?,?,?,?,?)",
                (uid, snapshot["id"], mode, code, str(message)[:600], int(time.time()))).rowcount
            if inserted:
                db.execute("""DELETE FROM custody_activity_events WHERE uid=? AND rowid NOT IN
                    (SELECT rowid FROM custody_activity_events WHERE uid=?
                     ORDER BY created_at DESC,rowid DESC LIMIT 200)""", (uid, uid))
    except Exception:
        # Do not print provider messages, request bodies, credentials or SQL values.
        try:
            runtime.log("Activity history write unavailable.")
        except Exception:
            pass


def record_event(runtime, uid, event_id, code, message):
    """Record account-scoped system activity without coupling it to trade execution."""
    try:
        with runtime.ledger.db() as db:
            inserted = db.execute("INSERT OR IGNORE INTO custody_activity_events VALUES(?,?,?,?,?,?)",
                (uid, event_id, "system", code, str(message)[:600], int(time.time()))).rowcount
            if inserted:
                db.execute("""DELETE FROM custody_activity_events WHERE uid=? AND rowid NOT IN
                    (SELECT rowid FROM custody_activity_events WHERE uid=?
                     ORDER BY created_at DESC,rowid DESC LIMIT 200)""", (uid, uid))
    except Exception:
        try:
            runtime.log("Activity history write unavailable.")
        except Exception:
            pass


def label(runtime, symbol):
    if not symbol:
        return ""
    info = getattr(runtime, "watchlist", {}).get(symbol, {})
    return str(info.get("label", symbol))[:60]


def observe(runtime, uid, snapshot, mode):
    decision = snapshot["decisions"][mode]
    action = decision["action"]
    asset = label(runtime, decision["symbol"])
    stamp = datetime.fromtimestamp(snapshot["created_at"], timezone.utc).strftime("%H:%M:%S UTC")
    message = f"{mode.upper()} AI {action} {asset} — analysis {stamp}. {decision['reason']}"
    if action != "HOLD":
        message += " Decision only; not a confirmed trade."
    record(runtime, uid, snapshot, mode, "decision", message)


STATES = {
    "RESERVED": "funds reserved; no broadcast yet",
    "PREPARED": "signed and saved; network outcome not yet confirmed; do not resend",
    "SUBMITTED": "submitted; waiting for finalized network confirmation",
    "UNKNOWN": "network outcome unknown; do not resend",
    "COMPLETED": "finalized on Solana",
    "FAILED": "failed; see the recorded failure reason below",
}

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|token|secret|password|authorization)\b\s*([=:])\s*[^\s,;]+")
_STANDALONE_CREDENTIAL = re.compile(
    r"(?i)^(?:secret|token|api[_-]?key|password|credential)[_:= -][a-z0-9._~+/-]{4,}$")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


def safe_failure_reason(value):
    """Keep useful provider/chain errors while removing credentials and URLs."""
    text = " ".join(str(value or "").split())
    if _STANDALONE_CREDENTIAL.fullmatch(text):
        return "[Sensitive error text redacted]"
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[redacted]", text)
    text = _URL.sub("[URL redacted]", text)
    return text[:300]


def report(runtime, uid):
    """Read-only diagnostics. Never invokes AI, quotes, signing or sendTransaction."""
    settings = runtime.ledger.settings(uid)
    latest = runtime.ledger.ai_latest()
    config = ai_decisions.configuration()
    active = settings["trading"] or settings["sniper_trading"]
    pending = runtime.pending()
    status = ("Core: " + ("ON" if settings["trading"] else "OFF") +
              " · Meme Sniper: " + ("ON" if settings["sniper_trading"] else "OFF") + ". ")
    if not active:
        status += "Automatic trading is paused."
    elif not config["configured"]:
        status += "AI is not configured; no new AI orders."
    elif getattr(runtime, "ai_task", None) is not None:
        status += "AI analysis is running or awaiting collection."
    elif latest:
        remaining = max(0, math.ceil(latest["created_at"] + config["interval_seconds"] - time.time()))
        status += (f"Next AI analysis due in about {remaining} seconds." if remaining else
                   "Next AI analysis is due; waiting for the worker.")
    else:
        status += "Waiting for the first AI analysis and market data."
    if active and getattr(runtime, "ai_activity_error", ""):
        status += " " + runtime.ai_activity_error
    if settings["close_requested"]:
        remaining = len(runtime.ledger.positions(uid, "OPEN"))
        status += f" Manual close queue active: {remaining} open position(s) remaining."
        if any(op["uid"] == uid for op in pending):
            status += " Waiting for your existing transaction to resolve; do not resubmit."
        elif pending:
            status += " Waiting for another shared-wallet operation to resolve."
        retry = getattr(runtime, "_close_retry_after", {}).get(uid)
        if retry:
            seconds = max(0, math.ceil(retry["until"] - time.monotonic()))
            if seconds:
                status += f" Next close attempt no sooner than {seconds} seconds."
    elif pending:
        status += "A shared-vault operation is awaiting resolution; new sends may wait."
    with runtime.ledger.db() as db:
        events = [dict(r) for r in db.execute(
            """SELECT analysis_id,created_at,message,mode,code FROM custody_activity_events WHERE uid=?
               ORDER BY created_at DESC,rowid DESC LIMIT 50""", (uid,))]
        operations = [dict(r) for r in db.execute(
            """SELECT kind,state,created_at,signature,failure_reason FROM custody_operations WHERE uid=?
               ORDER BY created_at DESC,rowid DESC LIMIT 15""", (uid,))]
        attempted = {r[0] for r in db.execute(
            "SELECT mode FROM custody_ai_attempts WHERE uid=? AND analysis_id=?",
            (uid, latest["id"] if latest else ""))}
    entries = [{**event, "category": "SYSTEM" if event["mode"] == "system" else "AI"}
               for event in events]
    # A freshly upgraded or paused account can have a model result without a
    # recorded worker event. Display it explicitly as a result, never an execution.
    if latest:
        decisions = json.loads(latest["decisions"])["decisions"]
        for mode in ("main", "sniper"):
            if not any(e["mode"] == mode and e["code"] == "decision" and
                       e["analysis_id"] == latest["id"] for e in events):
                decision = decisions[mode]
                entries.append({"created_at": latest["created_at"], "category": "AI",
                    "message": f"{mode.upper()} AI {decision['action']} {label(runtime, decision['symbol'])}"
                    f" — {decision['reason']} Decision only; not proof of execution."})
            if mode in attempted:
                status += f" {mode.upper()}: this analysis was already claimed; it will not be resent."
    for op in operations:
        message = f"{op['kind']}: {op['state']} — {STATES.get(op['state'], 'check operation status')}"
        if op["state"] == "FAILED":
            reason = safe_failure_reason(op.get("failure_reason"))
            message += (". Cause: " + reason if reason else
                        ". Cause unavailable: this operation predates failure-detail logging.")
        entries.append({"created_at": op["created_at"], "category": "TRANSACTION",
                        "message": message, "signature": op["signature"]})
    entries.sort(key=lambda e: e["created_at"], reverse=True)
    entries = entries[:65]
    if not entries:
        entries = [{"created_at": int(time.time()), "category": "STATUS",
                    "message": "No financial transactions or AI decisions recorded yet."}]
    logs = [status] + [
        datetime.fromtimestamp(e["created_at"], timezone.utc).isoformat() +
        " [" + e["category"] + "] " + e["message"] +
        ("\nTx: " + e["signature"] if e.get("signature") else "") for e in entries]
    return {"activity_status": status, "entries": entries, "logs": logs,
            "activity_version": "decision-history-v2"}
