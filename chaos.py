"""Traffic generator and (from build step 2) fault injector for the target service.

Usage:  python chaos.py load [seconds] [orders_per_second]
"""

import os
import random
import sys
import time

import requests

SERVICE_URL = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")


def load(seconds: float = 60, rate: float = 5) -> None:
    """Send a steady stream of realistic orders so the metrics keep moving."""
    end = time.time() + seconds
    sent = failed = 0
    while time.time() < end:
        order = {"amount": round(random.uniform(10, 500), 2), "currency": random.choice(["SAR", "SAR", "USD"])}
        try:
            requests.post(f"{SERVICE_URL}/orders", json=order, timeout=3).raise_for_status()
            sent += 1
        except requests.RequestException:
            failed += 1
        time.sleep(1 / rate)
    print(f"load done: {sent} orders sent, {failed} failed")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "load":
        sys.exit("usage: python chaos.py load [seconds] [orders_per_second]")
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60
    per_sec = float(sys.argv[3]) if len(sys.argv) > 3 else 5
    load(secs, per_sec)
