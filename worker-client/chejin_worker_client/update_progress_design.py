"""Presentation only: shared layout for native painting and offline visual QA."""
from pathlib import Path

WIDTH, HEIGHT = 400, 300
BACKGROUND = '#fbfdff'
INK, MUTED, BLUE = '#111a2b', '#748198', '#347bfa'
BRAND_PATH = Path(__file__).with_name('web_assets') / 'update-brand.bmp'


def window_bounds(work_area, dpi, anchor=None):
    left, top, right, bottom = work_area
    scale = min(max(dpi / 96, 1), (right-left) / WIDTH, (bottom-top) / HEIGHT)
    width, height = max(1, round(WIDTH*scale)), max(1, round(HEIGHT*scale))
    x,y = anchor or (left + (right-left-width)//2, top + (bottom-top-height)//2)
    return max(left,min(x,right-width)), max(top,min(y,bottom-height)), width, height


def paint(canvas, title, seconds, angle):
    """Coordinates are logical pixels; the adapter supplies raster scaling."""
    canvas.fill((0, 0, WIDTH, HEIGHT), BACKGROUND)
    # Opaque, very subtle cool tint: no desktop alpha/compositor dependency.
    for y in range(35, HEIGHT, 8):
        t = (y-35)/(HEIGHT-35)
        rgb = tuple(round(a+(b-a)*t) for a,b in zip((251,253,255),(243,248,254)))
        canvas.fill((0, y, WIDTH, min(8, HEIGHT-y)), '#' + ''.join(f'{v:02x}' for v in rgb))
    canvas.fill((0, 0, WIDTH, 34), '#f1f6fc')
    canvas.line((1, 34, WIDTH-1, 34), '#e5ebf4', 0.6)
    canvas.image((14, 7, 20, 20), BRAND_PATH)
    canvas.text((43, 4, 300, 27), '车金 Worker 更新', 14, INK, False, False)
    canvas.line((373, 12, 383, 22), MUTED, 1.1)
    canvas.line((383, 12, 373, 22), MUTED, 1.1)
    canvas.arc((179, 105, 42, 42), '#e4efff', 5, 0, 360)
    # Activity indicator, deliberately no percentage or fake completion estimate.
    for i in range(48):
        rgb = tuple(round(a+(b-a)*i/47) for a,b in zip((52,123,250),(220,235,255)))
        canvas.arc((179, 105, 42, 42), '#' + ''.join(f'{v:02x}' for v in rgb),
                   5, angle-i*6.75, -7.75)
    canvas.text((22, 164, 356, 44), title, 20, INK, True, True)
    canvas.text((22, 207, 356, 24), f'已等待 {seconds} 秒', 14, MUTED, False, True)
    for ends in ((14,1,386,1),(14,299,386,299),(1,14,1,286),(399,14,399,286)):
        canvas.line(ends, '#ffffff', 1)
    for box,start in (((1,1,26,26),180),((373,1,26,26),270),
                      ((373,273,26,26),0),((1,273,26,26),90)):
        canvas.arc(box, '#ffffff', 1, start, 90)
