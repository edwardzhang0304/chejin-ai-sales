"""Neutral search-control templates and strict native-resolution empty proof.

References contain only input chrome and icons. No text, account or customer
pixels. Never resize observed pixels: even one residual glyph must fail proof.
"""
from functools import lru_cache
import base64
import io
import numpy as np
from PIL import Image
from .icon_similarity import icon_candidates

_EMPTY_REFERENCES = ('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')

@lru_cache(maxsize=1)
def empty_references():
    return tuple(Image.open(io.BytesIO(base64.b64decode(p))).convert("RGB") for p in _EMPTY_REFERENCES)


@lru_cache(maxsize=1)
def clear_templates():
    sources = [ref.crop((ref.width-ref.height, 0, ref.width, ref.height)).convert("L") for ref in empty_references()]
    return tuple((scale, np.asarray(ref.resize((round(ref.width*scale), round(ref.height*scale)), Image.Resampling.BILINEAR), dtype=np.float64))
                 for ref in sources for scale in (.8, 1., 1.25, 1.5, 2.))


def clear_icon_candidates(image, *, search_bounds):
    return icon_candidates(image, search_bounds=search_bounds, templates=clear_templates(),
                           source="vision_clear_icon", method="clear_template")


def empty_pixel_error(image):
    """Return None only for a complete empty control, else a stable error code."""
    import cv2
    a = np.asarray(image.convert("RGB"), dtype=int)
    height, width = a.shape[:2]
    if not 22 <= height <= 64 or not 4*height <= width <= 14*height:
        return "SEARCH_EMPTY_STYLE_UNSUPPORTED"
    possible_caret = False
    for ref_image in empty_references():
        # Scale reference chrome to native input height; keep input untouched.
        ref = np.asarray(ref_image.resize((round(ref_image.width*height/ref_image.height), height), Image.Resampling.BILINEAR),dtype=int)
        expected = np.repeat(ref[:,ref.shape[1]//2:ref.shape[1]//2+1,:],width,axis=1)
        expected[:,:height,:] = ref[:,:height,:]
        expected[:,-height:,:] = ref[:,-height:,:]
        different = np.max(np.abs(a-expected),axis=2)>18
        if not different.any():
            return None
        count,_,stats,_=cv2.connectedComponentsWithStats(different.astype('uint8'),8)
        if count == 2:
            x,y,w,h,area=map(int,stats[1]); scale=height/31
            if 24*scale<=x<=30*scale and w<=max(2,2*scale) and 4*scale<=y and y+h<=27*scale and 10*scale<=h<=23*scale and area>=h:
                possible_caret = True
    return "SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED" if possible_caret else "SEARCH_QUERY_OR_STYLE_REMAINS"
