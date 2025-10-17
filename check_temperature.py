import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import List

import requests
from dotenv import load_dotenv

THRESHOLD = 90.0
WINDOW_SIZE = 3  # Look over the last 15 minutes, every 5 minutes
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "check_temperature_history.json")

load_dotenv()


def run_sensors() -> str:
    """Execute `sensors` and return stdout as text. Raises on failure."""
    try:
        result = subprocess.run(
            ["sensors"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
    except FileNotFoundError:
        raise RuntimeError(
            "`sensors` command not found. Install lm-sensors and ensure it is on PATH."
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"`sensors` command failed with code {exc.returncode}: {exc.stderr}")


def parse_cpu_temperatures(sensors_output: str) -> List[float]:
    """
    Extract only the temperature from the 'Package id 0:' line, if present.
    Returns a single-element list with that temperature, or an empty list
    if not found.
    """
    for line in sensors_output.splitlines():
        if "Package id 0:" in line:
            match = re.search(r"\+(\d+(?:\.\d+)?)°C", line)
            if match:
                try:
                    return [float(match.group(1))]
                except ValueError:
                    return []
            return []
    return []


def load_history(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def save_history(path: str, history: List[dict]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as exc:
        # Non-fatal: print to stderr so cron logs capture it
        print(f"Failed to write history file: {exc}", file=sys.stderr)


def send_notification(message: str, title: str, priority: str = "default", tags: str = "") -> None:
    """Generic ntfy.sh notification sender"""
    try:
        topic = os.getenv("NTFY_CPU_TEMPERATURES_TOPIC")
        if not topic:
            print("Missing NTFY_CPU_TEMPERATURES_TOPIC in environment.", file=sys.stderr)
            return

        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message,
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except Exception as e:
        print(f"Failed to send notification: {e}", file=sys.stderr)


def create_alert(avg_temp: float, history: List[dict]) -> None:
    """Send temperature alert"""
    send_notification(
        f"🔥 Server overheating!\nAverage: {avg_temp:.1f}°C over last {len(history)} checks",
        "Home Server Temperature Alert",
        priority="urgent",
        tags="warning",
    )


def check_and_alert(history: List[dict], window_size: int, threshold: float) -> bool:
    """Return True and trigger alert if avg temperature over the last window exceeds threshold."""
    if len(history) < window_size:
        return False

    window = history[-window_size:]
    temps = [e["temperature"] for e in window if isinstance(e.get("temperature"), (int, float))]

    if len(temps) < window_size:
        # Incomplete temperature data for the full window; skip alerting
        return False

    avg_temp = sum(temps) / len(temps)
    if avg_temp > threshold:
        create_alert(avg_temp, window)
        return True
    return False


def main() -> int:
    try:
        output = run_sensors()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        send_notification(
            f"Temperature monitoring failed:\n{exc}",
            "Monitoring Script Error",
            priority="high",
            tags="x",
        )
        return 1

    temps = parse_cpu_temperatures(output)
    if not temps:
        msg = "No temperatures found in sensors output."
        print(msg, file=sys.stderr)
        send_notification(msg, "Monitoring Script Error", priority="high", tags="x")
        return 2

    current_temp = temps[0]
    over = current_temp > THRESHOLD

    history = load_history(HISTORY_FILE)
    history.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature": current_temp,
            "over_threshold": over,
            "threshold": THRESHOLD,
        }
    )
    # Keep only last WINDOW_SIZE entries (we only need that many for alerting)
    if len(history) > WINDOW_SIZE:
        history = history[-WINDOW_SIZE:]

    save_history(HISTORY_FILE, history)

    alerted = check_and_alert(history, WINDOW_SIZE, THRESHOLD)

    # Human-readable output for interactive or logs
    status = "OVER" if over else "OK"
    print(f"CPU temp: {current_temp:.1f}°C | Threshold: {THRESHOLD:.1f}°C | Status: {status}")
    if alerted:
        print(f"Alert condition met over last {WINDOW_SIZE} intervals.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
