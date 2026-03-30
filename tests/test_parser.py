"""Validate NMEA parser against real system data.

This test feeds the EXACT NMEA sentences captured from the live GPS system
and verifies the parser produces correct results.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gpssat.gps_client import NmeaParser, _nmea_checksum_valid

# Real NMEA sentences from the live system (gpspipe -r output)
REAL_NMEA = [
    "$GPRMC,213411.00,V,3512.5613,S,14900.6865,E,0.0000,-0.000,280326,12.3,E*73",
    "$GPGSA,A,1,,,,,,,,,,,,,,,,*32",
    "$GPGSV,6,1,22,01,01,229,21,02,28,224,20,08,35,264,21,09,-55,316,23*56",
    "$GPGSV,6,2,22,10,55,146,19,18,15,050,00,20,-37,138,23,23,26,111,22*5B",
    "$GPGSV,6,3,22,24,11,136,22,26,-12,351,23,27,39,304,25,28,16,016,16*50",
    "$GPGSV,6,4,22,29,-29,033,23,30,-49,217,23,31,-1,358,23,32,83,338,00*62",
    "$GPGSV,6,5,22,42,48,345,00,48,01,083,00,50,49,353,00,194,16,350,00*44",
    "$GPGSV,6,6,22,195,81,034,00,196,50,312,23*70",
]


def test_checksum_validation():
    """All real NMEA sentences must have valid checksums."""
    for sentence in REAL_NMEA:
        assert _nmea_checksum_valid(sentence), f"Bad checksum: {sentence}"
    # Test with intentionally bad checksum
    bad = "$GPRMC,213411.00,V,3512.5613,S,14900.6865,E,0.0000,-0.000,280326,12.3,E*FF"
    assert not _nmea_checksum_valid(bad)


def test_rmc_parsing():
    """GPRMC must be parsed correctly - V status means NO FIX."""
    parser = NmeaParser()
    parser.feed_nmea(REAL_NMEA[0])  # RMC sentence
    state = parser.state

    # Status must be V (void/invalid)
    assert state.rmc_status == "V"
    assert state.fix_valid is False

    # Time should be parsed even though fix is invalid
    assert "21:34:11" in state.timestamp
    assert "2026-03-28" in state.timestamp

    # Position is present in NMEA even with V status
    # Lat: 35°12.5613'S = -35.209355
    assert state.latitude is not None
    assert abs(state.latitude - (-35.209355)) < 0.001

    # Lon: 149°00.6865'E = 149.011442
    assert state.longitude is not None
    assert abs(state.longitude - 149.011442) < 0.001

    # Speed
    assert state.speed == 0.0

    # Magnetic variation: 12.3°E
    assert state.mag_var is not None
    assert abs(state.mag_var - 12.3) < 0.01


def test_gsa_parsing():
    """GPGSA mode=1 means no fix, no satellites used."""
    parser = NmeaParser()
    parser.feed_nmea(REAL_NMEA[0])  # RMC first
    parser.feed_nmea(REAL_NMEA[1])  # GSA
    state = parser.state

    assert state.fix_mode == 1  # No fix
    assert state.satellites_used == 0
    assert state.used_prns == []
    assert state.fix_valid is False


def test_gsv_full_parse():
    """All 6 GSV messages must produce exactly 22 satellites."""
    parser = NmeaParser()
    # Feed RMC and GSA first
    parser.feed_nmea(REAL_NMEA[0])
    parser.feed_nmea(REAL_NMEA[1])
    # Feed all 6 GSV messages
    for sentence in REAL_NMEA[2:8]:
        parser.feed_nmea(sentence)
    state = parser.state

    assert state.satellites_visible == 22

    # Build a lookup by PRN
    by_prn = {s.prn: s for s in state.satellites}

    # Verify specific satellites from the data
    # PRN 1: elev 1°, azim 229°, SNR 21
    assert 1 in by_prn
    assert by_prn[1].elevation == 1.0
    assert by_prn[1].azimuth == 229.0
    assert by_prn[1].snr == 21.0
    assert by_prn[1].gnss == "GP"

    # PRN 9: elev -55° (negative!), azim 316°, SNR 23
    assert 9 in by_prn
    assert by_prn[9].elevation == -55.0
    assert by_prn[9].azimuth == 316.0
    assert by_prn[9].snr == 23.0

    # PRN 18: elev 15°, azim 50°, SNR 0 (no signal)
    assert 18 in by_prn
    assert by_prn[18].snr == 0.0

    # PRN 42: SBAS (PRN 33-64 range)
    assert 42 in by_prn
    assert by_prn[42].gnss == "SB"
    assert by_prn[42].constellation_name == "SBAS"
    assert by_prn[42].snr == 0.0

    # PRN 194: QZSS (PRN 193-200 range)
    assert 194 in by_prn
    assert by_prn[194].gnss == "QZ"
    assert by_prn[194].constellation_name == "QZSS"

    # PRN 195: QZSS
    assert 195 in by_prn
    assert by_prn[195].gnss == "QZ"

    # PRN 196: QZSS, has signal (SNR 23)
    assert 196 in by_prn
    assert by_prn[196].gnss == "QZ"
    assert by_prn[196].snr == 23.0

    # No satellites should be marked as used (GSA mode=1, no PRNs listed)
    for s in state.satellites:
        assert s.used is False


def test_negative_elevations():
    """Parser must handle negative elevation values (below horizon)."""
    parser = NmeaParser()
    for sentence in REAL_NMEA:
        parser.feed_nmea(sentence)
    state = parser.state

    neg_elev_prns = [s.prn for s in state.satellites if s.elevation < 0]
    # From data: PRN 9 (-55°), 20 (-37°), 26 (-12°), 29 (-29°), 30 (-49°), 31 (-1°)
    assert 9 in neg_elev_prns
    assert 20 in neg_elev_prns
    assert 26 in neg_elev_prns
    assert 29 in neg_elev_prns
    assert 30 in neg_elev_prns
    assert 31 in neg_elev_prns


def test_mixed_constellations():
    """Must correctly identify GPS, SBAS, and QZSS from PRN ranges."""
    parser = NmeaParser()
    for sentence in REAL_NMEA:
        parser.feed_nmea(sentence)
    state = parser.state

    constellations = set(s.gnss for s in state.satellites)
    assert "GP" in constellations  # GPS (PRN 1-32)
    assert "SB" in constellations  # SBAS (PRN 42, 48, 50)
    assert "QZ" in constellations  # QZSS (PRN 194, 195, 196)


def test_no_fix_state():
    """With this data, fix_valid must be False and fix_status must be NO FIX."""
    parser = NmeaParser()
    for sentence in REAL_NMEA:
        parser.feed_nmea(sentence)
    state = parser.state

    assert state.fix_valid is False
    assert state.fix_mode == 1
    assert state.fix_status_text == "NO FIX"


def test_satellites_match_cgps():
    """Verify satellite count matches cgps output: Seen 21/Used 0.

    Note: cgps shows 21 but GPGSV reports 22 total. The difference is that
    cgps may deduplicate or the GSV count field (22) includes a satellite
    that cgps doesn't display. Our parser should report 22 as that's what
    the NMEA data contains.
    """
    parser = NmeaParser()
    for sentence in REAL_NMEA:
        parser.feed_nmea(sentence)
    state = parser.state

    # GSV says 22 satellites total
    assert state.satellites_visible == 22
    # GSA says 0 used
    assert state.satellites_used == 0


def test_signal_strength_distribution():
    """Verify SNR values match real data patterns."""
    parser = NmeaParser()
    for sentence in REAL_NMEA:
        parser.feed_nmea(sentence)
    state = parser.state

    with_signal = [s for s in state.satellites if s.snr > 0]
    without_signal = [s for s in state.satellites if s.snr == 0]

    # From the data, many satellites have signal (SNR 16-25 range)
    # and several have 0 (no signal)
    assert len(with_signal) > 0
    assert len(without_signal) > 0

    # All SNR values should be in valid range (0-50 dBHz typical)
    for s in state.satellites:
        assert 0 <= s.snr <= 50, f"PRN {s.prn} has invalid SNR {s.snr}"


def test_gpsd_json_tpv():
    """Test parsing of gpsd JSON TPV message."""
    parser = NmeaParser()
    parser.feed_json({
        "class": "TPV",
        "mode": 1,
        "time": "2026-03-28T21:35:36.000Z",
        "status": 0,
    })
    state = parser.state

    assert state.fix_mode == 1
    assert state.fix_valid is False
    assert state.timestamp == "2026-03-28T21:35:36.000Z"


def test_gpsd_json_sky():
    """Test parsing of gpsd JSON SKY message with mixed constellations."""
    parser = NmeaParser()
    parser.feed_json({
        "class": "SKY",
        "satellites": [
            {"PRN": 1, "gnssid": 0, "el": 1, "az": 229, "ss": 21, "used": False},
            {"PRN": 9, "gnssid": 0, "el": -55, "az": 316, "ss": 23, "used": False},
            {"PRN": 42, "gnssid": 1, "el": 48, "az": 345, "ss": 0, "used": False},
            {"PRN": 194, "gnssid": 5, "el": 16, "az": 350, "ss": 0, "used": False},
        ],
    })
    state = parser.state

    assert state.satellites_visible == 4
    assert state.satellites_used == 0
    by_prn = {s.prn: s for s in state.satellites}
    assert by_prn[1].gnss == "GP"
    assert by_prn[42].gnss == "SB"
    assert by_prn[194].gnss == "QZ"
    assert by_prn[9].elevation == -55


def test_state_dict_serialization():
    """to_dict() must produce valid JSON-serializable output."""
    parser = NmeaParser()
    for sentence in REAL_NMEA:
        parser.feed_nmea(sentence)
    d = parser.state.to_dict()

    assert isinstance(d, dict)
    assert d["fix_status"] == "NO FIX"
    assert d["satellites_visible"] == 22
    assert d["satellites_used"] == 0
    assert len(d["satellites"]) == 22
    # Check a satellite dict
    sat0 = d["satellites"][0]
    assert "gnss" in sat0
    assert "prn" in sat0
    assert "elevation" in sat0
    assert "azimuth" in sat0
    assert "snr" in sat0
    assert "used" in sat0
    assert "constellation" in sat0

    import json
    json.dumps(d)  # Must not raise


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed} tests")
    if failed:
        sys.exit(1)
