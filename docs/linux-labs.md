# Progressive Linux labs

The bundled Rocky Linux 9 track develops operational skills through outcome-graded
single-node exercises. Every lab writes its detailed brief to
`/home/student/LAB.md`; use `labctl console LAB_ID node` when a task requires
console access and `labctl grade LAB_ID` to assess the resulting system state.

| Lab | Focus | VM resources |
| --- | --- | --- |
| LX001 | Users, groups, ownership, permissions | 1 CPU, 1 GiB RAM, 12 GiB disk |
| LX002 | Files, hard/symbolic links, text processing | 1 CPU, 1 GiB RAM, 12 GiB disk |
| LX101 | Packages, systemd services, persistent logs | 1 CPU, 1 GiB RAM, 12 GiB disk |
| LX102 | XFS and persistent mounts | 1 CPU, 1 GiB RAM, 14 GiB disk |
| LX201 | NetworkManager-safe firewall operations | 1 CPU, 1 GiB RAM, 12 GiB disk |
| LX202 | Services, timers, and maintenance evidence | 1 CPU, 1 GiB RAM, 12 GiB disk |
| LX301 | httpd on a nonstandard port with SELinux | 1 CPU, 2 GiB RAM, 12 GiB disk |
| LX302 | LVM and online XFS expansion/recovery | 1 CPU, 2 GiB RAM, 14 GiB disk |
| LX401 | GRUB, root recovery, fstab, and SELinux relabeling | 2 CPUs, 2 GiB RAM, 14 GiB disk |
| LX402 | Bounded production-incident diagnosis | 2 CPUs, 2 GiB RAM, 14 GiB disk |

## Storage media

Definition schema version 1 does not support additional virtual disks. LX102,
LX302, LX401, and LX402 therefore use deterministic image files under
`/var/lib/labctl-media` as lab media. These images are intentionally bounded and
isolated from the host/orchestrator. Learners use loop devices, filesystems, and
LVM exactly as documented in each in-guest brief.

## Recovery and safety notes

- LX301 and LX401 require SELinux to remain enforcing. Disabling SELinux is not a
  valid solution.
- LX401 is reachable after creation. The learner must open the console and reboot
  to activate the staged boot faults, then complete recovery from GRUB.
- LX402 confines disk pressure to a 384 MiB loop filesystem and caps the staged
  CPU symptom at 20 percent of one host CPU. Recovery must be durable and include
  the requested incident evidence.
- Graders inspect outcomes rather than prescribed learner command sequences or
  script contents. SSH host keys are pinned through the grading context.
