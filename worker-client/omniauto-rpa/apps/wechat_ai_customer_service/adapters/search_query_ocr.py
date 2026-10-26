"""Keep verified search chrome out of OCR, without modifying text pixels.

The original screenshot remains evidence. Only template-proven icon rectangles
in the cached input are excluded in the OCR copy; all coordinates stay unchanged.
Unrecognized chrome is left untouched, so exact query verification fails closed.
"""
import numpy as np
from PIL import Image
from .search_control_icons import empty_references


def prepare_search_query_ocr(image, bounds):
    if bounds is None:
        return image, []
    left, top, right, bottom = bounds
    height = bottom - top
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        return image, []
    if not 22 <= height <= 64 or not 4 * height <= right - left <= 14 * height:
        return image, []
    excluded = []
    # Scale only existing neutral references, never observed text. Startup has
    # already located this control; compare fixed edge strips without relocating.
    for reference in empty_references():
        scaled = reference.resize((round(reference.width * height / reference.height), height),
                                  Image.Resampling.BILINEAR).convert('L')
        icon_width = round(round(reference.height * .8) * height / reference.height)
        for rect, template_rect in (
            ([left, top, left + icon_width, bottom], (0, 0, icon_width, height)),
            ([right - height, top, right, bottom], (scaled.width - height, 0, scaled.width, height)),
        ):
            if any(rect[0] < old[2] and old[0] < rect[2] for old in excluded):
                continue
            pixels = np.asarray(image.crop(rect).convert('L'), dtype=float)
            template = np.asarray(scaled.crop(template_rect), dtype=float)
            # Correlation alone could hide an extra glyph in template whitespace.
            if np.max(np.abs(pixels - template)) <= 18:
                excluded.append(rect)
    if not excluded:
        return image, []
    prepared = image.copy()
    for rect in excluded:
        prepared.paste((255, 255, 255), tuple(rect))
    return prepared, excluded
