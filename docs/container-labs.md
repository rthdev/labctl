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
`lab.yaml` and centrally provisioned `/home/student/LAB.md` is a complete standalone
learner contract. Setup is idempotent, does not overwrite `LAB.md`, and does not
replace learner-created objective resources. CT402 stages its incident once and
preserves subsequent repair work.

## Images And Connectivity

Guest setup installs Podman and refreshes the required fully qualified UBI 9.8
minor tags in the `student` rootless image store, even if the tag is cached:

| Image | Roles |
| --- | --- |
| `registry.access.redhat.com/ubi9/ubi:9.8` | CT001, CT002, CT102, CT201 build base, CT302, CT401; CT202 client, CT301 checker, CT402 helper |
| `registry.access.redhat.com/ubi9/httpd-24:9.8` | CT101 web, CT202 API, CT301 web, CT402 API |

Provisioning requires package repositories, registry DNS and Internet access.
The base has Bash, core utilities, curl and DNF, but no HTTP server. The Apache
image has `/usr/sbin/httpd`, defaults to UID 1001 and `/usr/bin/run-httpd`, and
uses `/var/www/html` as its document root (not its `/opt/app-root/src` working
directory). Its launcher configures non-root HTTP on 8080. CT301/CT402 instead
use the minimal static Apache configuration printed in their assignments.
Probes use bounded curl with proxy bypass, not an assumed wget applet.

These minor tags are mutable, **not digest pins**. Setup records resolved image
IDs and repository digests in `/var/lib/labctl/ct-image-provenance` for diagnosis;
this does not authenticate or freeze upstream bytes. A successful setup rerun
retains its image record and learner work. A reset rebuilds guest storage using
the original saved definition and setup, so those saved tags may resolve to newer
bytes; it does not adopt a new catalogue assignment. Recreate after updating
labctl to adopt this migration, backing up learner work first. No lifecycle or
seed migration is performed. Grading never pulls or consults the registry.

CT402 explicitly starts a healthy internal port-9090 Apache API while publishing
loopback 8080 to the wrong container port 8080. Setup checks both conditions
before marking the incident initialized. `API_PORT` remains incident metadata:
it does not automatically reconfigure Apache. Recovery must fix the configuration
`Listen` directive as well as the container environment and preserve the volume.

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
requires the Rocky 9 cgroup-v2 hierarchy. Its setup installs a student-specific
`user@UID.service.d/50-labctl-cpu.conf` drop-in delegating `cpu memory pids`, then
reloads systemd before enabling linger or starting the user manager. Rocky's
memory/pids-only default otherwise makes the assigned rootless `--cpus 0.5`
fail. Reloading also updates an already-active manager without restarting it or
interrupting learner workloads. This is guest provisioning, not a learner sudo
step or a host cgroup policy change. Existing saved setups are unchanged; back
up work and recreate CT302 after updating to adopt the fix.
CT401 requires Podman's Quadlet
generator and a working systemd user manager. No container engine is configured
on the labctl host.

Tests execute each standalone grader with pinned fake SSH responses, reject
malformed JSON and wrong runtime outcomes, exercise SSH failure/timeout paths,
and execute setup scripts twice with privileged commands replaced by sandbox
shims. These are deterministic contract/control-flow tests, **not real guest
acceptance**. Separately, all ten labs completed real nested-KVM lifecycle
acceptance on a CentOS Stream 10 test host with Rocky 9 learner guests and
SELinux enforcing. See [UBI migration verification](container-ubi-verification.md)
for the tested artifacts, per-lab scope, equivalent-code evidence and preserved
harness failures. The normal suite must not mutate real libvirt resources.

CT201 correlates container/image IDs and rejects mounted or runtime-modified
application files. CT202 correlates actual network IDs. CT301 correlates pod and
container IDs (allowing the normal infra container). CT401 correlates systemd
MainPID with the container ConmonPid, generated source/path, linger, boot change,
and default-target dependency. CT402 compares the recovered volume creation time
with its setup baseline and probes the actual published loopback endpoint.
CT101 and CT402 require the entire published-port map to match the one allowed
loopback binding; extra publications fail even when port 8080 is correct.

CT301 and CT402 include an explicitly scoped, read-only Apache static-HTTP
probe. It independently reads exact backing-file bytes using `podman unshare`,
rejects backing-path symlinks and overlapping inspected mounts, and compares
backing, mounted and live document-root device/inode identities. It obtains
process IDs from the named container's `podman top`, then correlates listening
socket inodes with those processes' file descriptors. A checker-hosted service,
sleeping web container, ordinary copied response or shadow mount is rejected
within the supported static-server contract. The learner contracts document the exact minimal Apache configuration and foreground argv
and exclude includes, aliases, redirects, extra modules and CGI; this deliberate scope avoids invasive
write-and-restore challenge probes or guessing an arbitrary server's semantics.
Current config bytes do not prove what Apache loaded. The probe also requires
both config mtime and ctime to predate **every** named-container Apache process
(including the parent), with a conservative two-second rounding margin. Finish
edits and permission/SELinux-label changes, wait at least three seconds, then
start or fully restart the container; graceful reload is not sufficient. If
Podman relabels the config at launch (`:Z`), wait three seconds and restart again.
Waiting after starting too soon does not repair that ordering. Before/after metadata
(including inode and nanosecond timestamps) and process start-time checks reject
ordinary concurrent edits/replacements and PID reuse. Do not edit or restart
while grading. This assumes a stable host clock and ordinary filesystem/proc
semantics; it is not an atomic snapshot, loaded-memory inspection, or protection
against deliberate timestamp/namespace/process tampering. The conservative
ordering can reject a correctly loaded config created immediately before launch.
The helper uses 2-second subprocess timeouts, a 12-second guest deadline and a
bounded process scan. Mocked regressions supplement the separate real
Podman/SELinux acceptance linked above. Grading observes current bytes, not historical proof that matching
data was never deleted and rewritten; it does not resist deliberate guest tampering.

Relevant upstream formats: [Podman inspect](https://docs.podman.io/en/latest/markdown/podman-inspect.1.html),
[container inspect schema](https://github.com/containers/podman/blob/v5.4.2/libpod/define/container_inspect.go),
and [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html).
The tests model these formats; fixture payloads are not captured guest output.
