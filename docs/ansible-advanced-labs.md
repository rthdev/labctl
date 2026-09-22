# Advanced Ansible labs

AN401 and AN402 are outcome-graded labs. Their graders run the learner's automation from a persistent controller against managed VMs that `labctl` resets immediately before each grade. The graders do not inspect `site.yml`, roles, task ordering, or module selection.

## AN401: repair fragile application automation

Topology: one persistent `controller` and one reset `target`.

The controller contains an intentionally fragile project at `/home/student/ansible-lab`, including `site.yml` and a `roles/web` role. The learner must repair it so that:

- Apache HTTP Server is enabled and active.
- `/etc/an401/app.conf` contains exactly `environment=production` followed by a newline and is `root:apache` mode `0640`.
- `http://127.0.0.1/` returns `AN401 application ready`.
- The playbook succeeds twice; the second recap reports zero changed, unreachable, and failed hosts.

Grading resets only `target`, preserving the controller project.

## AN402: recover a multi-node platform

Topology: one persistent `controller` and three reset managed nodes: `web1`, `web2`, and `web3`.

The managed-node image has deterministic starting conditions:

- `web1` is healthy, testing preservation of already-correct state.
- `web2` has a stopped/disabled web service and incorrect landing content.
- `web3` has blocked HTTP, an incorrect SELinux boolean, and a stopped/disabled backend.

The repaired automation must leave every managed node with:

- `httpd` and `an402-backend` enabled and active;
- the prescribed reverse-proxy configuration and landing page;
- `firewalld` enabled with the `http` service allowed in both runtime and permanent configuration;
- SELinux enforcing with `httpd_can_network_connect` enabled; and
- a healthy backend response through `http://127.0.0.1/api/health`.

It must also create `/home/student/ansible-lab/recovery-summary.json` on the controller with this schema:

```json
{
  "lab": "AN402",
  "status": "recovered",
  "hosts": [
    {"name": "web1", "status": "healthy"},
    {"name": "web2", "status": "healthy"},
    {"name": "web3", "status": "healthy"}
  ]
}
```

Host entry order is not significant, but exactly one healthy entry for each managed node is required. The second playbook recap must report zero changed, unreachable, and failed hosts.

## Grading security model

For both labs, the grader:

1. receives refreshed VM addresses and pinned host-key data from the grading context;
2. creates a dedicated controller-to-target Ed25519 key on the controller;
3. installs only its public key on managed nodes;
4. stages a generated inventory, the managed nodes' pinned host keys, and a grader-owned Ansible configuration in a private directory using no-follow file creation;
5. pins the default, uncolored Ansible callback output before parsing idempotence recaps;
6. invalidates any prior AN402 recovery summary, runs the learner playbook twice, and requires a newly generated summary before checking observable host outcomes; and
7. removes the temporary staging directory.

The host-management private key remains on the `labctl` host and is never copied into controller inventory or files.

These checks are pedagogical outcome validation, not an adversarial anti-cheat boundary. A learner has root-capable automation on the lab VMs and could tamper with in-guest observations. Accepting alternate valid implementations without parsing playbook source is intentional.
