# Intradiem GTM Signal Processor

A PLG signal processor for Intradiem's existing install base. Takes daily usage events as input, evaluates six expansion and churn-risk signals against rolling-window thresholds, suppresses duplicates, and outputs routing decisions to AE or CSM.

Built as part of a GTM engineering proof-of-work project. The architecture is designed to sit between Intradiem's usage event stream and a CRM (Salesforce + Gainsight), but runs fully standalone against sample data out of the box.

---

## What it does

Evaluates six signals per account on each run:

**Expansion signals (route to AE or CSM):**
- Seat utilization at 85%+ over a 30-day rolling window
- Coaching coverage below 60% of agents over 30 days
- Rule engine utilization below 35% of licensed rules
- WFM connected 90+ days with no CRM integration

**Churn risk signals (route to CSM):**
- Automation event volume down 20%+ month-over-month

Each signal fires at most once per 14-day suppression window per account.

---

## Usage

```bash
# Run against built-in sample data (3 accounts, 90 days of events)
python signal_processor.py

# Run against your own events file
python signal_processor.py events.json
```

No dependencies beyond the Python standard library.

---

## Sample output

Running against the built-in sample data produces 4 routing decisions:

```json
{
  "account_id": "synchrony-financial",
  "signal": "seat_utilization",
  "route_to": "AE",
  "as_of": "2026-06-09",
  "reason": "Seat utilization at 88.5% over the last 30 days (threshold: 85.0%). Route to AE with expansion proposal."
}
{
  "account_id": "centene-corporation",
  "signal": "coaching_coverage",
  "route_to": "CSM",
  "as_of": "2026-06-09",
  "reason": "Coaching coverage at 45.0% of agents over the last 30 days (threshold: < 60.0%). Route to CSM for deployment expansion conversation."
}
{
  "account_id": "centene-corporation",
  "signal": "crm_not_connected",
  "route_to": "AE",
  "as_of": "2026-06-09",
  "reason": "WFM integration active for 95 days with no CRM connection. Route to AE for back-office expansion pitch."
}
{
  "account_id": "caresource",
  "signal": "automation_volume_drop",
  "route_to": "CSM",
  "as_of": "2026-06-09",
  "reason": "Automation event volume dropped 33.3% month-over-month (threshold: >= 20.0%). Route to CSM for stakeholder mapping call."
}
```

---

## Event schema

Each event is a daily usage snapshot per account:

```json
{
  "account_id": "string",
  "date": "YYYY-MM-DD",
  "licensed_seats": 500,
  "active_seats": 440,
  "total_agents": 1200,
  "agents_coached": 800,
  "licensed_rules": 80,
  "active_rules": 50,
  "automation_events": 14000,
  "wfm_connected_days": 120,
  "crm_connected": true
}
```

Pass a JSON array of these objects as `events.json` to run against real data.

---

## Configuration

Thresholds and routing targets are in the `THRESHOLDS` and `ROUTING` dicts at the top of `signal_processor.py`. Adjust to match Intradiem's actual account benchmarks once baseline data is available.

```python
THRESHOLDS = {
    "seat_utilization_pct":   85.0,
    "coaching_coverage_pct":  60.0,
    "rule_engine_active_pct": 35.0,
    "wfm_connected_days":     90,
    "automation_drop_pct":    20.0,
}

ROLLING_WINDOW_DAYS     = 30
SUPPRESSION_WINDOW_DAYS = 14
```

---

## Architecture context

This script is the signal processor layer in a larger GTM Signal Engine:

```
Intradiem Platform
  (usage events, coaching logs, schedule adherence, rule activity)
       |
       v
Signal Processor  <-- this script
  (threshold checks, rolling windows, suppression ledger)
       |
       v
Routing Rules
  (expansion to AE, health risk to CSM, churn to both)
       |
       v
CRM / CS Platform
  (Salesforce task, Gainsight health score, Slack alert)
       |
       v
Rep Action
```

Day one in a real deployment: daily CSV export from Intradiem's reporting module, run on a cron job, output piped to a Salesforce Flow via the API. The full live webhook architecture is the 90-day state.
