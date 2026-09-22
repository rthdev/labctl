#!/bin/sh
set -eu

install -d -m 0755 -o student -g student /home/student
if [ ! -e /home/student/LAB.md ]; then
    install -m 0644 -o student -g student /dev/null /home/student/LAB.md
    cat > /home/student/LAB.md <<'EOF'
# LX001: Users and permissions

1. Create group `labops` and user `opsadmin`.
2. Add `opsadmin` to `labops`.
3. Create `/srv/labshare` as `opsadmin:labops` with mode `2770`.
4. Create `/srv/labshare/operations.txt` as `opsadmin:labops` with mode `0660`.

The setgid directory bit is part of the required result.
EOF
    chown student:student /home/student/LAB.md
fi
