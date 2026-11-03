# -*- coding: utf-8 -*-
"""生成桌面应用图标（app.ico / app.png）——纯 Pillow 绘制，不引用外部素材。

用法： E:\\AiLive\\env\\python.exe E:\\AiLive\\app\\make_icon.py
产物： app.ico（16/24/32/48/64/128/256 多尺寸）、app.png（256，给文档用）
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BRAND = (56, 189, 248)          # #38bdf8 浅蓝（主色）
BRAND_DEEP = (14, 165, 233)     # #0ea5e9
INK = (6, 42, 61)               # #062a3d


def rounded_gradient(size: int) -> Image.Image:
    """圆角方块 + 斜向渐变（用逐像素画，保证各尺寸都清晰）。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / max(1, (size - 1) * 2)
            px[x, y] = (int(BRAND[0] + (BRAND_DEEP[0] - BRAND[0]) * t),
                        int(BRAND[1] + (BRAND_DEEP[1] - BRAND[1]) * t),
                        int(BRAND[2] + (BRAND_DEEP[2] - BRAND[2]) * t), 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.24), fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def draw_glyph(size: int) -> Image.Image:
    """闪电（与界面 logo 的图标一致），用多边形绘制。"""
    img = rounded_gradient(size)
    d = ImageDraw.Draw(img)
    s = size
    pts = [(0.585, 0.14), (0.255, 0.555), (0.475, 0.555), (0.415, 0.86), (0.745, 0.445), (0.525, 0.445)]
    poly = [(x * s, y * s) for x, y in pts]
    d.polygon(poly, fill=INK)
    return img


def main() -> int:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw_glyph(n) for n in sizes]
    ico = HERE / "app.ico"
    imgs[-1].save(ico, format="ICO", sizes=[(n, n) for n in sizes])
    png = HERE / "app.png"
    imgs[-1].save(png, format="PNG")
    print("已生成：%s（%d 字节，%d 个尺寸）" % (ico, ico.stat().st_size, len(sizes)))
    print("已生成：%s（%d 字节）" % (png, png.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())