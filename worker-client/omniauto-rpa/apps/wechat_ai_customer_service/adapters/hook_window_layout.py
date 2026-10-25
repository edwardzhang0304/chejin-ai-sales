"""Shared Hook desktop dimensions, in logical pixels until the native boundary."""
import math

WECHAT_SIZE = (1060, 740)
WORKER_SIZE = (380, 720)
GAP = 12
MARGIN = 12


def window_pair_plan(width, height, dpi_scale=1.0):
    width, height, scale = float(width), float(height), float(dpi_scale)
    if not all(math.isfinite(v) and v > 0 for v in (width, height, scale)):
        raise ValueError('invalid desktop geometry')
    margin, gap = round(MARGIN * scale), round(GAP * scale)
    fit = max(0, min(1.0,
        (width - 2 * margin - gap) / ((WECHAT_SIZE[0] + WORKER_SIZE[0]) * scale),
        (height - 2 * margin) / (max(WECHAT_SIZE[1], WORKER_SIZE[1]) * scale)))
    def size(base):
        return tuple(max(1, int(v * scale * fit)) for v in base)
    return {'wechat': size(WECHAT_SIZE), 'worker': size(WORKER_SIZE),
            'margin': margin, 'gap': gap, 'fit_scale': fit}
