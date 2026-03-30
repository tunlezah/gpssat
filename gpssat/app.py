"""Flask web application for GPS-disciplined NTP server monitoring."""

import logging
import os
import signal
import sys

from flask import Flask, jsonify, render_template

from gpssat.gps_client import GpsPoller
from gpssat.chrony_client import get_full_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global GPS poller
_gps_poller: GpsPoller | None = None


def get_gps_poller() -> GpsPoller:
    global _gps_poller
    if _gps_poller is None:
        host = os.environ.get("GPSD_HOST", "localhost")
        port = int(os.environ.get("GPSD_PORT", "2947"))
        _gps_poller = GpsPoller(host=host, port=port)
        _gps_poller.start()
        logger.info("GPS poller started (gpsd %s:%d)", host, port)
    return _gps_poller


@app.route("/")
def index():
    """Serve the main dashboard page."""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Combined GPS + Chrony status endpoint."""
    poller = get_gps_poller()
    gps_state = poller.state.to_dict()
    chrony_status = get_full_status()
    return jsonify({
        "gps": gps_state,
        "chrony": chrony_status,
    })


@app.route("/api/gps")
def api_gps():
    """GPS-only status endpoint."""
    poller = get_gps_poller()
    return jsonify(poller.state.to_dict())


@app.route("/api/chrony")
def api_chrony():
    """Chrony-only status endpoint."""
    return jsonify(get_full_status())


def main():
    host = os.environ.get("GPSSAT_HOST", "0.0.0.0")
    port = int(os.environ.get("GPSSAT_PORT", "5000"))
    debug = os.environ.get("GPSSAT_DEBUG", "0") == "1"

    # Start GPS poller
    get_gps_poller()

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        if _gps_poller:
            _gps_poller.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("Starting GPS NTP Monitor on %s:%d", host, port)
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
