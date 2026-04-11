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
    # Extended u-blox / gpsd fields
    sig_id: str = ""      # Signal ID: L1CA, L2CL, L5I, E1, E5a, B1, B2 etc.
    health: int = -1      # 0=unknown, 1=healthy, 2=unhealthy (gpsd health)
    quality: int = -1     # Signal quality 0-7 (gpsd qual)

    @property
    def constellation_name(self) -> str:
        names = {
            "GP": "GPS", "GL": "GLONASS", "GA": "Galileo",
            "GB": "BeiDou", "BD": "BeiDou", "QZ": "QZSS",
            "SB": "SBAS", "IR": "IRNSS",
        }
        return names.get(self.gnss, self.gnss)

    @property
    def health_text(self) -> str:
        return {0: "Unknown", 1: "OK", 2: "Unhealthy"}.get(self.health, "")

    @property
    def quality_text(self) -> str:
        # gpsd signal quality indicator meanings
        return {
            0: "No signal", 1: "Searching", 2: "Acquired",
            3: "Unusable", 4: "Code locked", 5: "Code+Carrier",
            6: "Code+Carrier", 7: "Code+Carrier",
        }.get(self.quality, "")


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
    # -- Extended u-blox / gpsd fields --
    # Additional DOPs
    gdop: float | None = None    # Geometric DOP
    xdop: float | None = None    # Longitudinal DOP
    ydop: float | None = None    # Latitudinal DOP
    # Accuracy estimates (from gpsd TPV, sourced from UBX-NAV-PVT)
    eph: float | None = None     # Horizontal position error (m)
    epv: float | None = None     # Vertical position error (m)
    eps: float | None = None     # Speed error (m/s)
    ept: float | None = None     # Time error (s)
    epd: float | None = None     # Direction/track error (deg)
    epc: float | None = None     # Climb/vertical speed error (m/s)
    epx: float | None = None     # Longitude error (m)
    epy: float | None = None     # Latitude error (m)
    # ECEF coordinates (from UBX-NAV-PVT via gpsd)
    ecef_x: float | None = None
    ecef_y: float | None = None
    ecef_z: float | None = None
    ecef_pacc: float | None = None  # ECEF position accuracy (m)
    # Velocity components (m/s)
    vel_north: float | None = None
    vel_east: float | None = None
    vel_down: float | None = None
    # GGA fix quality (0=invalid, 1=GPS, 2=DGPS, 4=RTK fixed, 5=RTK float)
    fix_quality: int = 0
    # Geoid separation (m)
    geoid_sep: float | None = None
    # Leap seconds
    leap_seconds: int | None = None
    # Receiver status
    status: int = -1             # gpsd status field (0=no fix, 1=fix, 2=DGPS, etc.)
    # PPS data
    pps_offset: float | None = None   # PPS offset in seconds
    pps_precision: float | None = None  # PPS precision
    # Parsed device info
    device_driver: str = ""      # e.g. "u-blox"
    device_subtype: str = ""     # Firmware version string
    device_path: str = ""        # e.g. "/dev/ttyACM0"
    device_baud: int = 0         # Baud rate
    device_cycle: float = 0.0    # Update cycle (seconds)
    device_flags: int = 0        # Device capability flags

    @property
    def fix_quality_text(self) -> str:
        """Human-readable fix quality from GGA/gpsd status."""
        q = self.fix_quality
        if q == 0 and self.status > 1:
            # Prefer gpsd status if GGA not parsed
            q = self.status
        return {
            0: "", 1: "GPS", 2: "DGPS", 3: "PPS",
            4: "RTK Fixed", 5: "RTK Float", 6: "Estimated",
            7: "Manual", 8: "Simulation",
        }.get(q, "")

    @property
    def fix_status_text(self) -> str:
        if not self.gpsd_running:
            return "GPSD NOT RUNNING"
        if self.fix_mode == 0:
            return "NO DATA"
        if self.fix_mode == 1 or not self.fix_valid:
            return "NO FIX"
        qual = self.fix_quality_text
        suffix = f" ({qual})" if qual and qual != "GPS" else ""
        if self.fix_mode == 2:
            return f"2D FIX{suffix}"
        if self.fix_mode == 3:
            return f"3D FIX{suffix}"
        return "UNKNOWN"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "fix_mode": self.fix_mode,
            "fix_valid": self.fix_valid,
            "fix_status": self.fix_status_text,
            "fix_quality": self.fix_quality,
            "fix_quality_text": self.fix_quality_text,
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
                    "sig_id": s.sig_id,
                    "health": s.health,
                    "health_text": s.health_text,
                    "quality": s.quality,
                    "quality_text": s.quality_text,
                }
                for s in self.satellites
            ],
            "time_offset": self.time_offset,
            "hdop": self.hdop,
            "vdop": self.vdop,
            "pdop": self.pdop,
            "tdop": self.tdop,
            "gdop": self.gdop,
            "xdop": self.xdop,
            "ydop": self.ydop,
            # Accuracy estimates (metres / m/s / degrees / seconds)
            "eph": self.eph,
            "epv": self.epv,
            "eps": self.eps,
            "ept": self.ept,
            "epd": self.epd,
            "epc": self.epc,
            "epx": self.epx,
            "epy": self.epy,
            # ECEF position
            "ecef_x": self.ecef_x,
            "ecef_y": self.ecef_y,
            "ecef_z": self.ecef_z,
            "ecef_pacc": self.ecef_pacc,
            # Velocity components
            "vel_north": self.vel_north,
            "vel_east": self.vel_east,
            "vel_down": self.vel_down,
            # Geoid / leap seconds
            "geoid_sep": self.geoid_sep,
            "leap_seconds": self.leap_seconds,
            "status": self.status,
            # PPS
            "pps_offset": self.pps_offset,
            "pps_precision": self.pps_precision,
            # Device info
            "device_driver": self.device_driver,
            "device_subtype": self.device_subtype,
            "device_path": self.device_path,
            "device_baud": self.device_baud,
            "device_cycle": self.device_cycle,
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
                self._parse_devices(data)
            elif cls == "PPS":
                self._parse_pps(data)
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
        """Parse $xxGGA - Fix information.

        GGA fields: time, lat, N/S, lon, E/W, quality, numSV, HDOP,
                     alt, M, sep, M, diffAge, diffStation
        """
        if len(parts) < 10:
            return
        # Field 6 = Fix quality indicator
        #   0=Invalid, 1=GPS SPS, 2=DGPS, 3=PPS, 4=RTK Fixed,
        #   5=RTK Float, 6=Estimated, 7=Manual, 8=Simulation
        fix_qual = _safe_int(parts[6])
        if fix_qual is not None:
            self._state.fix_quality = fix_qual
        # Field 7 = number of satellites used
        n_used = _safe_int(parts[7])
        if n_used is not None:
            self._state.satellites_used = n_used
        # Field 8 = HDOP
        hdop = _safe_float(parts[8])
        if hdop is not None:
            self._state.hdop = hdop
        # Field 9 = altitude MSL
        alt = _safe_float(parts[9])
        if alt is not None:
            self._state.altitude = alt
        # Field 11 = Geoid separation (metres)
        if len(parts) >= 12:
            geoid = _safe_float(parts[11])
            if geoid is not None:
                self._state.geoid_sep = geoid

    # -- gpsd JSON parsers --

    def _parse_tpv(self, data: dict):
        """Parse gpsd TPV (Time-Position-Velocity) object.

        Extracts all available fields from UBX-NAV-PVT that gpsd exposes:
        position, velocity, accuracy estimates, ECEF, and timing data.
        """
        mode = data.get("mode", 0)
        self._state.fix_mode = mode
        self._state.timestamp = data.get("time", self._state.timestamp)

        # Status field: 0=none, 1=fix, 2=DGPS, etc.
        status = data.get("status", -1)
        self._state.status = status
        self._state.fix_valid = mode >= 2 and status != 0

        # Map gpsd status to fix_quality if not set by NMEA GGA
        if status > 0 and self._state.fix_quality == 0:
            self._state.fix_quality = status

        if mode >= 2:
            self._state.latitude = data.get("lat", self._state.latitude)
            self._state.longitude = data.get("lon", self._state.longitude)
            self._state.speed = data.get("speed", self._state.speed)
            self._state.track = data.get("track", self._state.track)
        if mode >= 3:
            self._state.altitude = data.get("altMSL", data.get("alt", self._state.altitude))

        # Accuracy estimates from UBX-NAV-PVT via gpsd
        for attr, key in (
            ("eph", "eph"), ("epv", "epv"), ("eps", "eps"),
            ("ept", "ept"), ("epd", "epd"), ("epc", "epc"),
            ("epx", "epx"), ("epy", "epy"),
        ):
            val = data.get(key)
            if val is not None:
                setattr(self._state, attr, val)

        # ECEF coordinates (UBX-NAV-PVT / UBX-NAV-POSECEF)
        for attr, key in (
            ("ecef_x", "ecefx"), ("ecef_y", "ecefy"), ("ecef_z", "ecefz"),
            ("ecef_pacc", "ecefpAcc"),
        ):
            val = data.get(key)
            if val is not None:
                setattr(self._state, attr, val)

        # Velocity components (m/s) from UBX-NAV-PVT
        for attr, key in (
            ("vel_north", "velN"), ("vel_east", "velE"), ("vel_down", "velD"),
        ):
            val = data.get(key)
            if val is not None:
                setattr(self._state, attr, val)

        # Geoid separation
        sep = data.get("geoidSep")
        if sep is not None:
            self._state.geoid_sep = sep

        # Magnetic variation (from UBX-NAV-PVT magDec)
        mag = data.get("magvar")
        if mag is not None:
            self._state.mag_var = mag

        # Leap seconds
        leap = data.get("leapseconds")
        if leap is not None:
            self._state.leap_seconds = leap

    # -- Signal ID mapping for u-blox receivers --
    # gpsd gnssid + sigid → human-readable signal name
    _SIGNAL_MAP = {
        # GPS (gnssid 0)
        (0, 0): "L1CA", (0, 3): "L2CL", (0, 4): "L2CM",
        (0, 6): "L5I", (0, 7): "L5Q",
        # SBAS (gnssid 1)
        (1, 0): "L1CA",
        # Galileo (gnssid 2)
        (2, 0): "E1C", (2, 1): "E1B", (2, 3): "E5aI",
        (2, 4): "E5aQ", (2, 5): "E5bI", (2, 6): "E5bQ",
        # BeiDou (gnssid 3)
        (3, 0): "B1I", (3, 1): "B1Q", (3, 2): "B1C",
        (3, 5): "B2I", (3, 6): "B2Q", (3, 7): "B2a",
        # IRNSS (gnssid 4)
        (4, 0): "L5A",
        # QZSS (gnssid 5)
        (5, 0): "L1CA", (5, 1): "L1S", (5, 4): "L2CM",
        (5, 5): "L2CL", (5, 8): "L5I", (5, 9): "L5Q",
        # GLONASS (gnssid 6)
        (6, 0): "L1OF", (6, 2): "L2OF",
    }

    def _parse_sky(self, data: dict):
        """Parse gpsd SKY object - satellite info.

        Extracts extended per-satellite data that u-blox provides:
        signal ID (multi-frequency), health status, and signal quality.
        """
        sats_json = data.get("satellites", [])
        all_sats = []
        used_prns = set()
        gnss_map = {0: "GP", 1: "SB", 2: "GA", 3: "GB", 4: "IR", 5: "QZ", 6: "GL"}

        for s in sats_json:
            prn = s.get("PRN", 0)
            gnssid = s.get("gnssid", -1)
            constellation = gnss_map.get(gnssid, _identify_constellation("GP", prn))
            used = s.get("used", False)

            # Signal identification (multi-frequency u-blox)
            sigid = s.get("sigid", -1)
            sig_name = self._SIGNAL_MAP.get((gnssid, sigid), "")

            sat = Satellite(
                gnss=constellation,
                prn=prn,
                elevation=s.get("el", 0.0),
                azimuth=s.get("az", 0.0),
                snr=s.get("ss", 0.0),
                used=used,
                sig_id=sig_name,
                health=s.get("health", -1),
                quality=s.get("qual", -1),
            )
            all_sats.append(sat)
            if used:
                used_prns.add(prn)

        self._state.satellites = all_sats
        self._state.satellites_visible = len(all_sats)
        self._state.satellites_used = len(used_prns)
        self._state.used_prns = sorted(used_prns)
        self._used_prns = used_prns

        # DOP values (including extended)
        for attr, key in (
            ("hdop", "hdop"), ("vdop", "vdop"), ("pdop", "pdop"),
            ("tdop", "tdop"), ("gdop", "gdop"), ("xdop", "xdop"),
            ("ydop", "ydop"),
        ):
            val = data.get(key)
            if val is not None:
                setattr(self._state, attr, val)

    def _parse_devices(self, data: dict):
        """Parse gpsd DEVICES message - receiver hardware info.

        Extracts u-blox driver, firmware version, device path, baud rate,
        and update cycle from the gpsd device list.
        """
        devices = data.get("devices", [])
        self._state.devices = devices
        # Use the first active device for summary info
        for dev in devices:
            self._state.device_driver = dev.get("driver", "")
            self._state.device_subtype = dev.get("subtype", "")
            self._state.device_path = dev.get("path", "")
            self._state.device_baud = dev.get("bps", 0)
            self._state.device_cycle = dev.get("cycle", 0.0)
            self._state.device_flags = dev.get("flags", 0)
            break

    def _parse_pps(self, data: dict):
        """Parse gpsd PPS message - precision timing pulse data.

        PPS provides sub-microsecond timing offsets for NTP disciplining.
        Fields: real_sec, real_nsec, clock_sec, clock_nsec, precision.
        """
        real_sec = data.get("real_sec")
        real_nsec = data.get("real_nsec", 0)
        clock_sec = data.get("clock_sec")
        clock_nsec = data.get("clock_nsec", 0)
        if real_sec is not None and clock_sec is not None:
            # Compute offset using integer arithmetic to avoid float precision
            # loss from adding nanoseconds to large epoch seconds
            diff_sec = real_sec - clock_sec
            diff_nsec = real_nsec - clock_nsec
            self._state.pps_offset = diff_sec + diff_nsec * 1e-9
        precision = data.get("precision")
        if precision is not None:
            self._state.pps_precision = precision


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
