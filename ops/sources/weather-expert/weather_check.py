#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import warnings
warnings.filterwarnings('ignore', message=r'.*urllib3 .*charset_normalizer.*doesn.*supported version.*')
warnings.filterwarnings('ignore', message=r'.*urllib3 .*chardet.*doesn.*supported version.*')
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import RequestsDependencyWarning

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOLIDAY_API = 'https://\u6bcf\u5e74\u5de5\u4f5c\u65e5\u6570\u91cf.com/api/holidays/{year}'
WTTR_API = 'https://wttr.in/{query}?format=j1'
warnings.filterwarnings('ignore', category=RequestsDependencyWarning)

REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0'}
LOCAL_TZ = ZoneInfo('Asia/Shanghai')

LOCATIONS = {
    'shenzhen_pingzhou': {
        'label': '\u6df1\u5733\u576a\u6d32',
        'query': 'Shenzhen,Pingzhou',
        'aliases': {'sz', 'shenzhen', 'pingzhou', 'shenzhen_pingzhou', 'shenzhen-pingzhou', '\u6df1\u5733', '\u576a\u6d32', '\u6df1\u5733\u576a\u6d32'},
    },
    'guangzhou_yayuncheng': {
        'label': '\u5e7f\u5dde\u4e9a\u8fd0\u57ce',
        'query': 'Guangzhou',
        'aliases': {'gz', 'guangzhou', 'yayuncheng', 'guangzhou_yayuncheng', 'guangzhou-yayuncheng', '\u5e7f\u5dde', '\u4e9a\u8fd0\u57ce', '\u5e7f\u5dde\u4e9a\u8fd0\u57ce'},
    },
}

WEATHER_MAP = {
    '113': '\u6674',
    '116': '\u591a\u4e91',
    '119': '\u9634',
    '122': '\u9634',
    '143': '\u96fe',
    '176': '\u9635\u96e8',
    '179': '\u96e8\u5939\u96ea',
    '182': '\u96e8\u5939\u96ea',
    '185': '\u51bb\u96e8',
    '200': '\u96f7\u9635\u96e8',
    '227': '\u5c0f\u96ea',
    '230': '\u5927\u96ea',
    '248': '\u96fe',
    '260': '\u5927\u96fe',
    '263': '\u6bdb\u6bdb\u96e8',
    '266': '\u5c0f\u96e8',
    '281': '\u51bb\u96e8',
    '284': '\u51bb\u96e8',
    '293': '\u5c0f\u96e8',
    '296': '\u4e2d\u96e8',
    '299': '\u4e2d\u96e8',
    '302': '\u5927\u96e8',
    '305': '\u5927\u96e8',
    '308': '\u66b4\u96e8',
    '311': '\u51bb\u96e8',
    '314': '\u51bb\u96e8',
    '317': '\u51bb\u96e8',
    '320': '\u96e8\u5939\u96ea',
    '323': '\u96e8\u5939\u96ea',
    '326': '\u96e8\u5939\u96ea',
    '329': '\u5927\u96ea',
    '332': '\u5927\u96ea',
    '335': '\u5927\u96ea',
    '338': '\u51b0\u7c92',
    '350': '\u51b0\u7c92',
    '353': '\u5c0f\u96e8',
    '356': '\u4e2d\u96e8',
    '359': '\u5927\u96e8',
    '362': '\u51bb\u96e8',
    '365': '\u96e8\u5939\u96ea',
    '368': '\u5c0f\u96ea',
    '371': '\u5927\u96ea',
    '374': '\u51b0\u7c92',
    '377': '\u51b0\u7c92',
    '386': '\u96f7\u9635\u96e8',
    '389': '\u96f7\u66b4',
    '392': '\u96f7\u9635\u96e8',
    '395': '\u5927\u96ea',
}


WIND_DIR_MAP = {
    'N': '北风',
    'NNE': '北东北风',
    'NE': '东北风',
    'ENE': '东东北风',
    'E': '东风',
    'ESE': '东东南风',
    'SE': '东南风',
    'SSE': '东南偏南风',
    'S': '南风',
    'SSW': '西南偏南风',
    'SW': '西南风',
    'WSW': '西南偏西风',
    'W': '西风',
    'WNW': '西北偏西风',
    'NW': '西北风',
    'NNW': '西北偏北风',
}

