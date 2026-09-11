"""Reuse only successful cases of trusted Windows run 34615585004; recovery never reused."""
import argparse,hashlib,json,shutil
from pathlib import Path
from candidate import source_inputs
from source_evidence import git
SOURCE='64bb0a1dc07b1b4d231f9813ab2205ebd03af869'
HASHES={'chejin-manual-install/upgrade-result.json': '58d198c5b0c4bf8dca99f6216a35d0a2f0bcb50bc57dbcfe680d16cd8a0840e7', 'chejin-updater-real-process/success/control/update-result.json': '3402bea03f1c90deeaf56a6e60f7f41b3cb1f4cd121a6ddff040a21706860794', 'chejin-updater-real-process/rollback/control/update-result.json': 'dc11d98cc9d89d800be8310ae804c0b0d4faf9b3eb2784c86a9232bbd12778a5', 'chejin-updater-real-process/formal-client/control/update-result.json': '890a359c271a83f5d3d77d78488dca0256a69966911d5399ee879f15dd71eef6'}

def validate(folder, target):
    head=git('rev-parse','HEAD').decode().strip()
    assert source_inputs(SOURCE)==source_inputs(head), 'Candidate runtime/build changed'
    for name in ('worker-client/scripts/run-windows-client-upgrade-test.py','worker-client/scripts/run-windows-updater-process-test.ps1'):
        assert git('show',SOURCE+':'+name)==git('show',head+':'+name), 'Passed acceptance harness changed'
    for rel,want in HASHES.items():
        p=Path(folder)/'_temp'/rel
        assert hashlib.sha256(p.read_bytes()).hexdigest()==want, 'Prior report identity mismatch'
    for rel in HASHES:
        dst=Path(target)/rel; dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(Path(folder)/'_temp'/rel,dst)
    return {'run':'34615585004','manual_install_cases_reused':2,'process_cases_reused':3,'cases_reexecuted':0,'pending_read_recovery_reused':False,'report_hashes':HASHES}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--folder',type=Path,required=True);p.add_argument('--target',type=Path,required=True);a=p.parse_args()
    result=validate(a.folder,a.target);(a.target/'reused-completed-cases.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
