"""Reuse exact Windows native checks from run 34612064011, not its failed process probe.

The pinned XML was independently fetched from the trusted run and has 52 passes.
Any subsequent business/dependency change invalidates this one-release reuse.
"""
import argparse,hashlib,json
from pathlib import Path
import xml.etree.ElementTree as ET
from source_evidence import git,relevant

def validate(path):
    raw=Path(path).read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='4db4924964fd958da1038ce3a49874af77d6c729a4d17f0772f6d28e99cef59a', 'Native XML identity mismatch'
    base='c6d74d7f40f92266b86a71d2d987c0b418e59447'
    git('merge-base','--is-ancestor',base,'HEAD')
    changed=[p for p in git('diff','--name-only','-z',base,'HEAD').decode().split('\0') if p and relevant(p,'source')]
    assert not changed, 'Native source/dependencies changed: '+str(changed)
    cases=ET.fromstring(raw).findall('.//testcase')
    assert len(cases)==52 and all(not any(c.find(k) is not None for k in ('failure','error','skipped')) for c in cases)
    return {'native_checks_reused':52,'native_checks_reexecuted':0,'source_commit':base,'evidence_run':'34612064011','xml_sha256':'4db4924964fd958da1038ce3a49874af77d6c729a4d17f0772f6d28e99cef59a'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--report',required=True);a=p.parse_args();print(json.dumps(validate(a.report)))
