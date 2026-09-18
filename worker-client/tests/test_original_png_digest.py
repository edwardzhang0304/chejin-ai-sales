"""Capture bytes, RGB pixels and correction-time hashes are distinct facts."""
import hashlib
from PIL import Image

from chejin_worker_client.shared_rules import historical_text_correction

captured_png_evidence = historical_text_correction.captured_png_evidence
from chejin_worker_client.wechat_c2 import _original_png_evidence


def test_first_capture_commits_png_bytes_and_rejects_another_frame(tmp_path):
    image = Image.new('RGB', (5, 7), (22, 44, 66))
    path = tmp_path/'capture.png'; image.save(path)
    rgb_hash = hashlib.sha256(image.tobytes()).hexdigest()
    png_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert rgb_hash != png_hash
    captured = captured_png_evidence(str(path), raw_rgb_sha256=rgb_hash)
    assert captured['state'] == 'captured' and captured['sha256'] == png_hash
    payload = {'screenshot_path': str(path), 'frame_observation': {
        'screenshot_path': str(path), 'screenshot_sha256': rgb_hash, 'png_byte_evidence': captured}}
    saved = _original_png_evidence(payload)
    assert saved['screenshot_sha256'] == png_hash
    assert saved['screenshot_digest_recorded_at'] == captured['digest_recorded_at']
    image.putpixel((0, 0), (0, 0, 0)); image.save(path)
    assert captured_png_evidence(str(path), raw_rgb_sha256=rgb_hash)['state'] == 'unavailable'
    payload['screenshot_path'] = str(tmp_path/'other.png')
    assert _original_png_evidence(payload) == {'screenshot_digest_provenance': 'capture_unavailable'}


def test_pixel_digest_or_missing_png_cannot_be_a_byte_commitment(tmp_path):
    assert captured_png_evidence(str(tmp_path/'missing.png'), raw_rgb_sha256='a'*64)['state'] == 'unavailable'
    assert _original_png_evidence({'screenshot_path': 'old.png', 'frame_observation': {
        'screenshot_path': 'old.png', 'screenshot_sha256': 'a'*64}}) == {'screenshot_digest_provenance': 'capture_unavailable'}
