"""Constructed text vectors, not OCR or cross-layer acceptance evidence."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

from apps.wechat_ai_customer_service.adapters.text_correspondence import (
    business_comparison_text, field_value_text, historical_text_candidate,
    normalized_projection_text, protected_spans, single_edit,
)
from apps.wechat_ai_customer_service.adapters.message_viewport_projection import stable_business_content_signature


@pytest.mark.parametrize('old,new', [
    ('一般两厢，平时接送孩子', '般两厢，平时接送孩子'),
    ('平时接送孩子周末出去旅游', '平时接送孩纸周末出去旅游'),
    ('平时接送孩子周末出去旅游', '平时接送小孩子周末出去旅游'),
])
def test_one_edit_candidate_needs_no_invented_entity_coverage_flag(old, new):
    result = historical_text_candidate(old, new)
    assert result['eligible'] and result['matched_by'] == 'context_ocr', result
    assert result['edit_distance'] == 1 and result['similarity'] >= .9
    assert len(result['edits']) == 1
    assert result['canonical_text_sha256'] == hashlib.sha256(old.encode()).hexdigest()
    assert result['observed_text_sha256'] == hashlib.sha256(new.encode()).hexdigest()


@pytest.mark.parametrize('old,new,entities', [
    ('我想看看车然后带孩子出去旅行', '我看看车然后带孩子出去旅行', []),
    ('这辆车不能保证装下全部东西', '这辆车能保证装下全部东西', []),
    ('这辆车报价为8.8万请核对', '这辆车报价为88万请核对', []),
    ('这辆车报价为十万元请核对', '这辆车报价为九万元请核对', []),
    ('销售电话13900000001请核对', '销售电话13900000002请核对', []),
    ('这辆车是bz7型号请核对', '这辆车是bz8型号请核对', []),
    ('以后去南京和孩子一起旅行', '以后去南经和孩子一起旅行', ['南京']),
    ('客户王梓轩平时接送孩子', '客户王子轩平时接送孩子', ['王梓轩']),
    ('2026-09-18去看车请先联系', '2026-09-19去看车请先联系', []),
    ('温度显示-3度请仔细看看', '温度显示3度请仔细看看', []),
])
def test_protected_changes_cannot_be_accepted(old, new, entities):
    result = historical_text_candidate(old, new, protected_entities=entities)
    assert not result['eligible'] and result['protected_conflict'], result


@pytest.mark.parametrize('old,new', [('平时接送孩子', '平时接送孩纸'),
    ('平时接送孩子周末出去旅游', '平时接送孩纸周末出去旅油')])
def test_length_and_edit_limits(old, new):
    assert not historical_text_candidate(old, new)['eligible']


def test_general_adverb_is_not_quantity_and_longest_polarity_wins():
    assert not protected_spans('一般两厢，平时接送孩子')
    assert [s['text'] for s in protected_spans('一定')] == ['一定']
    assert [s['text'] for s in protected_spans('最多十万元以内')] == ['最多', '十万元', '以内']


@pytest.mark.parametrize('text', [' ＡｂＣ １２３\n“你好”…', '一般两厢，平时接送孩子',
    '8.8万', '88万', 'A\u200bB\ufe0f\ufe0e', '【测试】——…‥', '', '你好\r\n世界'])
def test_original_signature_semantics_and_utf8_are_unchanged(text):
    # Explicit old normalization and hashes are compatibility vectors, not
    # assertions that punctuation-only number changes are safe correspondence.
    import unicodedata, re
    from apps.wechat_ai_customer_service.adapters.message_viewport_projection import _OCR_PUNCTUATION_TRANSLATION
    old = unicodedata.normalize('NFKC', text).translate(_OCR_PUNCTUATION_TRANSLATION)
    old = ''.join(c for c in old if not c.isspace() and unicodedata.category(c) != 'Cf' and ord(c) not in {0xFE0E, 0xFE0F})
    old = re.sub(r'\.{2,}', '...', old).casefold()
    assert normalized_projection_text(text) == old
    expected = ''.join(c for c in old if not unicodedata.category(c).startswith('P'))
    assert business_comparison_text(text) == expected
    assert stable_business_content_signature({'row_kind': 'text_bubble', 'content_clean': text}) == hashlib.sha256(expected.encode()).hexdigest()


def test_field_value_does_not_apply_historical_ocr_normalization():
    assert field_value_text('  Ａ\r\nB  ') == 'A\nB'
    assert field_value_text('你好 顾客') != field_value_text('你好顾客')
    assert field_value_text('你好，顾客') != field_value_text('你好顾客')
    assert field_value_text('CJABCDEF') != field_value_text('cjabcdef')


def test_backend_can_load_same_rule_without_worker_or_optional_ocr_dependencies():
    path = Path(__file__).resolve().parents[1] / 'adapters/text_correspondence.py'
    spec = importlib.util.spec_from_file_location('backend_rule_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.historical_text_candidate('一般两厢，平时接送孩子', '般两厢，平时接送孩子')
    assert result['eligible']


def test_edit_offsets_and_empty_cases():
    assert single_edit('甲乙', '甲丙')['old_offset'] == 1
    assert single_edit('甲乙', '甲')['op'] == 'delete'
    assert single_edit('甲', '甲乙')['op'] == 'insert'
    assert single_edit('甲乙', '丙丁') is None
    assert single_edit('甲乙', '甲乙') is None


def test_known_and_unknown_name_have_the_explicitly_different_r7_boundary():
    old, new = '客户王梓轩平时接送孩子周末出游', '客户王子轩平时接送孩子周末出游'
    assert not historical_text_candidate(old, new, protected_entities=['王梓轩'])['eligible']
    # This is accepted correspondence risk, not a claim of equal semantics.
    assert historical_text_candidate(old, new, protected_entities=[])['eligible']


@pytest.mark.parametrize('old,new,entity,expected', [
    ('每天陪王梓轩接送孩子周末出游', '每天陪王小梓轩接送孩子周末出游', '王梓轩', False),
    ('每天陪王梓轩接送孩子周末出游', '每天陪小王梓轩接送孩子周末出游', '王梓轩', True),
    ('每天陪王梓轩接送孩子周末出游', '每天陪王梓轩呀接送孩子周末出游', '王梓轩', True),
    ('每天接送孩子周末出去旅游', '不每天接送孩子周末出去旅游', '', False),
    ('每天接送孩子周末出去旅游', '每天接送3孩子周末出去旅游', '', False),
])
def test_protected_entity_interior_and_boundary_insertions(old, new, entity, expected):
    assert historical_text_candidate(old, new, protected_entities=[entity])['eligible'] is expected
