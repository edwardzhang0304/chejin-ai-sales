"""Extract inline formal PowerShell for the independent Windows parser (never execute it)."""
import argparse
from pathlib import Path
import re
import yaml


def extract(workflow, output):
    data = yaml.load(Path(workflow).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for job_id, job in data['jobs'].items():
        for index, step in enumerate(job.get('steps', [])):
            shell = step.get('shell', job.get('defaults', {}).get('run', {}).get('shell', ''))
            if shell in ('powershell', 'pwsh') and 'run' in step:
                # GitHub expressions are not PowerShell. Substitute data tokens for syntax parsing only.
                script = re.sub(r'\$\{\{.*?\}\}', 'CI_VALUE', step['run'], flags=re.S)
                path = output / f'{job_id}-{index}.ps1'
                path.write_text(script, encoding='utf-8-sig')
                paths.append(path)
    if not paths:
        raise ValueError('NO_POWERSHELL_STEPS_FOUND')
    return paths


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workflow', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(f'Extracted {len(extract(args.workflow, args.output))} PowerShell steps')
