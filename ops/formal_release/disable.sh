#!/bin/sh
# Disable only the newly added receiver. Preserve staged artifacts and audit records.
set -eu
test "$(id -u)" = 0
test "${1:-}" = --confirm-disable-formal-receiver
keys=/var/lib/chejin-release/.ssh/authorized_keys
if test -f "$keys"; then
  mv "$keys" "$keys.disabled"
fi
rm -f /etc/sudoers.d/chejin-formal-release
usermod --lock chejin-release
visudo -c >/dev/null
echo 'FORMAL_RECEIVER_DISABLED: existing applications and staged evidence preserved.'
