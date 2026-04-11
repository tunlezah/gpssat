"""Render the dashboard HTML with dummy data and capture a screenshot."""

import json
import os
import sys
from pathlib import Path

# Realistic dummy data matching the real NMEA test data from the project
DUMMY_DATA = {
    "gps": {
        "timestamp": "2026-03-28T21:34:11.00Z",
        "fix_mode": 3,
        "fix_valid": True,
        "fix_status": "3D FIX",
        "latitude": -35.209355,
        "longitude": 149.011442,
        "altitude": 582.4,
        "speed": 0.0,
        "track": 0.0,
        "satellites_visible": 22,
        "satellites_used": 10,
        "used_prns": [1, 2, 8, 10, 23, 24, 27, 28, 32, 196],
        "satellites": [
            {"gnss": "GP", "constellation": "GPS", "prn": 1, "elevation": 1, "azimuth": 229, "snr": 21, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 2, "elevation": 28, "azimuth": 224, "snr": 20, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 8, "elevation": 35, "azimuth": 264, "snr": 21, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 9, "elevation": -55, "azimuth": 316, "snr": 23, "used": False},
            {"gnss": "GP", "constellation": "GPS", "prn": 10, "elevation": 55, "azimuth": 146, "snr": 19, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 18, "elevation": 15, "azimuth": 50, "snr": 0, "used": False},
            {"gnss": "GP", "constellation": "GPS", "prn": 20, "elevation": -37, "azimuth": 138, "snr": 23, "used": False},
            {"gnss": "GP", "constellation": "GPS", "prn": 23, "elevation": 26, "azimuth": 111, "snr": 22, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 24, "elevation": 11, "azimuth": 136, "snr": 22, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 26, "elevation": -12, "azimuth": 351, "snr": 23, "used": False},
            {"gnss": "GP", "constellation": "GPS", "prn": 27, "elevation": 39, "azimuth": 304, "snr": 25, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 28, "elevation": 16, "azimuth": 16, "snr": 16, "used": True},
            {"gnss": "GP", "constellation": "GPS", "prn": 29, "elevation": -29, "azimuth": 33, "snr": 23, "used": False},
            {"gnss": "GP", "constellation": "GPS", "prn": 30, "elevation": -49, "azimuth": 217, "snr": 23, "used": False},
            {"gnss": "GP", "constellation": "GPS", "prn": 31, "elevation": -1, "azimuth": 358, "snr": 23, "used": False},
            {"gnss": "GP", "constellation": "GPS", "prn": 32, "elevation": 83, "azimuth": 338, "snr": 30, "used": True},
            {"gnss": "SB", "constellation": "SBAS", "prn": 42, "elevation": 48, "azimuth": 345, "snr": 0, "used": False},
            {"gnss": "SB", "constellation": "SBAS", "prn": 48, "elevation": 1, "azimuth": 83, "snr": 0, "used": False},
            {"gnss": "SB", "constellation": "SBAS", "prn": 50, "elevation": 49, "azimuth": 353, "snr": 0, "used": False},
            {"gnss": "QZ", "constellation": "QZSS", "prn": 194, "elevation": 16, "azimuth": 350, "snr": 0, "used": False},
            {"gnss": "QZ", "constellation": "QZSS", "prn": 195, "elevation": 81, "azimuth": 34, "snr": 0, "used": False},
            {"gnss": "QZ", "constellation": "QZSS", "prn": 196, "elevation": 50, "azimuth": 312, "snr": 23, "used": True},
        ],
        "time_offset": None,
        "hdop": 1.2,
        "vdop": 1.8,
        "pdop": 2.1,
        "tdop": None,
        "devices": [],
        "gpsd_running": True,
        "rmc_status": "A",
        "mag_var": 12.3,
        "last_update": 0,
        "age_seconds": 1.2,
    },
    "chrony": {
        "available": True,
        "tracking": {
            "ref_id": "50505300 (PPS)",
            "ref_name": "PPS",
            "stratum": 1,
            "ref_time": "Sat Mar 28 21:34:10 2026",
            "system_time": "0.000000312 seconds slow of NTP time",
            "system_time_offset": 0.000000312,
            "last_offset": "+0.000000142 seconds",
            "last_offset_seconds": 0.000000142,
            "rms_offset": "0.000000487 seconds",
            "rms_offset_seconds": 0.000000487,
            "frequency": "12.354 ppm slow",
            "frequency_ppm": -12.354,
            "residual_freq": "+0.001 ppm",
            "residual_freq_ppm": 0.001,
            "skew": "0.012 ppm",
            "skew_ppm": 0.012,
            "root_delay": "0.000000001 seconds",
            "root_delay_seconds": 0.000000001,
            "root_dispersion": "0.000015432 seconds",
            "root_dispersion_seconds": 0.000015432,
            "update_interval": "16.0 seconds",
            "update_interval_seconds": 16.0,
            "leap_status": "Normal",
        },
        "sources": [
            {"mode": "refclock", "state": "not_combined", "state_char": "-", "name": "GPS", "stratum": 0, "poll": 4, "reach": "377", "last_rx": "12s", "last_sample": "+877us[+1298us] +/- 200ms"},
            {"mode": "refclock", "state": "selected", "state_char": "*", "name": "PPS", "stratum": 0, "poll": 4, "reach": "377", "last_rx": "12s", "last_sample": "+142ns[+312ns] +/- 1us"},
            {"mode": "server", "state": "combined", "state_char": "+", "name": "203.19.96.1", "stratum": 1, "poll": 6, "reach": "377", "last_rx": "42s", "last_sample": "-1.2ms[-1.2ms] +/- 15ms"},
            {"mode": "server", "state": "combined", "state_char": "+", "name": "162.159.200.1", "stratum": 3, "poll": 6, "reach": "377", "last_rx": "38s", "last_sample": "+2.1ms[+2.1ms] +/- 22ms"},
        ],
        "sourcestats": [],
    },
}


def main():
    project_root = Path(__file__).parent.parent
    template_path = project_root / "gpssat" / "templates" / "index.html"
    output_path = project_root / "screenshots" / "dashboard.jpg"

    html_content = template_path.read_text()

    # Replace the polling script with one that just renders the dummy data immediately
    dummy_json = json.dumps(DUMMY_DATA)
    injection_script = f"""
<script>
// Override the poll function to use dummy data instead of fetching
const DUMMY = {dummy_json};
// Wait for DOM to be ready, then render
document.addEventListener('DOMContentLoaded', function() {{
    updateUI(DUMMY);
}});
// Also call immediately in case DOMContentLoaded already fired
if (document.readyState !== 'loading') {{
    setTimeout(function() {{ updateUI(DUMMY); }}, 100);
}}
</script>
"""

    # Inject the dummy data script right before </body>
    modified_html = html_content.replace("</body>", injection_script + "</body>")

    # Also remove the original polling to avoid fetch errors
    modified_html = modified_html.replace("poll();\npollInterval = setInterval(poll, 2000);",
                                          "// polling disabled for screenshot\nupdateUI(DUMMY);")

    # Write temp HTML file
    temp_html = project_root / "screenshots" / "_temp_dashboard.html"
    temp_html.write_text(modified_html)

    # Use Playwright to capture screenshot
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(f"file://{temp_html.resolve()}")
        # Wait for rendering
        page.wait_for_timeout(1500)
        page.screenshot(path=str(output_path), type="jpeg", quality=90, full_page=True)
        browser.close()

    # Clean up temp file
    temp_html.unlink()

    print(f"Screenshot saved to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
