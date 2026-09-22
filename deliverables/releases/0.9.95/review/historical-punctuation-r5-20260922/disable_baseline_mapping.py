"""Negative control: restore the old assumption that both old indexes coincide."""
def pytest_configure(config):
    from chejin_worker_client.shared_rules import historical_text_alignment
    historical_text_alignment._baseline_indexes = lambda *args: None


def pytest_collection_modifyitems(items):
    seen = set()
    for item in items:
        module = item.module
        if module.__name__ != 'test_historical_confidence_worker_http' or id(module) in seen:
            continue
        original = module.worker_source
        def source(original=original):
            value = original()
            anchor = 'bridge=Wechat()'
            assert value.count(anchor) == 1
            return value.replace(anchor, '''from apps.wechat_ai_customer_service.adapters import historical_text_alignment
historical_text_alignment._baseline_indexes=lambda *args:None
bridge=Wechat()''')
        module.worker_source = source
        seen.add(id(module))