WEEKDAY_NAMES = [
    '\u5468\u4e00',
    '\u5468\u4e8c',
    '\u5468\u4e09',
    '\u5468\u56db',
    '\u5468\u4e94',
    '\u5468\u516d',
    '\u5468\u65e5',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Weather helper for nanobot')
    parser.add_argument(
        'location',
        nargs='?',
        help='shenzhen_pingzhou / guangzhou_yayuncheng; omit to keep legacy auto mode',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='legacy auto mode only: force both locations even if commute rules would skip one',
    )
    return parser.parse_args()


def holiday_cache_path(year: int) -> str:
    return os.path.join(SCRIPT_DIR, f'holidays_{year}.json')


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def get_holidays(year: int) -> dict[str, Any]:
    cache_path = holiday_cache_path(year)
    if os.path.exists(cache_path):
        cached = load_json(cache_path, {})
        if isinstance(cached, dict):
            return cached

    try:
        response = requests.get(HOLIDAY_API.format(year=year), headers=REQUEST_HEADERS, timeout=5)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            save_json(cache_path, data)
            return data
    except Exception:
        pass

    return {}


def get_holiday_info(target_date: date) -> dict[str, Any]:
    holidays = get_holidays(target_date.year)
    info = holidays.get(target_date.isoformat(), {})
    return info if isinstance(info, dict) else {}


def is_shenzhen_day(target_date: date) -> bool:
    holiday_info = get_holiday_info(target_date)
    if holiday_info.get('holiday'):
        return False
    return target_date.weekday() < 5


def is_guangzhou_day(target_date: date) -> bool:
    holiday_info = get_holiday_info(target_date)
    if holiday_info.get('holiday'):
        return True
    if '\u8865\u73ed' in str(holiday_info.get('name', '')):
        return False
    return target_date.weekday() >= 4


def normalize_location(raw: str | None) -> str | None:
    if not raw:
        return None
    lowered = raw.strip().lower()
    for key, meta in LOCATIONS.items():
        aliases = {alias.lower() for alias in meta['aliases']}
        if lowered == key or lowered in aliases:
            return key
    return None


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def weather_text(code: Any, fallback: str = 'Unknown') -> str:
    return WEATHER_MAP.get(str(code), fallback or 'Unknown')


def hour_from_wttr_time(value: Any) -> int:
    raw = str(value or '0').strip()
    digits = ''.join(ch for ch in raw if ch.isdigit()) or '0'
    hour = safe_int(digits, 0) // 100
    return max(0, min(hour, 23))


def format_day_label(slot_date: date, today: date) -> str:
    delta = (slot_date - today).days
    if delta == 0:
        return '今天'
    if delta == 1:
        return '明天'
    if delta == 2:
        return '后天'
    return f'{slot_date.month:02d}-{slot_date.day:02d}'


def wind_direction_text(value: Any) -> str:
    raw = str(value or '-').strip().upper()
    if raw == '-' or not raw:
        return '-'
    zh = WIND_DIR_MAP.get(raw)
    return f'{zh}（{raw}）' if zh else raw


def uv_level_text(index: int) -> str:
    if index <= 2:
        return '低'
    if index <= 5:
        return '中等'
    if index <= 7:
        return '强'
    if index <= 10:
        return '很强'
    return '极强'


def format_advice_text(advice: list[str]) -> str:
    if not advice:
        return '无特殊提醒。'
    text = '；'.join(item.rstrip('。；; ') for item in advice)
    return text + '。'


def extract_weather_desc(block: dict[str, Any]) -> str:
    desc = block.get('weatherDesc') or []
    if desc and isinstance(desc, list):
        return str(desc[0].get('value', 'Unknown'))
    return 'Unknown'


def fetch_json_with_retry(url: str, timeout: int = 15, retries: int = 2) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1)
                continue
            raise
    if last_error:
        raise last_error
    return {}


