"""Run LX201 setup with isolated files and stateful service/firewall shims."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

LAB = Path(__file__).parents[1] / "src/labctl/data/labs/LX201"

SHIMS = r"""
dnf() {
    test "$*" = '-y install httpd firewalld curl' || return 1
}
ip() {
    test "$*" = '-o route show default' || return 1
    printf '%s\n' 'default via 192.0.2.1 dev eth0'
}
nmcli() {
    case "$*" in
        '-g GENERAL.CONNECTION device show eth0') printf '%s\n' wired ;;
        '-g connection.uuid connection show wired') printf '%s\n' test-uuid ;;
        '-g IP4.ADDRESS device show eth0') printf '%s\n' 192.0.2.2/24 ;;
        *) return 1 ;;
    esac
}
systemctl() {
    printf 'systemctl %s\n' "$*" >> "$STATE/events"
    action=$1
    shift
    now=no
    if test "${1:-}" = --now; then now=yes; shift; fi
    for service in "$@"; do
        case "$service" in httpd|NetworkManager|firewalld) ;; *) return 1 ;; esac
        case "$action" in
            enable|disable)
                printf '%s\n' "$action" > "$STATE/$service.enabled"
                if test "$now" = yes; then
                    case "$action" in enable) active=active ;; disable) active=inactive ;; esac
                    printf '%s\n' "$active" > "$STATE/$service.active"
                fi ;;
            start) printf '%s\n' active > "$STATE/$service.active" ;;
            stop) printf '%s\n' inactive > "$STATE/$service.active" ;;
            *) return 1 ;;
        esac
    done
}
firewall_cmd() {
    read -r active < "$STATE/firewalld.active"
    printf 'firewall-cmd %s [%s]\n' "$*" "$active" >> "$STATE/events"
    test "$active" = active || return 1
    case "$*" in
        '--permanent --zone=public --remove-service=http')
            printf '%s\n' absent > "$STATE/http.permanent" ;;
        '--permanent --zone=public --add-service=cockpit')
            printf '%s\n' present > "$STATE/cockpit.permanent" ;;
        --reload)
            read -r http < "$STATE/http.permanent"
            read -r cockpit < "$STATE/cockpit.permanent"
            printf '%s\n' "$http" > "$STATE/http.runtime"
            printf '%s\n' "$cockpit" > "$STATE/cockpit.runtime" ;;
        *) return 1 ;;
    esac
}
"""


def test_lx201_setup_stages_policy_before_leaving_firewall_off(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # Deliberately start with HTTP allowed and cockpit missing.
    for name, value in {
        "http.permanent": "present",
        "cockpit.permanent": "absent",
        "firewalld.active": "inactive",
        "firewalld.enabled": "disable",
    }.items():
        (state / name).write_text(value + "\n")

    # No host service/network/package tools are reachable through PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    firewall = bin_dir / "firewall-cmd"
    firewall.write_text("#!/bin/sh\nset -eu\n" + SHIMS + '\nfirewall_cmd "$@"\n')
    firewall.chmod(0o755)
    for name in ("install", "chmod", "awk", "head"):
        executable = shutil.which(name)
        assert executable is not None
        (bin_dir / name).symlink_to(executable)
    guest = tmp_path / "guest"
    (guest / "var/www/html").mkdir(parents=True)
    setup = (LAB / "setup.sh").read_text()
    for path in ("/var/lib/labctl", "/var/www/html"):
        setup = setup.replace(path, shlex.quote(str(guest) + path))
    script = SHIMS + "\n" + setup

    for _ in range(2):
        (state / "events").write_text("")
        subprocess.run(
            ["/bin/sh", "-eu", "-c", script],
            env={"PATH": str(bin_dir), "STATE": str(state)},
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        events = (state / "events").read_text().splitlines()
        policy_events = [line for line in events if line.startswith("firewall-cmd ")]
        assert policy_events == [
            "firewall-cmd --permanent --zone=public --remove-service=http [active]",
            "firewall-cmd --permanent --zone=public --add-service=cockpit [active]",
            "firewall-cmd --reload [active]",
        ]
        for layer in ("permanent", "runtime"):
            assert (state / f"http.{layer}").read_text().strip() == "absent"
            assert (state / f"cockpit.{layer}").read_text().strip() == "present"
        assert (state / "firewalld.active").read_text().strip() == "inactive"
        assert (state / "firewalld.enabled").read_text().strip() == "disable"
        # Shutdown must follow all policy staging, not precede or interrupt it.
        last_policy = events.index(policy_events[-1])
        assert all("firewalld" in event for event in events[last_policy + 1 :])
        assert events[last_policy + 1 :]
        for service in ("httpd", "NetworkManager"):
            assert (state / f"{service}.active").read_text().strip() == "active"
            assert (state / f"{service}.enabled").read_text().strip() == "enable"
        baseline = guest / "var/lib/labctl/lx201-network-baseline"
        assert baseline.read_text().splitlines() == ["eth0", "test-uuid", "192.0.2.2/24"]
        assert baseline.stat().st_mode & 0o777 == 0o600
        assert (guest / "var/www/html/network-health").read_text() == "LX201 reachable\n"
