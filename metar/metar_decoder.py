import re
from typing import Dict, List

WEATHER_MAP = {
    "DZ": "drizzle",
    "RA": "rain",
    "SN": "snow",
    "SG": "snow grains",
    "IC": "ice crystals",
    "PL": "ice pellets",
    "GR": "hail",
    "GS": "small hail",
    "UP": "unknown precipitation",
    "BR": "mist",
    "FG": "fog",
    "FU": "smoke",
    "VA": "volcanic ash",
    "DU": "widespread dust",
    "SA": "sand",
    "HZ": "haze",
    "PY": "spray",
    "PO": "dust/sand whirls",
    "SQ": "squalls",
    "FC": "funnel cloud",
    "SS": "sandstorm",
    "DS": "duststorm",
    "TS": "thunderstorm",
    "SH": "showers",
    "FZ": "freezing",
    "MI": "shallow",
    "BC": "patches of",
    "PR": "partial",
    "DR": "low drifting",
    "BL": "blowing",
    "VC": "in the vicinity",
}

CLOUD_MAP = {
    "SKC": "sky clear",
    "CLR": "clear",
    "NSC": "no significant clouds",
    "FEW": "few clouds",
    "SCT": "scattered clouds",
    "BKN": "broken clouds",
    "OVC": "overcast",
    "VV": "vertical visibility",
}

DIRECTION_MAP = {
    0: "N",
    45: "NE",
    90: "E",
    135: "SE",
    180: "S",
    225: "SW",
    270: "W",
    315: "NW",
    360: "N",
}

MIDDLE_CLOUD_REMARK_MAP = {
    "AC1": "Altostratus translucidus.",
    "AC2": "Altostratus opacus or nimbostratus.",
    "AC3": "Altocumulus translucidus at a single level.",
    "AC4": "Patchy altocumulus, often in almond or fish shapes.",
    "AC5": "Altocumulus in bands or one or more continuous layers.",
    "AC6": "Altocumulus formed by the spreading out of cumulus.",
    "AC7": "Altocumulus in two or more layers, often with altostratus or nimbostratus.",
    "AC8": "Altocumulus castellanus or floccus, showing turrets or small convective towers.",
    "AC9": "Chaotic altocumulus at several levels.",
}


def c_to_f(celsius: int) -> int:
    return round((celsius * 9 / 5) + 32)


def ft_to_m(feet: int) -> int:
    return round(feet * 0.3048)


def miles_to_km(miles: float) -> float:
    return miles * 1.60934


def knots_to_mps(knots: int) -> float:
    return knots * 0.514444


def inhg_to_hpa(inhg: float) -> int:
    return round(inhg * 33.8639)


def parse_signed_temperature(value: str) -> int | None:
    if value == "":
        return None
    if value.startswith("M") and value[1:].isdigit():
        return -int(value[1:])
    if value.isdigit():
        return int(value)
    return None


def format_temperature_value(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value}°C ({c_to_f(value)}°F)"


def direction_to_compass(degrees: int) -> str:
    normalized = round(degrees / 45) * 45
    if normalized == 360 and degrees != 360:
        normalized = 0
    return DIRECTION_MAP.get(normalized, "")


def decode_temperature(token: str) -> str:
    if "/" not in token:
        return ""

    temp_str, dew_str = token.split("/", 1)

    def parse_temp(value: str) -> str:
        if value == "":
            return "unknown"
        if value.startswith("M"):
            return f"-{int(value[1:])}°C"
        return f"{int(value)}°C"

    try:
        temp = parse_temp(temp_str)
        dew = parse_temp(dew_str)
        return f"Temperature {temp}, dew point {dew}."
    except ValueError:
        return ""


def decode_altimeter(token: str) -> str:
    if re.fullmatch(r"A\d{4}", token):
        inches = float(token[1:]) / 100
        return f"Altimeter {inches:.2f} inches of mercury."
    if re.fullmatch(r"Q\d{4}", token):
        return f"Altimeter {int(token[1:])} hectopascals."
    return ""


