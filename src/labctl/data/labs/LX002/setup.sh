#!/bin/sh
set -eu
# Leave archive creation to the learner; its parent must be writable.
install -d -m 0755 -o student -g student /srv/lx002 /srv/lx002/source
cat > /srv/lx002/source/records.txt <<'EOF'
INFO api ready
WARN cache cold
ERROR request failed
INFO worker ready
ERROR database timeout
WARN retry scheduled
EOF
chown student:student /srv/lx002/source/records.txt
chmod 0644 /srv/lx002/source/records.txt
rm -f /srv/lx002/archive/records.hard /home/student/current-records /srv/lx002/report.txt
