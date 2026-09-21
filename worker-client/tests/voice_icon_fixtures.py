"""Controlled desktop fixtures with the real reference glyph, not blank bubbles."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps

FIXTURES = Path(__file__).parent/'fixtures/voice_icons'


def draw_voice(image, bounds, role='customer'):
    left,top,right,bottom=bounds
    colour=(140,226,146) if role=='self' else (232,232,234)
    ImageDraw.Draw(image).rounded_rectangle(bounds,radius=8,fill=colour)
    glyph=Image.open(FIXTURES/'customer_9s.png').convert('L').crop((47,33,77,71))
    if role=='self':glyph=ImageOps.mirror(glyph)
    h=round((bottom-top)*.64);w=round(h*30/38)
    glyph=glyph.resize((w,h))
    # Preserve the reference anti-aliasing, blend only ink into the chosen bubble.
    mask=glyph.point(lambda value:max(0,round((238-value)*255/213)))
    x=right-8-w if role=='self' else left+8
    image.paste(Image.new('RGB',(w,h),(25,25,25)),(x,top+round((bottom-top-h)/2)),mask)


def classified_duration(item):
    """Input of geometry-only unit tests AFTER classification; not pixel proof."""
    return {**item, '_voice_visual_evidence': {
        'state':'confirmed','duration_bounds':[[item[k] for k in ('left','top','right','bottom')]],
    }}
