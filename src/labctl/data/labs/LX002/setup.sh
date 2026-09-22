#!/bin/sh
set -eu
install -d -m 0755 -o student -g student /srv/lx002/source /srv/lx002/archive
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
cat > /home/student/LAB.md <<'EOF'
# LX002: Files, links, and text processing
Create the required hard and symbolic links. Use text-processing tools to count
records by severity and write exactly these sorted lines to `/srv/lx002/report.txt`:
`ERROR 2`, `INFO 2`, and `WARN 2`. Set ownership to student:student and mode 0640.
EOF
chown student:student /home/student/LAB.md
