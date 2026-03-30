"""GPS client that connects to gpsd and parses satellite/fix data.

Connects via the gps (gpsd) Python library and falls back to direct
NMEA parsing when the library is unavailable. Maintains a thread-safe
snapshot of the current GPS state.
"""

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Satellite:
    """Single satellite observation."""
    gnss: str = ""        # Constellation: GP, GL, GA, GB, QZ, SB, etc.
    prn: int = 0
    elevation: float = 0.0
    azimuth: float = 0.0
    snr: float = 0.0      # dBHz, 0 means not tracked
    used: bool = False

    @property
    def constellation_name(self) -> str:
        names = {
            "GP": "GPS", "GL": "GLONASS", "GA": "Galileo",
            "GB": "BeiDou", "BD": "BeiDou", "QZ": "QZSS",
            "SB": "SBAS", "IR": "IRNSS",
        }
        return names.get(self.gnss, self.gnss)


@dataclass
class GpsState:
    """Snapshot of current GPS state."""
    timestamp: str = ""          # UTC time string from GPS
    fix_mode: int = 0            # 0=unknown, 1=no fix, 2=2D, 3=3D
    fix_valid: bool = False      # True only when RMC status='A' AND mode>=2
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    speed: float | None = None   # knots
    track: float | None = None   # degrees true
    satellites_visible: int = 0
    satellites_used: int = 0
    used_prns: list[int] = field(default_factory=list)
    satellites: list[Satellite] = field(default_factory=list)
    time_offset: float | None = None  # seconds, from gpsd TPV
    hdop: float | None = None
    vdop: float | None = None
    pdop: float | None = None
    tdop: float | None = None
    devices: list[dict] = field(default_factory=list)
    last_update: float = 0.0     # monotonic timestamp of last data
    gpsd_running: bool = False
    rmc_status: str = ""         # 'A' or 'V' raw from GPRMC
    mag_var: float | None = None

    @property
    def fix_status_text(self) -> str:
        if not self.gpsd_running:
            return "GPSD NOT RUNNING"
        if self.fix_mode == 0:
            return "NO DATA"
        if self.fix_mode == 1 or not self.fix_valid:
            return "NO FIX"
        if self.fix_mode == 2:
            return "2D FIX"
        if self.fix_mode == 3:
            return "3D FIX"
        return "UNKNOWN"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "fix_mode": self.fix_mode,
            "fix_valid": self.fix_valid,
            "fix_status": self.fix_status_text,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "speed": self.speed,
            "track": self.track,
            "satellites_visible": self.satellites_visible,
            "satellites_used": self.satellites_used,
            "used_prns": self.used_prns,
            "satellites": [
                {
                    "gnss": s.gnss,
                    "constellation": s.constellation_name,
                    "prn": s.prn,
                    "elevation": s.elevation,
                    "azimuth": s.azimuth,
                    "snr": s.snr,
                    "used": s.used,
                }
                for s in self.satellites
            ],
            "time_offset": self.time_offset,
            "hdop": self.hdop,
            "vdop": self.vdop,
            "pdop": self.pdop,
            "tdop": self.tdop,
            "devices": self.devices,
            "gpsd_running": self.gpsd_running,
            "rmc_status": self.rmc_status,
            "mag_var": self.mag_var,
            "last_update": self.last_update,
            "age_seconds": round(time.monotonic() - self.last_update, 1) if self.last_update else None,
        }


# ---------------------------------------------------------------------------
# NMEA checksum
# ---------------------------------------------------------------------------

def _nmea_checksum_valid(sentence: str) -> bool:
    """Validate NMEA checksum. Returns True if valid or no checksum present."""
    if "*" not in sentence:
        return True
    try:
        body, cksum_hex = sentence.rsplit("*", 1)
        body = body.lstrip("$")
        computed = 0
        for ch in body:
            computed ^= ord(ch)
        return computed == int(cksum_hex[:2], 16)
    except (ValueError, IndexError):
        return False


# ---------------------------------------------------------------------------
# NMEA field helpers
# ---------------------------------------------------------------------------

def _safe_float(val: str) -> float | None:
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _safe_int(val: str) -> int | None:
    try:
        return int(val) if val else None
    except ValueError:
        return None


def _parse_lat(val: str, hemi: str) -> float | None:
    """Parse NMEA latitude (DDMM.MMMM) to decimal degrees."""
    if not val:
        return None
    try:
        deg = int(val[:2])
        minutes = float(val[2:])
        result = deg + minutes / 60.0
        if hemi == "S":
            result = -result
        return round(result, 7)
    except (ValueError, IndexError):
        return None


