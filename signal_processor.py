"""
Intradiem GTM Signal Processor
--------------------------------
Reads mock Intradiem usage events, evaluates expansion and churn-risk signals
against rolling-window thresholds, applies a suppression window to prevent
duplicate alerts, and outputs routing decisions to stdout (or a CRM sink).

Usage:
    python signal_processor.py                  # runs against built-in sample data
    python signal_processor.py events.json      # runs against a JSON events file

Output format (one JSON object per line):
    {"account_id": "...", "signal": "...", "route_to": "...", "reason": "..."}
"""

import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "seat_utilization_pct":     85.0,   # trigger expansion if >= this
    "coaching_coverage_pct":    60.0,   # trigger expansion if <  this
    "rule_engine_active_pct":   35.0,   # trigger expansion if <  this
    "wfm_connected_days":       90,     # days WFM active before CRM check fires
    "automation_drop_pct":      20.0,   # churn risk if MoM drop >= this
    "automation_drop_months":   2,      # consecutive months of drop required
}

ROLLING_WINDOW_DAYS    = 30    # all utilization signals use a 30-day rolling average
SUPPRESSION_WINDOW_DAYS = 14   # same (account, signal) pair suppressed for 14 days
ROUTING = {
    "seat_utilization":     "AE",
    "coaching_coverage":    "CSM",
    "rule_engine_active":   "CSM",
    "crm_not_connected":    "AE",
    "automation_volume_drop": "CSM",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Event:
    """A single daily usage snapshot for one account."""
    def __init__(self, d: dict):
        self.account_id:          str   = d["account_id"]
        self.date:                date  = date.fromisoformat(d["date"])
        self.licensed_seats:      int   = d["licensed_seats"]
        self.active_seats:        int   = d["active_seats"]
        self.total_agents:        int   = d["total_agents"]
        self.agents_coached:      int   = d["agents_coached"]
        self.licensed_rules:      int   = d["licensed_rules"]
        self.active_rules:        int   = d["active_rules"]
        self.automation_events:   int   = d["automation_events"]
        self.wfm_connected_days:  int   = d["wfm_connected_days"]
        self.crm_connected:       bool  = d["crm_connected"]


class SuppressionLedger:
    """Tracks the last time a (account_id, signal) pair fired."""
    def __init__(self):
        self._last: dict[tuple, date] = {}

    def is_suppressed(self, account_id: str, signal: str, today: date) -> bool:
        key = (account_id, signal)
        last = self._last.get(key)
        if last is None:
            return False
        return (today - last).days < SUPPRESSION_WINDOW_DAYS

    def record(self, account_id: str, signal: str, today: date):
        self._last[(account_id, signal)] = today


# ---------------------------------------------------------------------------
# Signal evaluators
# ---------------------------------------------------------------------------

def rolling_avg(values: list[float]) -> float:
    window = values[-ROLLING_WINDOW_DAYS:] if len(values) >= ROLLING_WINDOW_DAYS else values
    return mean(window) if window else 0.0


def check_seat_utilization(events: list[Event]) -> Optional[str]:
    """Expansion signal: average seat utilization >= 85% over rolling 30 days."""
    util_series = [
        100.0 * e.active_seats / e.licensed_seats
        for e in events if e.licensed_seats > 0
    ]
    avg = rolling_avg(util_series)
    if avg >= THRESHOLDS["seat_utilization_pct"]:
        return (
            f"Seat utilization at {avg:.1f}% over the last {ROLLING_WINDOW_DAYS} days "
            f"(threshold: {THRESHOLDS['seat_utilization_pct']}%). "
            "Route to AE with expansion proposal."
        )
    return None


def check_coaching_coverage(events: list[Event]) -> Optional[str]:
    """Expansion signal: < 60% of agents receiving coaching over rolling 30 days."""
    coverage_series = [
        100.0 * e.agents_coached / e.total_agents
        for e in events if e.total_agents > 0
    ]
    avg = rolling_avg(coverage_series)
    if avg < THRESHOLDS["coaching_coverage_pct"]:
        return (
            f"Coaching coverage at {avg:.1f}% of agents over the last {ROLLING_WINDOW_DAYS} days "
            f"(threshold: < {THRESHOLDS['coaching_coverage_pct']}%). "
            "Route to CSM for deployment expansion conversation."
        )
    return None


def check_rule_engine(events: list[Event]) -> Optional[str]:
    """Expansion signal: < 35% of licensed rules active over rolling 30 days."""
    usage_series = [
        100.0 * e.active_rules / e.licensed_rules
        for e in events if e.licensed_rules > 0
    ]
    avg = rolling_avg(usage_series)
    if avg < THRESHOLDS["rule_engine_active_pct"]:
        return (
            f"Rule engine utilization at {avg:.1f}% of licensed rules "
            f"(threshold: < {THRESHOLDS['rule_engine_active_pct']}%). "
            "Route to CSM for QBR with use-case discovery agenda."
        )
    return None


def check_crm_not_connected(events: list[Event]) -> Optional[str]:
    """Expansion signal: WFM connected 90+ days, CRM never connected."""
    latest = events[-1]
    if (
        latest.wfm_connected_days >= THRESHOLDS["wfm_connected_days"]
        and not latest.crm_connected
    ):
        return (
            f"WFM integration active for {latest.wfm_connected_days} days "
            "with no CRM connection. "
            "Route to AE for back-office expansion pitch."
        )
    return None


def check_automation_drop(events: list[Event]) -> Optional[str]:
    """
    Churn-risk signal: automation volume down >= 20% month over month
    for two consecutive months.

    We approximate months by splitting the event history into 30-day buckets
    and comparing the most recent two buckets.
    """
    if len(events) < 60:
        return None  # not enough history

    def month_avg(bucket: list[Event]) -> float:
        return mean(e.automation_events for e in bucket) if bucket else 0.0

    recent   = events[-30:]
    previous = events[-60:-30]
    avg_recent   = month_avg(recent)
    avg_previous = month_avg(previous)

    if avg_previous == 0:
        return None

    drop_pct = 100.0 * (avg_previous - avg_recent) / avg_previous
    if drop_pct >= THRESHOLDS["automation_drop_pct"]:
        return (
            f"Automation event volume dropped {drop_pct:.1f}% month-over-month "
            f"(threshold: >= {THRESHOLDS['automation_drop_pct']}%). "
            "Route to CSM for stakeholder mapping call."
        )
    return None


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

SIGNAL_CHECKS = [
    ("seat_utilization",      check_seat_utilization),
    ("coaching_coverage",     check_coaching_coverage),
    ("rule_engine_active",    check_rule_engine),
    ("crm_not_connected",     check_crm_not_connected),
    ("automation_volume_drop", check_automation_drop),
]


def process_accounts(all_events: list[dict]) -> list[dict]:
    """
    Group events by account, sort chronologically, evaluate each signal,
    apply suppression, and return routing decisions.
    """
    by_account: dict[str, list[Event]] = defaultdict(list)
    for raw in all_events:
        e = Event(raw)
        by_account[e.account_id].append(e)

    for events in by_account.values():
        events.sort(key=lambda e: e.date)

    ledger = SuppressionLedger()
    decisions = []

    for account_id, events in by_account.items():
        today = events[-1].date
        for signal_name, check_fn in SIGNAL_CHECKS:
            if ledger.is_suppressed(account_id, signal_name, today):
                continue
            reason = check_fn(events)
            if reason:
                ledger.record(account_id, signal_name, today)
                decisions.append({
                    "account_id": account_id,
                    "signal":     signal_name,
                    "route_to":   ROUTING[signal_name],
                    "as_of":      today.isoformat(),
                    "reason":     reason,
                })

    return decisions


# ---------------------------------------------------------------------------
# Sample data generator
# ---------------------------------------------------------------------------

def generate_sample_events() -> list[dict]:
    """
    Produces 90 days of mock events for three accounts that each trigger
    a different signal combination.
    """
    base = date(2026, 3, 12)
    events = []

    for i in range(90):
        d = (base + timedelta(days=i)).isoformat()

        # Synchrony Financial: high seat utilization (expansion signal)
        events.append({
            "account_id": "synchrony-financial",
            "date": d,
            "licensed_seats": 500,
            "active_seats": 440 + (i % 10),     # ~88-90% utilization
            "total_agents": 1200,
            "agents_coached": 800,               # 67% coaching coverage (fine)
            "licensed_rules": 80,
            "active_rules": 50,                  # 62% rule usage (fine)
            "automation_events": 14000,
            "wfm_connected_days": 120,
            "crm_connected": True,
        })

        # Centene Corporation: low coaching coverage + CRM not connected (two signals)
        events.append({
            "account_id": "centene-corporation",
            "date": d,
            "licensed_seats": 800,
            "active_seats": 600,                 # 75% seat util (fine)
            "total_agents": 2000,
            "agents_coached": 900,               # 45% coaching coverage (below 60%)
            "licensed_rules": 120,
            "active_rules": 90,                  # 75% rule usage (fine)
            "automation_events": 22000,
            "wfm_connected_days": 95,            # >= 90 days
            "crm_connected": False,              # CRM not connected
        })

        # CareSource: automation volume dropping (churn risk)
        # First 60 days: healthy volume; last 30 days: sharp drop
        auto_events = 18000 if i < 60 else 12000  # 33% drop
        events.append({
            "account_id": "caresource",
            "date": d,
            "licensed_seats": 300,
            "active_seats": 210,                 # 70% seat util (fine)
            "total_agents": 600,
            "agents_coached": 390,               # 65% coaching coverage (fine)
            "licensed_rules": 60,
            "active_rules": 40,                  # 67% rule usage (fine)
            "automation_events": auto_events,
            "wfm_connected_days": 80,
            "crm_connected": True,
        })

    return events


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            raw_events = json.load(f)
    else:
        print("No events file provided — running against built-in sample data.\n")
        raw_events = generate_sample_events()

    decisions = process_accounts(raw_events)

    if not decisions:
        print("No signals fired.")
    else:
        print(f"{len(decisions)} routing decision(s):\n")
        for d in decisions:
            print(json.dumps(d, indent=2))
