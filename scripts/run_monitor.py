"""
Run every monitoring rule over the session and print the incident list (the "morning report").

Usage (from the repository root):
    python scripts/run_monitor.py            # prints incidents, writes reports/alerts.csv and reports/incidents.csv
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                   # noqa: E402
from bridgelab import monitor as M                    # noqa: E402

if __name__ == "__main__":
    alerts = M.run_all()
    incidents = M.group_incidents(alerts)
    os.makedirs("reports", exist_ok=True)
    alerts.drop(columns="sev_rank").to_csv("reports/alerts.csv", index=False)
    incidents.to_csv("reports/incidents.csv", index=False)
    pd.set_option("display.width", 220, "display.max_colwidth", 80, "display.max_rows", 200)
    print(f"{len(alerts)} alerts grouped into {len(incidents)} incidents\n")
    print(incidents[["start", "severity", "rule", "lp", "symbol", "alerts", "first_detail"]].fillna("").to_string(index=False))
    print("\nSaved reports/alerts.csv and reports/incidents.csv")