def _parse_lon(val: str, hemi: str) -> float | None:
    """Parse NMEA longitude (DDDMM.MMMM) to decimal degrees."""
    if not val:
        return None
    try:
        deg = int(val[:3])
        minutes = float(val[3:])
        result = deg + minutes / 60.0
        if hemi == "W":
            result = -result
        return round(result, 7)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Constellation identification from NMEA talker ID + PRN
# ---------------------------------------------------------------------------

def _identify_constellation(talker: str, prn: int) -> str:
    """Map NMEA talker ID and PRN to constellation code.

    NMEA 4.10+ uses talker IDs like GA (Galileo), GB (BeiDou), GL (GLONASS).
    Older NMEA uses GP for everything and encodes constellation in PRN ranges:
      1-32: GPS
      33-64: SBAS (WAAS/EGNOS/MSAS) - often displayed as PRN-87
      65-96: GLONASS
      193-200: QZSS
      201-264: BeiDou
      301-336: Galileo
    """
    if talker in ("GA", "GL", "GB", "BD", "QZ", "IR", "GI"):
        return talker
    # For GP talker, use PRN range
    if 1 <= prn <= 32:
        return "GP"
    if 33 <= prn <= 64:
        return "SB"
    if 65 <= prn <= 96:
        return "GL"
    if 193 <= prn <= 200:
        return "QZ"
    if 201 <= prn <= 264:
        return "GB"
    if 301 <= prn <= 336:
        return "GA"
    return "GP"


# ---------------------------------------------------------------------------
# NMEA sentence parsers
# ---------------------------------------------------------------------------

