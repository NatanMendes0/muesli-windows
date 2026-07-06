"""Generates the Muesli app icon at runtime with Pillow: a warm bowl on a cream tile."""
from PIL import Image, ImageDraw


def make_icon(size: int = 64) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded cream tile
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 5, fill=(247, 243, 234, 255))
    # Bowl
    bowl = (196, 121, 58, 255)
    d.pieslice([s * 0.16, s * 0.28, s * 0.84, s * 0.92], 0, 180, fill=bowl)
    d.rectangle([s * 0.16, s * 0.55, s * 0.84, s * 0.62], fill=bowl)
    # Grains above the bowl
    grain = (122, 94, 58, 255)
    for cx, cy, r in [(0.34, 0.36, 0.05), (0.5, 0.30, 0.055), (0.66, 0.36, 0.05)]:
        d.ellipse([s * (cx - r), s * (cy - r), s * (cx + r), s * (cy + r)], fill=grain)
    return img


def save_ico(path: str):
    img = make_icon(256)
    img.save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