def decode_visibility(token: str) -> str:
    if token.endswith("SM"):
        raw = token[:-2]
        return f"Visibility {raw} statute miles."
    if re.fullmatch(r"\d{4}", token):
        meters = int(token)
        if meters == 9999:
            return "Visibility 10 kilometers or more."
        return f"Visibility {meters} meters."
    return ""


def decode_wind(token: str) -> str:
    m = re.fullmatch(r"(VRB|\d{3})(\d{2,3})(G(\d{2,3}))?KT", token)
    if not m:
        return ""

    direction, speed, _, gust = m.groups()
    speed_value = int(speed)

    if direction == "VRB":
        message = f"Wind variable at {speed_value} knots"
    elif direction == "000" and speed_value == 0:
        message = "Calm wind"
    else:
        message = f"Wind from {direction}° at {speed_value} knots"

    if gust:
        message += f", gusting to {int(gust)} knots"

    return message + "."


def decode_cloud(token: str) -> str:
    if token in CLOUD_MAP:
        return CLOUD_MAP[token].capitalize() + "."

    m = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?", token)
    if not m:
        return ""

    amount, height, cloud_type = m.groups()
    height_ft = int(height) * 100
    desc = CLOUD_MAP[amount]

    result = f"{desc.capitalize()} at {height_ft:,} feet"
    if cloud_type == "CB":
        result += " with cumulonimbus"
    elif cloud_type == "TCU":
        result += " with towering cumulus"

    return result + "."


def decode_weather(token: str) -> str:
    if not re.fullmatch(r"[-+]?([A-Z]{2}){1,4}", token):
        return ""

    intensity = ""
    body = token

    if token.startswith("-"):
        intensity = "light "
        body = token[1:]
    elif token.startswith("+"):
        intensity = "heavy "
        body = token[1:]

    parts: List[str] = []
    i = 0
    while i < len(body):
        code = body[i : i + 2]
        if code in WEATHER_MAP:
            parts.append(WEATHER_MAP[code])
            i += 2
        else:
            return ""

    if not parts:
        return ""

    return (intensity + " ".join(parts)).capitalize() + "."


def decode_time(token: str) -> str:
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})Z", token)
    if not m:
        return ""
    day, hour, minute = m.groups()
    return f"Observed on day {int(day)} at {hour}:{minute} UTC."


def describe_temperature(token: str) -> str:
    if "/" not in token:
        return ""

    temp_str, dew_str = token.split("/", 1)
    temp = parse_signed_temperature(temp_str)
    dew = parse_signed_temperature(dew_str)

    if temp is None and dew is None:
        return ""

    return f"Temperature {format_temperature_value(temp)}, dew point {format_temperature_value(dew)}."


def describe_altimeter(token: str) -> str:
    if re.fullmatch(r"A\d{4}", token):
        inches = float(token[1:]) / 100
        return f"Sea level pressure is {inches:.2f} inHg ({inhg_to_hpa(inches)} hPa)."
    if re.fullmatch(r"Q\d{4}", token):
        hpa = int(token[1:])
        return f"Sea level pressure is {hpa} hPa ({hpa / 33.8639:.2f} inHg)."
    return ""


def describe_visibility(token: str) -> str:
    if token.endswith("SM"):
        raw = token[:-2]
        try:
            miles = float(raw)
        except ValueError:
            if "/" in raw:
                numerator, denominator = raw.split("/", 1)
                try:
                    miles = int(numerator) / int(denominator)
                except ValueError:
                    return ""
            else:
                return ""
        return f"Visibility {raw} statute miles ({miles_to_km(miles):.1f} km)."

    if re.fullmatch(r"\d{4}", token):
        meters = int(token)
        if meters == 9999:
            return "Visibility 10 kilometers or more."
        return f"Visibility {meters:,} meters ({meters / 1000:.1f} km)."
    return ""