class NmeaParser:
    """Stateful NMEA parser that accumulates data into a GpsState."""

    def __init__(self):
        self._state = GpsState()
        self._lock = threading.Lock()
        # Accumulate GSV satellites across multiple messages
        self._gsv_buffer: dict[str, list[Satellite]] = {}  # talker -> sats
        self._gsv_expected: dict[str, int] = {}  # talker -> total messages
        self._gsv_received: dict[str, set[int]] = {}  # talker -> msg numbers seen
        self._used_prns: set[int] = set()

    @property
    def state(self) -> GpsState:
        with self._lock:
            # Return a copy-ish - the dict conversion is the main consumer
            return self._state

    def feed_nmea(self, line: str):
        """Parse a single NMEA sentence and update state."""
        line = line.strip()
        if not line.startswith("$"):
            return
        if not _nmea_checksum_valid(line):
            logger.debug("Bad NMEA checksum: %s", line)
            return

        # Strip checksum for field parsing
        body = line.split("*")[0].lstrip("$")
        parts = body.split(",")
        if len(parts) < 1:
            return

        sentence_id = parts[0]
        # Extract talker (first 2 chars) and sentence type (remaining)
        if len(sentence_id) < 3:
            return
        talker = sentence_id[:2]
        msg_type = sentence_id[2:]

        with self._lock:
            if msg_type == "RMC":
                self._parse_rmc(parts)
            elif msg_type == "GSA":
                self._parse_gsa(parts, talker)
            elif msg_type == "GSV":
                self._parse_gsv(parts, talker)
            elif msg_type == "GGA":
                self._parse_gga(parts)

            self._state.last_update = time.monotonic()
            self._state.gpsd_running = True

    def feed_json(self, data: dict):
        """Parse a gpsd JSON message and update state."""
        cls = data.get("class", "")
        with self._lock:
            if cls == "TPV":
                self._parse_tpv(data)
            elif cls == "SKY":
                self._parse_sky(data)
            elif cls == "DEVICES":
                self._state.devices = data.get("devices", [])
            elif cls == "PPS":
                pass  # PPS timing data
            self._state.last_update = time.monotonic()
            self._state.gpsd_running = True

    # -- NMEA parsers --

    def _parse_rmc(self, parts: list[str]):
        """Parse $xxRMC - Recommended Minimum."""
        # $GPRMC,HHMMSS.ss,status,lat,N/S,lon,E/W,speed,track,DDMMYY,magvar,E/W
        if len(parts) < 10:
            return
        time_str = parts[1]
        status = parts[2]  # A=active/valid, V=void/invalid
        lat = _parse_lat(parts[3], parts[4])
        lon = _parse_lon(parts[5], parts[6])
        speed = _safe_float(parts[7])
        track = _safe_float(parts[8])
        date_str = parts[9]

        self._state.rmc_status = status
        # Fix is valid ONLY when status is 'A'
        rmc_valid = (status == "A")

        if time_str and date_str:
            try:
                self._state.timestamp = (
                    f"20{date_str[4:6]}-{date_str[2:4]}-{date_str[0:2]}T"
                    f"{time_str[0:2]}:{time_str[2:4]}:{time_str[4:]}Z"
                )
            except (IndexError, ValueError):
                pass

        # Position is only meaningful if status=A
        if rmc_valid:
            self._state.latitude = lat
            self._state.longitude = lon
        # Even with V status, some receivers output last-known position
        # We store it but mark fix as invalid
        elif lat is not None and self._state.latitude is None:
            self._state.latitude = lat
            self._state.longitude = lon

        self._state.speed = speed
        self._state.track = track
        # Update fix_valid considering both RMC status and GSA mode
        self._state.fix_valid = rmc_valid and self._state.fix_mode >= 2

        # Magnetic variation
        if len(parts) >= 12:
            mag = _safe_float(parts[10])
            if mag is not None and parts[11] == "W":
                mag = -mag
            self._state.mag_var = mag

    def _parse_gsa(self, parts: list[str], talker: str):
        """Parse $xxGSA - DOP and active satellites."""
        # $GPGSA,mode1,mode2,sv1,sv2,...,sv12,PDOP,HDOP,VDOP
        if len(parts) < 18:
            return
        fix_mode = _safe_int(parts[2])
        if fix_mode is not None:
            self._state.fix_mode = fix_mode

        # Satellite PRNs in use (fields 3-14)
        used = set()
        for i in range(3, 15):
            if i < len(parts):
                prn = _safe_int(parts[i])
                if prn is not None and prn > 0:
                    used.add(prn)
        self._used_prns.update(used)

        # DOP values (last 3 fields before checksum)
        if len(parts) >= 18:
            self._state.pdop = _safe_float(parts[15])
            self._state.hdop = _safe_float(parts[16])
            self._state.vdop = _safe_float(parts[17])

        self._state.used_prns = sorted(self._used_prns)
        self._state.satellites_used = len(self._used_prns)
        # Update fix_valid
        self._state.fix_valid = (
            self._state.rmc_status == "A" and self._state.fix_mode >= 2
        )

    def _parse_gsv(self, parts: list[str], talker: str):
        """Parse $xxGSV - Satellites in view."""
        # $GPGSV,total_msgs,msg_num,total_sats,[prn,elev,azim,snr]{1-4}
        if len(parts) < 4:
            return
        total_msgs = _safe_int(parts[1])
        msg_num = _safe_int(parts[2])
        total_sats = _safe_int(parts[3])
        if total_msgs is None or msg_num is None:
            return

        # Initialize buffer for this talker if needed
        if talker not in self._gsv_expected or self._gsv_expected[talker] != total_msgs:
            self._gsv_buffer[talker] = []
            self._gsv_expected[talker] = total_msgs
            self._gsv_received[talker] = set()

        self._gsv_received[talker].add(msg_num)

        # Parse satellite groups (4 fields each, starting at index 4)
        idx = 4
        while idx + 3 < len(parts):
            prn = _safe_int(parts[idx])
            elev = _safe_float(parts[idx + 1])
            azim = _safe_float(parts[idx + 2])
            snr = _safe_float(parts[idx + 3])
            if prn is not None:
                constellation = _identify_constellation(talker, prn)
                sat = Satellite(
                    gnss=constellation,
                    prn=prn,
                    elevation=elev if elev is not None else 0.0,
                    azimuth=azim if azim is not None else 0.0,
                    snr=snr if snr is not None else 0.0,
                    used=(prn in self._used_prns),
                )
                self._gsv_buffer[talker].append(sat)
            idx += 4

        # When all messages for this talker received, commit to state
        if self._gsv_received[talker] == set(range(1, total_msgs + 1)):
            self._commit_satellites()

    def _commit_satellites(self):
        """Merge all GSV buffers into state satellites list."""
        all_sats = []
        for talker_sats in self._gsv_buffer.values():
            all_sats.extend(talker_sats)
        # Update used flag
        for s in all_sats:
            s.used = s.prn in self._used_prns
        self._state.satellites = all_sats
        self._state.satellites_visible = len(all_sats)

    def _parse_gga(self, parts: list[str]):
        """Parse $xxGGA - Fix information."""
        if len(parts) < 10:
            return
        # Field 7 = number of satellites used
        n_used = _safe_int(parts[7])
        if n_used is not None:
            self._state.satellites_used = n_used
        # Field 9 = altitude MSL
        alt = _safe_float(parts[9])
        if alt is not None:
            self._state.altitude = alt
        # Field 8 = HDOP
        hdop = _safe_float(parts[8])
        if hdop is not None:
            self._state.hdop = hdop

    # -- gpsd JSON parsers --

    def _parse_tpv(self, data: dict):
        """Parse gpsd TPV (Time-Position-Velocity) object."""
        mode = data.get("mode", 0)
        self._state.fix_mode = mode
        self._state.timestamp = data.get("time", self._state.timestamp)

        # Status field in newer gpsd versions
        status = data.get("status", -1)
        # mode >= 2 and status != 0 means valid fix
        self._state.fix_valid = mode >= 2 and status != 0

        if mode >= 2:
            self._state.latitude = data.get("lat", self._state.latitude)
            self._state.longitude = data.get("lon", self._state.longitude)
            self._state.speed = data.get("speed", self._state.speed)
            self._state.track = data.get("track", self._state.track)
        if mode >= 3:
            self._state.altitude = data.get("altMSL", data.get("alt", self._state.altitude))

        # Time offset from clock_sec/clock_nsec vs time
        if "clock_sec" in data and "time" in data:
            pass  # Complex offset calc
        # Direct offset if available
        # Note: gpsd does not directly provide time_offset in TPV
        # We calculate from PPS or use chrony data

    def _parse_sky(self, data: dict):
        """Parse gpsd SKY object - satellite info."""
        sats_json = data.get("satellites", [])
        all_sats = []
        used_prns = set()
        for s in sats_json:
            prn = s.get("PRN", 0)
            gnss = s.get("gnssid", -1)
            # Map gpsd gnssid to constellation code
            gnss_map = {0: "GP", 1: "SB", 2: "GA", 3: "GB", 4: "IR", 5: "QZ", 6: "GL"}
            constellation = gnss_map.get(gnss, _identify_constellation("GP", prn))
            used = s.get("used", False)
            sat = Satellite(
                gnss=constellation,
                prn=prn,
                elevation=s.get("el", 0.0),
                azimuth=s.get("az", 0.0),
                snr=s.get("ss", 0.0),
                used=used,
            )
            all_sats.append(sat)
            if used:
                used_prns.add(prn)

        self._state.satellites = all_sats
        self._state.satellites_visible = len(all_sats)
        self._state.satellites_used = len(used_prns)
        self._state.used_prns = sorted(used_prns)
        self._used_prns = used_prns

        # DOP values
        self._state.hdop = data.get("hdop", self._state.hdop)
        self._state.vdop = data.get("vdop", self._state.vdop)
        self._state.pdop = data.get("pdop", self._state.pdop)
        self._state.tdop = data.get("tdop", self._state.tdop)


