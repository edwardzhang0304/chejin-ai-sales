"""Verify an owner-approved local engineering handoff without rerunning its tests.

The checked-in handoff contains hashes and test names only. It explicitly states
the original environment; CI attests identity, not a fresh business-test run.
"""
import hashlib
import json
from pathlib import Path
import re

from source_evidence import ROOT, REPO, git, relevant, require

VERSION_FILE = "worker-client/chejin_worker_client/__init__.py"
CONTRACT = "contracts/c2_contract_v3.json"
SCHEMA = "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/chejin_c2_observation_schema.generated.json"
PROVENANCE = "worker-client/omniauto-rpa/.chejin-source.json"


def normalized(name, raw):
    if name == VERSION_FILE:
        return re.sub(rb'__version__ = "[0-9]+\.[0-9]+\.[0-9]+"', b'__version__ = "RELEASE"', raw)
    if name in {CONTRACT, SCHEMA}:
        value = json.loads(raw)
        value.pop("contract_revision", None)
        if name == SCHEMA:
            value.pop("contract_sha256", None)
        return json.dumps(value, sort_keys=True).encode()
    return raw


def validate_provenance(old, new, version, contract_sha):
    a, b = json.loads(old), json.loads(new)
    current = b.pop("current_release")
    a.pop("current_release")
    require(current["version"] == current["contract_revision"] == version
            and current["contract_sha256"] == contract_sha, "HANDOFF_PROVENANCE_IDENTITY")
    old_entries = a.pop("selective_integrations")
    new_entries = b.pop("selective_integrations")
    require(new_entries[:len(old_entries)] == old_entries and len(new_entries) == len(old_entries) + 1,
            "HANDOFF_PROVENANCE_HISTORY_CHANGED")
    addition = new_entries[-1]
    require(addition["source_commit"] == current["source_commit"]
            and re.fullmatch(r"[0-9a-f]{40}", addition["source_commit"])
            and addition["scope"] == ["c2_contract_" + version.replace(".", "_") + "_generated_schema"],
            "HANDOFF_NOT_SCHEMA_ONLY_SOURCE")
    a.pop("notes", None); b.pop("notes", None)
    require(a == b, "HANDOFF_PROVENANCE_RUNTIME_CHANGED")


def validate(path):
    path = Path(path)
    require(path.parent == Path("ops/formal_release/handoffs") and path.suffix == ".json", "INVALID_HANDOFF_PATH")
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    base = data["reviewed_commit"]
    require(data["schema_version"] == 1 and data["repository"] == REPO and data.get("approval")
            and re.fullmatch(r"[0-9a-f]{40}", base), "UNAPPROVED_HANDOFF")
    git("merge-base", "--is-ancestor", base, "HEAD")
    for name, expected in data["source_sha256"].items():
        require(hashlib.sha256(git("show", base + ":" + name)).hexdigest() == expected, "HANDOFF_REVIEWED_HASH_MISMATCH")
    version = data["target_version"]
    contract = json.loads(git("show", "HEAD:" + CONTRACT))
    require(contract["contract_revision"] == version, "HANDOFF_TARGET_VERSION_MISMATCH")
    contract_sha = hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    schema = json.loads(git("show", "HEAD:" + SCHEMA))
    require(schema["contract_revision"] == version and schema["contract_sha256"] == contract_sha,
            "HANDOFF_GENERATED_SCHEMA_MISMATCH")
    require(git("show", "HEAD:" + VERSION_FILE).decode().strip() == '__version__ = "' + version + '"',
            "HANDOFF_APPLICATION_VERSION_MISMATCH")
    for name in git("diff", "--name-only", "-z", base, "HEAD").decode().split("\0"):
        if not name:
            continue
        if not relevant(name, "source"):
            continue
        old, new = git("show", base + ":" + name), git("show", "HEAD:" + name)
        if name == PROVENANCE:
            validate_provenance(old, new, version, contract_sha)
        else:
            require(normalized(name, old) == normalized(name, new), "HANDOFF_BUSINESS_OR_DEPENDENCY_CHANGED: " + name)
    total = 0
    for check in data["checks"]:
        require(re.fullmatch(r"[0-9a-f]{64}", check["original_xml_sha256"])
                and check["count"] == len(check["cases"]) > 0
                and all(c["result"] == "passed" and c["name"] for c in check["cases"]), "HANDOFF_RESULTS_NOT_PASSED")
        total += check["count"]
    require(total > 0, "EMPTY_HANDOFF")
    return {"reviewed_commit": base, "checks_reused": total, "checks_reexecuted": 0,
            "handoff_sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), "environment": data["environment"]}