def describe_wind(token: str) -> str:
    m = re.fullmatch(r"(VRB|\d{3})(\d{2,3})(G(\d{2,3}))?KT", token)
    if not m:
        return ""

    direction, speed, _, gust = m.groups()
    speed_value = int(speed)
    speed_mps = round(knots_to_mps(speed_value), 1)

    if direction == "VRB":
        message = f"Winds variable at {speed_value} knots ({speed_mps} m/s)"
    elif direction == "000" and speed_value == 0:
        message = "Calm wind"
    else:
        degrees = int(direction)
        compass = direction_to_compass(degrees)
        if compass:
            message = f"Winds from {degrees}° ({compass}) at {speed_value} knots ({speed_mps} m/s)"
        else:
            message = f"Winds from {degrees}° at {speed_value} knots ({speed_mps} m/s)"

    if gust:
        gust_value = int(gust)
        message += f", gusting to {gust_value} knots ({round(knots_to_mps(gust_value), 1)} m/s)"

    return message + "."


def describe_cloud(token: str) -> str:
    if token in CLOUD_MAP:
        return CLOUD_MAP[token].capitalize() + "."

    m = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?", token)
    if not m:
        return ""

    amount, height, cloud_type = m.groups()
    height_ft = int(height) * 100
    height_m = ft_to_m(height_ft)
    desc = CLOUD_MAP[amount].capitalize()

    result = f"{desc} at {height_ft:,} feet ({height_m:,} meters)"
    if cloud_type == "CB":
        result += " with cumulonimbus"
    elif cloud_type == "TCU":
        result += " with towering cumulus"

    return result + "."


def describe_time(token: str) -> str:
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})Z", token)
    if not m:
        return ""
    day, hour, minute = m.groups()
    return f"Issued on the {int(day)}th of the month at {hour}:{minute} UTC."


def split_remarks(remarks: str) -> List[str]:
    return [token for token in remarks.split() if token]


def format_time_hhmm(value: str) -> str:
    if len(value) == 2:
        return f"{value} minutes past the hour"
    if len(value) == 4:
        return f"{value[:2]}:{value[2:]} UTC"
    return value


def parse_slp(token: str) -> str:
    m = re.fullmatch(r"SLP(\d{3})", token)
    if not m:
        return ""
    value = int(m.group(1)) / 10
    pressure = 1000 + value if value < 50 else 900 + value
    return f"Sea level pressure from remarks: {pressure:.1f} hPa."


def parse_hourly_precip(token: str) -> str:
    m = re.fullmatch(r"P(\d{4})", token)
    if not m:
        return ""
    inches = int(m.group(1)) / 100
    mm = inches * 25.4
    return f"Hourly precipitation: {inches:.2f} inches ({mm:.1f} mm)."


def parse_exact_temperature(token: str) -> str:
    m = re.fullmatch(r"T([01])(\d{3})([01])(\d{3})", token)
    if not m:
        return ""
    temp_sign, temp_digits, dew_sign, dew_digits = m.groups()
    temp = int(temp_digits) / 10
    dew = int(dew_digits) / 10
    if temp_sign == "1":
        temp = -temp
    if dew_sign == "1":
        dew = -dew
    return (
        f"Exact temperature {temp:.1f}°C ({c_to_f(round(temp))}°F), "
        f"exact dew point {dew:.1f}°C ({c_to_f(round(dew))}°F)."
    )


def parse_begin_end_phenomenon(token: str) -> str:
    m = re.fullmatch(r"([A-Z]{2,4})B(\d{2})E(\d{2})", token)
    if not m:
        return ""
    phenomenon, begin, end = m.groups()
    return f"{phenomenon} began at {begin} and ended at {end} minutes past the hour."