# ---------------------------------------------------------------------------
# gpsd connection thread
# ---------------------------------------------------------------------------

class GpsPoller(threading.Thread):
    """Background thread that connects to gpsd and feeds data to NmeaParser."""

    def __init__(self, host: str = "localhost", port: int = 2947):
        super().__init__(daemon=True, name="gps-poller")
        self.host = host
        self.port = port
        self.parser = NmeaParser()
        self._stop_event = threading.Event()

    @property
    def state(self) -> GpsState:
        return self.parser.state

    def stop(self):
        self._stop_event.set()

    def run(self):
        """Main loop: try gps library first, fall back to gpspipe."""
        while not self._stop_event.is_set():
            try:
                self._run_gps_library()
            except Exception as e:
                logger.info("gps library unavailable (%s), trying gpspipe", e)
            if self._stop_event.is_set():
                break
            try:
                self._run_gpspipe()
            except Exception as e:
                logger.warning("gpspipe failed: %s", e)
            if self._stop_event.is_set():
                break
            # Mark gpsd as not running when we can't connect
            with self.parser._lock:
                self.parser._state.gpsd_running = False
            logger.info("Retrying gpsd connection in 5 seconds...")
            self._stop_event.wait(5)

    def _run_gps_library(self):
        """Connect using python3-gps library."""
        import gps as gpslib
        session = gpslib.gps(host=self.host, port=self.port, mode=gpslib.WATCH_ENABLE | gpslib.WATCH_NEWSTYLE)
        try:
            while not self._stop_event.is_set():
                report = session.next()
                if report:
                    self.parser.feed_json(report)
        except StopIteration:
            logger.info("gpsd connection closed")
        finally:
            session.close()

    def _run_gpspipe(self):
        """Fall back to gpspipe subprocess for NMEA + JSON data."""
        # Try JSON mode first
        proc = subprocess.Popen(
            ["gpspipe", "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            while not self._stop_event.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        self.parser.feed_json(data)
                    except json.JSONDecodeError:
                        pass
                elif line.startswith("$"):
                    self.parser.feed_nmea(line)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
