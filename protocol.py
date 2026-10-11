"""Mictrack MT710 V1.0 TCP reports and downlink commands."""
import datetime as dt
import re

MAX_FRAME = 8192


class Frames:
    def __init__(self):
        self.buffer = b""

    def feed(self, data):
        self.buffer += data
        result = []
        while b"##" in self.buffer:
            frame, self.buffer = self.buffer.split(b"##", 1)
            if len(frame) > MAX_FRAME:
                raise ValueError("Report too large")
            result.append(frame.strip().decode("ascii") + "##")
        if len(self.buffer) > MAX_FRAME:
            raise ValueError("Report too large")
        return result


def coordinate(value, hemisphere, latitude):
    if hemisphere not in ("NS" if latitude else "EW") or len(hemisphere) != 1:
        raise ValueError("Invalid hemisphere")
    number = float(value)
    degrees = int(number // 100)
    minutes = number - degrees * 100
    limit = 90 if latitude else 180
    if number < 0 or not 0 <= minutes < 60 or degrees > limit or (degrees == limit and minutes):
        raise ValueError("Invalid coordinate")
    return round((degrees + minutes / 60) * (-1 if hemisphere in "SW" else 1), 7)


def timestamp(time, date):
    value = dt.datetime.strptime(date + time[:6], "%d%m%y%H%M%S")
    return value.replace(tzinfo=dt.timezone.utc).isoformat()


def parse(frame):
    reply = re.fullmatch(r"#(\d{15})#REPLY#(.+)##", frame, re.S)
    if reply:
        text = reply[2].strip()
        return {"imei": reply[1], "kind": "reply", "reply": text,
                "family": text.split(",")[0].split("#")[0]}
    match = re.fullmatch(r"#(\d{15})#MT710(?:L)?#0000#([A-Z]+)#1\s*#(\d{3,5})#?([^$]*)\$([^*]+)\*([0-9A-Fa-f]{2})\s*##", frame)
    if not match:
        raise ValueError("Unsupported report format")
    imei, event, voltage, cell, sentence, checksum = match.groups()
    actual = 0
    for char in sentence:
        actual ^= ord(char)
    if actual != int(checksum, 16):
        raise ValueError("Invalid NMEA checksum")
    fields = sentence.split(",")
    report = {"imei": imei, "kind": "report", "event": event,
              "voltage": int(voltage) / 1000, "cell": cell or None,
              "latitude": None, "longitude": None, "speed_kmh": None,
              "fix": fields[2], "source": fields[0], "raw": frame}
    if fields[0] in ("GPRMC", "GNRMC") and len(fields) >= 10:
        report["recorded_at"] = timestamp(fields[1], fields[9])
        if fields[2] in ("A", "L"):
            report["latitude"] = coordinate(fields[3], fields[4], True)
            report["longitude"] = coordinate(fields[5], fields[6], False)
            if fields[7]:
                speed = float(fields[7])
                if not 0 <= speed < 10000:
                    raise ValueError("Invalid speed")
                report["speed_kmh"] = round(speed * 1.852, 1)
    elif fields[0] == "WIFI" and len(fields) >= 4:
        report["recorded_at"] = timestamp(fields[1], fields[-1])
    else:
        raise ValueError("Unsupported position sentence")
    if report["fix"] not in ("A", "V", "L"):
        raise ValueError("Invalid fix status")
    return report


MODES = {
    0: (10, 600, "seconds", True),
    1: (10, 600, "seconds", False),
    2: (10, 60, "minutes", False),
    3: (1, 24, "hours", False),
    4: (10, 600, "seconds", False),
    7: (10, 1440, "minutes", True),
}


def integer(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"Expected an integer between {low} and {high}")
    return value


def command(settings):
    kind = settings.get("kind")
    if kind == "mode":
        mode = integer(settings.get("mode"), 0, 7)
        if mode not in MODES:
            raise ValueError("Unsupported mode")
        low, high, _, still = MODES[mode]
        period = integer(settings.get("period"), low, high)
        text = f"MODE,{mode},{period}"
        if still:
            text += f",{integer(settings.get('still_hours'), 1, 24)}"
        if mode == 2:
            # Duty-cycle GPS; leave TCP up to allow browser configuration.
            text += ",0,1"
        return text
    if kind == "read_config":
        return f"RCONF,{integer(settings.get('page', 1), 1, 4)}"
    if kind == "last_position":
        return f"LEP,{integer(settings.get('enabled'), 0, 1)}"
    raise ValueError("Unsupported command")