def parse_simple_remark_token(token: str) -> str:
    if token == "AO1":
        return "Automated station without a precipitation discriminator."
    if token == "AO2":
        return "Automated station with a precipitation discriminator."
    if token == "PRESRR":
        return "Pressure is rising rapidly."
    if token == "PRESFR":
        return "Pressure is falling rapidly."
    if token == "FROPA":
        return "Frontal passage occurred."
    if token == "PNO":
        return "Tipping bucket rain gauge information not available."
    if token in MIDDLE_CLOUD_REMARK_MAP:
        return f"Middle cloud type: {MIDDLE_CLOUD_REMARK_MAP[token]}"

    for parser in (
        parse_slp,
        parse_hourly_precip,
        parse_exact_temperature,
        parse_begin_end_phenomenon,
    ):
        parsed = parser(token)
        if parsed:
            return parsed

    return ""


def parse_remarks_tokens(tokens: List[str]) -> List[Dict[str, str]]:
    parsed: List[Dict[str, str]] = []
    i = 0

    while i < len(tokens):
        token = tokens[i]

        if token == "PK" and i + 2 < len(tokens) and tokens[i + 1] == "WND":
            wind_token = tokens[i + 2]
            m = re.fullmatch(r"(\d{3})(\d{2,3})/(\d{2})(\d{2})", wind_token)
            if m:
                direction, speed, hour, minute = m.groups()
                parsed.append(
                    {
                        "title": "Remark",
                        "token": f"PK WND {wind_token}",
                        "description": (
                            f"Peak wind from {int(direction)}° at {int(speed)} knots "
                            f"({round(knots_to_mps(int(speed)), 1)} m/s) at {hour}:{minute} UTC."
                        ),
                    }
                )
                i += 3
                continue

        if token == "WSHFT" and i + 1 < len(tokens):
            shift_token = tokens[i + 1]
            if re.fullmatch(r"\d{2}|\d{4}", shift_token):
                parsed.append(
                    {
                        "title": "Remark",
                        "token": f"WSHFT {shift_token}",
                        "description": f"Wind shift recorded at {format_time_hhmm(shift_token)}.",
                    }
                )
                i += 2
                continue

        if token == "VIS" and i + 1 < len(tokens):
            vis_token = tokens[i + 1]
            if re.fullmatch(r"\d+/\d+", vis_token):
                try:
                    numerator, denominator = vis_token.split("/", 1)
                    miles = int(numerator) / int(denominator)
                    parsed.append(
                        {
                            "title": "Remark",
                            "token": f"VIS {vis_token}",
                            "description": f"Remark visibility {miles:g} statute miles ({miles_to_km(miles):.1f} km).",
                        }
                    )
                    i += 2
                    continue
                except ValueError:
                    pass

        description = parse_simple_remark_token(token)
        parsed.append(
            {
                "title": "Remark",
                "token": token,
                "description": description or "Raw remark token retained.",
            }
        )
        i += 1

    return parsed


def decode_metar(metar: str) -> str:
    tokens = metar.strip().split()
    if not tokens:
        return "Empty METAR."

    parts: List[str] = []
    i = 0

    if tokens[i] in {"METAR", "SPECI"}:
        parts.append(
            "Routine aviation weather report."
            if tokens[i] == "METAR"
            else "Special aviation weather report."
        )
        i += 1

    if i < len(tokens) and re.fullmatch(r"[A-Z]{4}", tokens[i]):
        parts.append(f"Station: {tokens[i]}.")
        i += 1

    if i < len(tokens):
        decoded_time = decode_time(tokens[i])
        if decoded_time:
            parts.append(decoded_time)
            i += 1

    if i < len(tokens) and tokens[i] in {"AUTO", "COR"}:
        if tokens[i] == "AUTO":
            parts.append("This is an automated report.")
        else:
            parts.append("This is a corrected report.")
        i += 1

    while i < len(tokens):
        token = tokens[i]

        if token == "RMK":
            remarks = " ".join(tokens[i + 1 :])
            if remarks:
                parts.append(f"Remarks: {remarks}.")
            break

        if (
            i + 1 < len(tokens)
            and re.fullmatch(r"\d+", token)
            and re.fullmatch(r"\d/\dSM", tokens[i + 1])
        ):
            parts.append(f"Visibility {token} {tokens[i + 1][:-2]} statute miles.")
            i += 2
            continue

        for decoder in (
            decode_wind,
            decode_visibility,
            decode_weather,
            decode_cloud,
            decode_temperature,
            decode_altimeter,
        ):
            text = decoder(token)
            if text:
                parts.append(text)
                break

        i += 1

    return " ".join(parts)


