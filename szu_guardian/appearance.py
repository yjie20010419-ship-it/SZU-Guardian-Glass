from __future__ import annotations
import colorsys
import json
import re
from pathlib import Path
DEFAULT_COLOR = '#A8C7FA'
PRESETS = [('雾霾蓝', '#A8C7FA'), ('薄荷绿', '#ADE5D1'), ('浅薰衣草', '#CBB9F3'), ('蜜桃粉', '#F3BECD'), ('奶油杏', '#F2D6A5')]

def normalize_color(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch('#[0-9a-fA-F]{6}', value.strip()):
        raise ValueError('请输入六位颜色值，例如 #A8C7FA')
    return value.strip().upper()

def accent_color(value: str) -> str:
    value = normalize_color(value)
    rgb = tuple((int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)))
    h, l, s = colorsys.rgb_to_hls(*rgb)
    lightness = 0.47
    while True:
        rgb = colorsys.hls_to_rgb(h, lightness, max(0.70, min(s, 0.95)))
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        luminance = sum((c * w for c, w in zip(linear, [0.2126, 0.7152, 0.0722])))
        if 1.05 / (luminance + 0.05) >= 4.65 or lightness <= 0.1:
            return '#%02X%02X%02X' % tuple((round(v * 255) for v in rgb))
        lightness -= 0.01

class ThemeStore:

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> str:
        try:
            return normalize_color(json.loads(self.path.read_text(encoding='utf-8'))['color'])
        except (OSError, ValueError, KeyError, TypeError):
            return DEFAULT_COLOR

    def save(self, color: str) -> None:
        color = normalize_color(color)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'color': color}), encoding='utf-8')
        temporary.replace(self.path)
