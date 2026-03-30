"""Chrony status reader.

Parses output from chronyc commands to provide NTP disciplining status.
"""

import logging
import re
import subprocess

logger = logging.getLogger(__name__)


def _run_chronyc(*args: str) -> str | None:
    """Run a chronyc command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["chronyc", "-n", *args],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
        logger.debug("chronyc %s failed: %s", args, result.stderr)
    except FileNotFoundError:
        logger.warning("chronyc not found")
    except subprocess.TimeoutExpired:
        logger.warning("chronyc timed out")
    except Exception as e:
        logger.warning("chronyc error: %s", e)
    return None


def get_tracking() -> dict | None:
    """Parse 'chronyc tracking' output into a dict."""
    raw = _run_chronyc("tracking")
    if raw is None:
        return None

    result = {}
    for line in raw.strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if key == "Reference ID":
            # "47505300 (GPS)" -> extract name
            m = re.search(r"\((\S+)\)", value)
            result["ref_id"] = value
            result["ref_name"] = m.group(1) if m else value
        elif key == "Stratum":
            result["stratum"] = _try_int(value)
        elif key == "Ref time (UTC)":
            result["ref_time"] = value
        elif key == "System time":
            result["system_time"] = value
            result["system_time_offset"] = _extract_seconds(value)
        elif key == "Last offset":
            result["last_offset"] = value
            result["last_offset_seconds"] = _extract_seconds(value)
        elif key == "RMS offset":
            result["rms_offset"] = value
            result["rms_offset_seconds"] = _extract_seconds(value)
        elif key == "Frequency":
            result["frequency"] = value
            result["frequency_ppm"] = _extract_ppm(value)
        elif key == "Residual freq":
            result["residual_freq"] = value
            result["residual_freq_ppm"] = _extract_ppm(value)
        elif key == "Skew":
            result["skew"] = value
            result["skew_ppm"] = _extract_ppm(value)
        elif key == "Root delay":
            result["root_delay"] = value
            result["root_delay_seconds"] = _extract_seconds(value)
        elif key == "Root dispersion":
            result["root_dispersion"] = value
            result["root_dispersion_seconds"] = _extract_seconds(value)
        elif key == "Update interval":
            result["update_interval"] = value
            result["update_interval_seconds"] = _extract_seconds(value)
        elif key == "Leap status":
            result["leap_status"] = value

    return result


def get_sources() -> list[dict]:
    """Parse 'chronyc sources -v' output into a list of source dicts."""
    raw = _run_chronyc("sources", "-v")
    if raw is None:
        return []

    sources = []
    # Find the data lines (after the === separator)
    in_data = False
    for line in raw.strip().splitlines():
        if line.startswith("=="):
            in_data = True
            continue
        if not in_data:
            continue
        if not line or len(line) < 3:
            continue

        # Format: MS Name/IP  Stratum Poll Reach LastRx Last sample
        # Example: #* GPS       0   4     0  123m   +877us[+1298us] +/-  200ms
        mode_char = line[0]  # ^ = server, # = refclock, = = peer
        state_char = line[1]  # * = selected, + = combined, - = not combined, etc.

        mode_map = {"^": "server", "#": "refclock", "=": "peer"}
        state_map = {
            "*": "selected", "+": "combined", "-": "not_combined",
            "x": "maybe_error", "~": "too_variable", "?": "unusable",
        }

        # Parse the rest of the fields
        rest = line[2:].strip()
        parts = rest.split()
        if len(parts) < 5:
            continue

        name = parts[0]
        stratum = _try_int(parts[1])
        poll = _try_int(parts[2])
        reach = parts[3]
        last_rx = parts[4]

        # Last sample is everything remaining (contains brackets and +/-)
        sample_str = " ".join(parts[5:]) if len(parts) > 5 else ""

        sources.append({
            "mode": mode_map.get(mode_char, mode_char),
            "state": state_map.get(state_char, state_char),
            "state_char": state_char,
            "name": name,
            "stratum": stratum,
            "poll": poll,
            "reach": reach,
            "last_rx": last_rx,
            "last_sample": sample_str,
        })

    return sources


def get_sourcestats() -> list[dict]:
    """Parse 'chronyc sourcestats' output."""
    raw = _run_chronyc("sourcestats", "-v")
    if raw is None:
        return []

    stats = []
    in_data = False
    for line in raw.strip().splitlines():
        if line.startswith("=="):
            in_data = True
            continue
        if not in_data:
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        stats.append({
            "name": parts[0],
            "np": _try_int(parts[1]),
            "nr": _try_int(parts[2]),
            "span": parts[3],
            "frequency": parts[4],
            "freq_skew": parts[5],
            "offset": parts[6],
            "std_dev": parts[7] if len(parts) > 7 else "",
        })

    return stats


def get_full_status() -> dict:
    """Get complete chrony status."""
    tracking = get_tracking()
    sources = get_sources()
    sourcestats = get_sourcestats()
    return {
        "available": tracking is not None,
        "tracking": tracking,
        "sources": sources,
        "sourcestats": sourcestats,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _try_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _extract_seconds(value: str) -> float | None:
    """Extract a seconds value from chronyc output like '0.000420814 seconds'."""
    m = re.search(r"([+-]?\d+\.?\d*)\s*seconds?", value)
    if m:
        return float(m.group(1))
    return None


def _extract_ppm(value: str) -> float | None:
    """Extract ppm value from chronyc output like '37.652 ppm slow'."""
    m = re.search(r"([+-]?\d+\.?\d*)\s*ppm", value)
    if m:
        val = float(m.group(1))
        if "slow" in value:
            val = -val
        return val
    return None