def fetch_weather(location_key: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(LOCAL_TZ)
    meta = LOCATIONS[location_key]
    data = fetch_json_with_retry(WTTR_API.format(query=meta['query']))

    current = (data.get('current_condition') or [{}])[0]
    weather_days = data.get('weather') or []

    text = weather_text(current.get('weatherCode'), fallback=extract_weather_desc(current))

    hourly_items: list[str] = []
    rain_chances: list[int] = []
    for day_index, day in enumerate(weather_days[:3]):
        raw_date = day.get('date')
        try:
            day_date = date.fromisoformat(str(raw_date))
        except Exception:
            day_date = now.date() + timedelta(days=day_index)

        for block in day.get('hourly') or []:
            hour = hour_from_wttr_time(block.get('time'))
            slot_dt = datetime.combine(day_date, dt_time(hour=hour), tzinfo=LOCAL_TZ)
            if slot_dt <= now:
                continue

            chance = safe_int(block.get('chanceofrain'), 0)
            rain_chances.append(chance)
            hourly_text = weather_text(block.get('weatherCode'), fallback=extract_weather_desc(block))
            day_label = format_day_label(slot_dt.date(), now.date())
            detail = f"{day_label} {slot_dt:%H:%M} {block.get('tempC', '-')}℃ {hourly_text}"
            if chance > 0:
                detail += f"，降雨 {chance}%"
            hourly_items.append(detail)
            if len(hourly_items) >= 4:
                break

        if len(hourly_items) >= 4:
            break

    return {
        'label': meta['label'],
        'weather': text,
        'temp_c': safe_int(current.get('temp_C'), 0),
        'feels_like_c': safe_int(current.get('FeelsLikeC'), 0),
        'humidity': safe_int(current.get('humidity'), 0),
        'wind_kmph': safe_int(current.get('windspeedKmph'), 0),
        'wind_dir': wind_direction_text(current.get('winddir16Point')),
        'precip_mm': safe_float(current.get('precipMM'), 0.0),
        'uv_index': safe_int(current.get('uvIndex'), 0),
        'hourly_items': hourly_items,
        'rain_chance_max': max(rain_chances or [0]),
    }

def build_advice(weather: dict[str, Any]) -> list[str]:
    advice: list[str] = []
    temp = weather['temp_c']
    feels_like = weather['feels_like_c']
    humidity = weather['humidity']
    uv_index = weather['uv_index']
    wind_kmph = weather['wind_kmph']
    precip_mm = weather['precip_mm']
    rain_chance = weather['rain_chance_max']
    weather_text_zh = weather['weather']

    if temp < 15:
        advice.append('\u5929\u6c14\u504f\u51c9\uff0c\u8bb0\u5f97\u5e26\u5916\u5957')
    elif temp >= 30 or feels_like >= 33:
        advice.append('\u4f53\u611f\u504f\u70ed\uff0c\u6ce8\u610f\u9632\u6691\u8865\u6c34')
    else:
        advice.append('\u8f7b\u88c5\u51fa\u95e8\u5373\u53ef')

    if humidity >= 80:
        advice.append('\u6e7f\u5ea6\u8f83\u9ad8\uff0c\u4f53\u611f\u4f1a\u504f\u95f7')
    if rain_chance >= 50 or precip_mm > 0 or '\u96e8' in weather_text_zh:
        advice.append('\u5efa\u8bae\u968f\u8eab\u5e26\u4f1e')
    if uv_index >= 6:
        advice.append('\u7d2b\u5916\u7ebf\u504f\u5f3a\uff0c\u6ce8\u610f\u9632\u6652')
    if wind_kmph >= 25:
        advice.append('\u98ce\u6709\u70b9\u5927\uff0c\u51fa\u95e8\u6ce8\u610f')

    return advice


def build_report(location_key: str, target_date: date | None = None) -> str:
    now = datetime.now(LOCAL_TZ)
    target_date = target_date or now.date()
    weather = fetch_weather(location_key, now=now)
    advice = build_advice(weather)
    weekday = WEEKDAY_NAMES[target_date.weekday()]

    advice_text = format_advice_text(advice)
    lines = [
        f"{weather['label']}天气（{target_date.isoformat()} {weekday}）",
        f"天气：{weather['weather']}",
        f"温度：{weather['temp_c']}℃（体感 {weather['feels_like_c']}℃）",
        f"湿度：{weather['humidity']}%",
        f"风力：{weather['wind_kmph']} km/h（{weather['wind_dir']}）",
        f"降水：{weather['precip_mm']:.1f} mm",
        f"紫外线：{weather['uv_index']}（{uv_level_text(weather['uv_index'])}）",
        f"建议：{advice_text}",
    ]

    if weather['hourly_items']:
        lines.append('未来几个时段：')
        lines.extend(f'  {item}' for item in weather['hourly_items'])

    return "\n".join(lines)

def build_legacy_auto_report(force: bool = False) -> str:
    today = datetime.now(LOCAL_TZ).date()
    header = f"\u4eca\u5929\u662f {today.isoformat()}\uff08{WEEKDAY_NAMES[today.weekday()]}\uff09"
    sections = [header]

    if force or is_shenzhen_day(today):
        sections.append(build_report('shenzhen_pingzhou', today))
    else:
        sections.append('\u6df1\u5733\u576a\u6d32\uff1a\u4eca\u65e5\u65e0\u9700\u63a8\u9001')

    if force or is_guangzhou_day(today):
        sections.append(build_report('guangzhou_yayuncheng', today))
    else:
        sections.append('\u5e7f\u5dde\u4e9a\u8fd0\u57ce\uff1a\u4eca\u65e5\u65e0\u9700\u63a8\u9001')

    return "\n\n".join(sections)


def main() -> int:
    args = parse_args()
    location_key = normalize_location(args.location)

    try:
        if args.location and not location_key:
            valid = ', '.join(sorted(LOCATIONS))
            print(f"Unknown location: {args.location}. Valid values: {valid}")
            return 2

        if location_key:
            print(build_report(location_key))
        else:
            print(build_legacy_auto_report(force=args.force))
        return 0
    except requests.RequestException as exc:
        print(f"\u5929\u6c14\u67e5\u8be2\u5931\u8d25: {exc}")
        return 1
    except Exception as exc:
        print(f"\u5929\u6c14\u811a\u672c\u5f02\u5e38: {exc}")
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
