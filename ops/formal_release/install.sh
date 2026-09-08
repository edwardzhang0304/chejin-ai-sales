#!/bin/sh
# Run from a reviewed bundle on the production host; no runtime configuration edits.
# Required bundle: receiver.py verify.py register.py, baseline/<version>/chejin_worker_client,
# stage.pub, promote.pub, and trusted-public-keys.json copied from existing trust.
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=/opt/chejin-formal-release
user=chejin-release
test "$(id -u)" = 0
test ! -e "$target"
test ! -e /etc/chejin-formal-release.json
test ! -e /etc/sudoers.d/chejin-formal-release
if getent passwd "$user" >/dev/null; then
  echo 'Dedicated account already exists; inspect before installing.' >&2
  exit 1
fi
python3 -c 'import cryptography'
for file in receiver.py verify.py register.py disable.sh stage.pub promote.pub trusted-public-keys.json; do
  test -f "$bundle/$file"
done
python3 - "$bundle" <<'PY'
import json, re, sys
from pathlib import Path
root=Path(sys.argv[1])
for name in ('stage.pub','promote.pub'):
    assert re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?\n?', (root/name).read_text())
assert json.loads((root/'trusted-public-keys.json').read_text())['keys']
for version in ('0.9.69',):
    for file in ('__init__.py','models.py','release_package_contract.py'):
        assert (root/'baseline'/version/'chejin_worker_client'/file).is_file()
PY
install -d -m 755 "$target"
install -m 700 "$bundle/disable.sh" "$target/disable.sh"
install -m 644 "$bundle/receiver.py" "$bundle/verify.py" "$bundle/register.py" "$target/"
cp -R "$bundle/baseline" "$target/baseline"
chown -R root:root "$target"
chmod -R go-w "$target"
install -m 644 "$bundle/trusted-public-keys.json" "$target/public-keys.json"
install -d -m 700 /var/lib/chejin-formal-staging
useradd --system --create-home --home-dir /var/lib/chejin-release --shell /bin/sh "$user"
# An unlocked account is needed for public-key auth; this non-hash cannot authenticate a password.
usermod --password '*' "$user"
install -d -m 755 -o root -g root /var/lib/chejin-release
install -d -m 700 -o "$user" -g "$user" /var/lib/chejin-release/.ssh
{
  printf 'restrict,command="/usr/bin/sudo -n /usr/bin/python3 %s/receiver.py stage" ' "$target"
  cat "$bundle/stage.pub"
  printf '\nrestrict,command="/usr/bin/sudo -n /usr/bin/python3 %s/receiver.py promote" ' "$target"
  cat "$bundle/promote.pub"
} > /var/lib/chejin-release/.ssh/authorized_keys
chown root:root /var/lib/chejin-release/.ssh /var/lib/chejin-release/.ssh/authorized_keys
chmod 755 /var/lib/chejin-release/.ssh
chmod 644 /var/lib/chejin-release/.ssh/authorized_keys
sudo_file=$(mktemp)
printf '%s ALL=(root) NOPASSWD: /usr/bin/python3 %s/receiver.py stage, /usr/bin/python3 %s/receiver.py promote\n' "$user" "$target" "$target" > "$sudo_file"
visudo -cf "$sudo_file"
install -m 440 "$sudo_file" /etc/sudoers.d/chejin-formal-release
rm -f "$sudo_file"
python3 - <<'PY'
import json
from pathlib import Path
config={'staging_root':'/var/lib/chejin-formal-staging','container':'chejin-leads-api',
        'public_keys':'/opt/chejin-formal-release/public-keys.json',
        'client_baselines':{v:'/opt/chejin-formal-release/baseline/'+v for v in ('0.9.69',)},
        'api_origin':'https://jiangsuchejin.com/api','staging_limit_bytes':4*1024**3}
p=Path('/etc/chejin-formal-release.json');p.write_text(json.dumps(config)+'\n');p.chmod(0o600)
PY
python3 -m py_compile "$target/receiver.py" "$target/verify.py" "$target/register.py"
visudo -c >/dev/null
echo 'FORMAL_RECEIVER_INSTALLED'