def parse_metar_sections(metar: str) -> List[Dict[str, str]]:
    tokens = metar.strip().split()
    if not tokens:
        return []

    sections: List[Dict[str, str]] = []
    i = 0

    if tokens[i] in {"METAR", "SPECI"}:
        report_type = "Routine aviation weather report." if tokens[i] == "METAR" else "Special aviation weather report."
        sections.append(
            {
                "title": "Report Type",
                "token": tokens[i],
                "description": report_type,
            }
        )
        i += 1

    if i < len(tokens) and re.fullmatch(r"[A-Z]{4}", tokens[i]):
        sections.append(
            {
                "title": "Identifier",
                "token": tokens[i],
                "description": f"Reporting station code: {tokens[i]}.",
            }
        )
        i += 1

    if i < len(tokens):
        decoded_time = describe_time(tokens[i]) or decode_time(tokens[i])
        if decoded_time:
            sections.append(
                {
                    "title": "Time Issued",
                    "token": tokens[i],
                    "description": decoded_time,
                }
            )
            i += 1

    if i < len(tokens) and tokens[i] in {"AUTO", "COR"}:
        description = (
            "This is an automated report."
            if tokens[i] == "AUTO"
            else "This is a corrected report."
        )
        sections.append(
            {
                "title": "Report Status",
                "token": tokens[i],
                "description": description,
            }
        )
        i += 1

    while i < len(tokens):
        token = tokens[i]

        if token == "RMK":
            remarks = " ".join(tokens[i + 1 :]).strip()
            if remarks:
                remark_tokens = split_remarks(remarks)
                parsed_remarks = parse_remarks_tokens(remark_tokens)
                parsed_descriptions = [
                    item["description"]
                    for item in parsed_remarks
                    if item["description"] != "Raw remark token retained."
                ]
                sections.append(
                    {
                        "title": "Remarks",
                        "token": remarks,
                        "description": (
                            " ".join(parsed_descriptions)
                            if parsed_descriptions
                            else "Raw remarks section preserved from the METAR."
                        ),
                        "layout": "full",
                        "items": [
                            {
                                "token": item["token"],
                                "description": item["description"],
                            }
                            for item in parsed_remarks
                        ],
                    }
                )
            break

        if (
            i + 1 < len(tokens)
            and re.fullmatch(r"\d+", token)
            and re.fullmatch(r"\d/\dSM", tokens[i + 1])
        ):
            raw = f"{token} {tokens[i + 1]}"
            try:
                whole = float(token)
                numerator, denominator = tokens[i + 1][:-2].split("/", 1)
                miles = whole + (int(numerator) / int(denominator))
                description = f"Visibility {raw[:-2] if raw.endswith('SM') else raw} statute miles ({miles_to_km(miles):.1f} km)."
            except ValueError:
                description = f"Visibility {token} {tokens[i + 1][:-2]} statute miles."
            sections.append(
                {
                    "title": "Prevailing Visibility",
                    "token": raw,
                    "description": description,
                }
            )
            i += 2
            continue

        matched = False
        for title, decoder in (
            ("Wind", describe_wind),
            ("Prevailing Visibility", describe_visibility),
            ("Weather", decode_weather),
            ("Clouds", describe_cloud),
            ("Temperature", describe_temperature),
            ("Pressure", describe_altimeter),
        ):
            text = decoder(token)
            if text:
                sections.append(
                    {
                        "title": title,
                        "token": token,
                        "description": text,
                    }
                )
                matched = True
                break

        if not matched:
            sections.append(
                {
                    "title": "Additional Data",
                    "token": token,
                    "description": "Token recognized but not yet expanded by this decoder.",
                }
            )

        i += 1

    return sections
