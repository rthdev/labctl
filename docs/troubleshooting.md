# Troubleshooting and Doctor

Run `labctl provider doctor` before creation. It checks the effective dedicated
libvirt storage root. Failed mandatory checks return 5;
warnings return 0. Doctor checks x86_64 and the supported OS release, `virsh`,
`qemu-img`, `virt-install`, `cloud-localds`, `ssh`, libvirt compute/storage/NAT
capabilities, current-user storage access, permissions, and SELinux mode. The
current-user check does not prove QEMU DAC or SELinux access. Doctor never
invokes package managers, sudo, ACL tools, or SELinux management commands.

RHEL, CentOS Stream, Rocky, and Fedora guidance uses `dnf install qemu-kvm
libvirt virt-install libvirt-client cloud-utils`. Arch guidance uses `pacman -S
qemu-full libvirt virt-install dnsmasq cloud-image-utils`. Run the displayed
command yourself under your site's authorization policy.

On Fedora 44, ensure all three modular sockets are active:

    sudo systemctl enable --now \
      virtqemud.socket virtnetworkd.socket virtstoraged.socket

For permission errors, verify libvirt policy and socket groups for the bound URI.
For storage/SELinux errors, have an administrator provision and label only the
dedicated libvirt storage root, then inspect AVC denials. Do not label the XDG
data/cache roots, disable SELinux, use `chmod 777`, or run QEMU as the desktop
user. For shutdown timeout, inspect guest
agents and use `--force` only when data-loss risk is accepted. For foreign
ownership or unrecoverable drift, do not rename resources to `labctl-*`; use
reset/remove when safe or perform documented manual recovery.

Fedora libvirt dynamically changes active disk, seed, and backing-file ownership
to `qemu`. This is expected. Do not recursively `chown` a running lab. New copied
bases use `0444` inside a `2750` UUID directory so the owning operator can still
verify/reset them without joining the host-wide `qemu` group; the parent
directories continue to prevent access by unrelated users.
