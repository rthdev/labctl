# Rootless Container Labs

The `CT` series is a ten-lab Rocky Linux 9 curriculum. Every lab uses one node,
the `student` account, rootless Podman, the student's container storage, an
enabled lingering user manager, and SELinux. Labs do not use the host container
engine, privileged containers, or disabled SELinux.

| Lab | Outcome |
| --- | --- |
| `CT001` | Run and retain a successful one-shot container. |
| `CT002` | Practice lifecycle, logs, and `exec` against one running worker. |
| `CT101` | Publish a web service only on a specified loopback port. |
| `CT102` | Preserve exact data in a named volume. |
| `CT201` | Author a Containerfile and build a labeled local image. |
| `CT202` | Connect an API and client by DNS on an internal network. |
| `CT301` | Run a configured two-container pod with a read-only bind. |
| `CT302` | Apply resource limits and effective workload hardening. |
| `CT401` | Survive a reboot with a generated rootless user Quadlet unit. |
| `CT402` | Diagnose a staged port mismatch and recover without losing volume data. |

The early labs provide exact names, commands, and expected content. Later labs
retain exact observable outcomes but require progressively more diagnosis. Each
`lab.yaml` and setup-produced `/home/student/LAB.md` is a complete standalone
learner contract. Setup is idempotent, does not overwrite `LAB.md`, and does not
replace learner-created objective resources. CT402 stages its incident once and
preserves subsequent repair work.

## Images And Connectivity

Guest setup installs Podman packages and downloads
`docker.io/library/alpine:3.20` into the `student` image store. Provisioning
therefore requires working package repositories, registry DNS, and Internet
access. Grading performs no registry operation and uses the preloaded local
image only.

`docker.io/library/alpine:3.20` is a fully qualified reputable upstream tag, but
it is mutable. These definitions intentionally do not claim digest provenance:
an immutable digest was not independently authenticated for this curriculum,
and no digest is invented. Reprovisioning at different times can
therefore obtain different image bytes. Operators needing stronger provenance
must mirror and independently verify an immutable image, then update and review
the definitions and tests together.

## Verification Limits

Graders validate the schema-v1 lab ID, exactly one `node`, the `student` SSH
account, absolute identity files, pinned `known_hosts`, and a bounded SSH path.
They parse raw Podman info and inspect JSON, require the student's rootless
storage and SELinux, reject privileged state, and connect functional probes to
the named inspected containers. CT302 additionally probes effective UID/GID,
read-only-root, and `NoNewPrivs`; CT401 requires a changed boot ID and a generated
unit tied to its Quadlet source and actual container; CT402 retains evidence of
the staged fault.

These checks are pedagogical outcome verification, not a tamper-resistant guest
attestation system. A learner with sufficient in-guest access can alter evidence
visible to an in-guest command. The graders do not inspect disks out of band and
do not prove the provenance of the mutable upstream image. They make no arbitrary
repair changes; their `exec`, HTTP, file, systemd, and inspect operations are
bounded read/probe operations.

### Requirements and evidence

Setup installs guest packages (including rootless networking support), validates
nonoverlapping student subordinate UID/GID ranges of at least 65536 IDs, enables
linger, starts the student user manager, and sets its runtime directory and D-Bus
environment for image preloading. Existing insufficient or overlapping mappings
fail provisioning rather than silently changing a used container store. CT302
requires the normal Rocky 9 cgroup-v2 hierarchy; CT401 requires Podman's Quadlet
generator and a working systemd user manager. No container engine is configured
on the labctl host.

Tests execute each standalone grader with pinned fake SSH responses, reject
malformed JSON and wrong runtime outcomes, exercise SSH failure/timeout paths,
and execute setup scripts twice with privileged commands replaced by sandbox
shims. These are deterministic contract/control-flow tests, **not real guest
acceptance**. Real libvirt creation, rootless networking/SELinux operation,
resource enforcement and reboot persistence have not been exercised for this
curriculum. An operator must authorize a disposable environment before that
acceptance work; the normal suite must not mutate real libvirt resources.

CT201 correlates container/image IDs and rejects mounted or runtime-modified
application files. CT202 correlates actual network IDs. CT301 correlates pod and
container IDs (allowing the normal infra container). CT401 correlates systemd
MainPID with the container ConmonPid, generated source/path, linger, boot change,
and default-target dependency. CT402 compares the recovered volume creation time
with its setup baseline and probes the actual published loopback endpoint.
CT101 and CT402 require the entire published-port map to match the one allowed
loopback binding; extra publications fail even when port 8080 is correct.

CT301 and CT402 include an explicitly scoped, read-only BusyBox static-HTTP
probe. It independently reads exact backing-file bytes using `podman unshare`,
rejects backing-path symlinks and overlapping inspected mounts, and compares
backing, mounted and live document-root device/inode identities. It obtains
process IDs from the named container's `podman top`, then correlates listening
socket inodes with those processes' file descriptors. A checker-hosted service,
sleeping web container, copied response or shadow mount cannot substitute for
the requested outcome. The learner contracts document supported BusyBox options
and exclude custom HTTP configuration/CGI; this deliberate scope avoids invasive
write-and-restore challenge probes or guessing an arbitrary server's semantics.
The helper uses 2-second subprocess timeouts, a 12-second guest deadline and a
bounded process scan. These remain mocked evidence tests, not real Podman/SELinux
acceptance. Grading observes current bytes, not historical proof that matching
data was never deleted and rewritten; it does not resist deliberate guest tampering.

Relevant upstream formats: [Podman inspect](https://docs.podman.io/en/latest/markdown/podman-inspect.1.html),
[container inspect schema](https://github.com/containers/podman/blob/v5.4.2/libpod/define/container_inspect.go),
and [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html).
The tests model these formats; fixture payloads are not captured guest output.
