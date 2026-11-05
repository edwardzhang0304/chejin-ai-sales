"""Certainty boundary for text findings, shared by all prepared-reply consumers.

Heuristic extraction is useful reviewer context, not proof that its guessed
subject/scope is correct. Only explicit local assertions may become hard errors.
"""
import re


def classify_fact_findings(plan, products, receipt, validation):
    segments = plan.get('reply_segments') or []
    errors = validation['errors']
    hard = [e for e in errors if e == 'model_declared_fact_conflict']
    names = {}
    for product in products:
        name = product.get('name')
        if name:
            names.setdefault(name, []).append(str(product['id']))
    by_id = {str(p['id']): p for p in products}
    certain_conflicts = []
    explicit_price_spans = []
    for occurrence in validation['occurrences']:
        if occurrence.get('pending_policy_review'):
            continue
        segment = segments[occurrence['segment_index']]
        start, end = occurrence['start'], occurrence['end']
        product = by_id.get(occurrence.get('product_id'))
        certain = False
        explicit_price_span = None
        if product and occurrence.get('expected') is not None:
            name = product.get('name', '')
            pos = segment.rfind(name, 0, start) if name else -1
            # Full unique name, explicit immediate predicate and precise value.
            # Approximation, comparison, pronouns, shared subjects and prose
            # between the entity and number are left to the existing reviewer.
            fields = {'price': r'(?:的)?(?:实际)?(?:报价|售价|价格|车价)?(?:为|是)?',
                      'mileage_km': r'(?:的)?(?:表显里程|里程)(?:为|是)?',
                      'displacement': r'(?:的)?排量(?:为|是)?',
                      'stock': r'(?:的)?库存(?:为|是|有)?'}
            predicate = fields.get(occurrence.get('role'))
            if pos >= 0 and names.get(name) == [str(product['id'])] and predicate:
                bridge = segment[pos + len(name):start]
                tail = segment[end:]
                prefix = re.split(r'[。；;！？!?，,]', segment[:pos])[-1]
                certain = bool(not prefix.strip()
                               and re.fullmatch(r'\s*[，,:：]?\s*' + predicate + r'\s*[:：]?\s*', bridge)
                               and (not tail or re.match(r'[。；;，,！!？?\s]', tail)))
                if certain and occurrence.get('role') == 'price':
                    explicit_price_span = (occurrence['segment_index'],pos,end)
        if not certain and occurrence.get('unit') == '万元' and receipt.get('scan_complete'):
            from apps.wechat_ai_customer_service.adapters.vehicle_fields import public_number
            clause = re.split(r'[。；;！？!?，,]', segment[:start])[-1]
            # An explicit exact asking price absent from *every* candidate is
            # false independently of which preceding car the pronoun meant.
            certain = bool(products and re.fullmatch(r'(?:实际)?(?:报价|售价|车价)(?:为|是)?\s*', clause)
                and (end == len(segment) or re.match(r'[。；;，,！!？?\s]', segment[end:]))
                and all(p.get('price') is not None for p in products)
                and public_number(occurrence['value']) not in {public_number(p['price']) for p in products})
        if not occurrence.get('verified') and occurrence.get('unit') == '万元':
            from apps.wechat_ai_customer_service.workflows.catalog_budget import amount_role
            role = amount_role(segment, start)
            if role in {'服务费','手续费','定金','订金','首付','月供','利息','利率'}:
                # A seller's explicit payment term needs policy authority;
                # customer preferences have already been verified separately.
                clause = re.split(r'[。；;！？!?，,]', segment[:start])[-1]
                for name, ids in names.items():
                    if len(ids) == 1 and clause.startswith(name):
                        clause = clause[len(name):]
                        break
                if re.fullmatch(r'(?:预约|实际|本车|该车)?' + role + r'(?:为|是)?\s*', clause):
                    certain = True
        if occurrence.get('role') in {'stock_count', 'match_count', 'known_condition_match_count', 'scanned_count'}:
            # Explicit whole-scope totals have a program-owned count. Local
            # demonstratives (这两台) and model-year suffixes do not fit this.
            clause = re.split(r'[。；;！？!?，,]', segment[:start])[-1]
            certain = bool(occurrence.get('expected') is not None
                and re.fullmatch(r'(?:全店|库存|本店库存|当前库存|本次查询结果)(?:一共|总共|共有|有|共计|共)', clause)
                and re.fullmatch(r'[0-9０-９零一二两三四五六七八九十百]+(?:台|辆)', occurrence['text']))
        sentence_end = re.search(r'[。；;！？!?]|$', segment[end:])
        if sentence_end.group() in {'？','?'}:
            certain = False
        if certain and explicit_price_span is not None:
            explicit_price_spans.append(explicit_price_span)
        if occurrence.get('verified'):
            # A coincidentally matching value does not establish its role.
            # In particular, normalized measure units must agree with the
            # original surface unit; display scope cannot default to all cars.
            uncertain = (occurrence.get('role') == 'display_count'
                         or occurrence.get('product_id') and not certain)
            occurrence['disposition'] = 'semantic_pending' if uncertain else 'verified'
            continue
        occurrence['disposition'] = 'confirmed_conflict' if certain else 'semantic_pending'
        if certain:
            certain_conflicts.append(occurrence)
    if certain_conflicts:
        hard.append('confirmed_public_fact_conflict')
    if 'reply_recommendation_order_mismatch' in errors and explicit_price_spans:
        # A list consisting solely of explicit full-name/price assertions has
        # an observable order. Prose, aliases and references remain semantic.
        remainder = [list(s) for s in segments]
        for index, start, end in explicit_price_spans:
            remainder[index][start:end] = [' '] * (end-start)
        if all(re.fullmatch(r'[\s，,。；;]*', ''.join(s)) for s in remainder):
            hard.append('reply_recommendation_order_mismatch')
    pending = [e for e in errors if e not in hard]
    if any(o.get('disposition') == 'semantic_pending' for o in validation['occurrences']):
        pending.append('numeric_role_or_scope_unconfirmed')
    return {'hard_errors': list(dict.fromkeys(hard)),
            'semantic_findings': pending,
            'confirmed_conflicts': certain_conflicts,
            'decision': 'conflict' if hard else 'semantic_pending' if pending else 'verified'}
