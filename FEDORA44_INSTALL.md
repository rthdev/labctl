# Install labctl on Fedora 44

Run these steps on an **x86_64 Fedora 44 host**, using your normal login
account with `sudo` access. Enable Intel VT-x or AMD-V in firmware. labctl
requires Python 3.12+, KVM, internet access for packages/images, and space for
the guest image and lab disks. LX001 uses one vCPU, 1 GiB guest RAM and a
12 GiB virtual disk, in addition to the host's own requirements.

The host runs Fedora 44; the lab exercises run **Rocky Linux 9**.

## 1. Install host packages

```sh
sudo dnf install \
  qemu-kvm qemu-img libvirt-daemon-kvm libvirt-daemon-config-network \
  libvirt-client virt-install dnsmasq cloud-utils openssh-clients pipx \
  policycoreutils-python-utils polkit git
```

`cloud-utils` pulls in `cloud-utils-cloud-localds`, which provides
`cloud-localds`. Keep SELinux enforcing. Install labctl with pipx as shown
below; labctl is distributed only as a Python package.

## 2. Configure libvirt access

Enable the modular libvirt services and add your login to the access groups:

```sh
sudo systemctl enable --now \
  virtqemud.socket virtnetworkd.socket virtstoraged.socket \
  virtlogd.socket virtlockd.socket
sudo usermod -aG libvirt,kvm "$USER"
```

Use this on a new libvirt installation; do not enable a competing modular stack
if the host already uses monolithic `libvirtd`.

**Log out and back in**, then check access without sudo:

```sh
test -r /dev/kvm && test -w /dev/kvm && echo "KVM accessible"
virsh -c qemu:///system list --all
```

If libvirt prompts for authentication or denies access, use your site's polkit
policy. On a personal lab host, an administrator can create
`/etc/polkit-1/rules.d/49-labctl-libvirt.rules` with `sudoedit`, replacing
`YOUR_LOGIN` with the output of `id -un`:

```javascript
polkit.addRule(function(action, subject) {
    if (action.id == "org.libvirt.unix.manage" && subject.user == "YOUR_LOGIN") {
        return polkit.Result.YES;
    }
});
```

Keep the file root-owned and mode `0644`, then repeat the `virsh` check. Grant
this only to a trusted operator: system-libvirt management is a powerful host
privilege. Run labctl as your normal user, never with sudo.

labctl creates its own NAT networks through `qemu:///system`; you do not need
to create or start a `default` network. Leave existing networks and firewall
rules intact; site policy must allow guest DHCP, DNS and outbound traffic.

## 3. Prepare lab storage

Create the dedicated storage directory with your login as owner and the host's
QEMU service group (`qemu`) as group:

```sh
uid=$(id -u)
sudo install -d -o "$USER" -g qemu -m 2750 \
  "/var/lib/libvirt/images/labctl/$uid"
sudo restorecon -RFv /var/lib/libvirt/images/labctl
sudo -u qemu test -x "/var/lib/libvirt/images/labctl/$uid"
```

The setgid bit keeps new directories in the QEMU group; libvirt manages disk
ownership. Keep private keys and state in labctl's private XDG directories,
not this storage tree. Do not use `chmod 777` or run QEMU as your login.
The default location uses the libvirt SELinux image policy; do not disable
SELinux to work around access errors.

## 4. Install labctl

Clone this project and install it into an isolated pipx environment:

```sh
git clone --branch develop https://github.com/rthdev/labctl.git
cd labctl
pipx install "$PWD"
pipx ensurepath
export PATH="$HOME/.local/bin:$PATH"
labctl --help
```

If you already have a reviewed checkout, use that directory instead of cloning
again. A Git clone does not contain generated release artefacts; the command
above builds and installs directly from source.
**Do not use `pipx install labctl`: that PyPI name belongs to another project.**
Source installation downloads build/runtime dependencies from your configured
Python package index. Do not use `sudo pip`.

Select system libvirt for the current shell and check the installation:

```sh
export LABCTL_LIBVIRT_URI=qemu:///system
labctl provider doctor
```

To persist the connection, add `libvirt_uri = "qemu:///system"` to
`~/.config/labctl/config.toml` (create the directory/file if absent; preserve
existing settings). If you use `XDG_CONFIG_HOME`, use its `labctl/config.toml`
instead. Resolve mandatory doctor failures before continuing. Doctor checks
operator access; the storage check above separately checks QEMU traversal.

## 5. Download Rocky Linux 9 and run LX001

```sh
labctl image pull rocky:9
labctl image inspect rocky:9
labctl lab inspect LX001
labctl lab create LX001
labctl lab ssh LX001
```

Work through the instructions shown by `lab inspect` inside the guest. Type
`exit` to return to the host, then grade the exercise:

```sh
labctl lab grade LX001
```

An unsolved exercise fails grading with exit status `7`; this is not an
installation failure. Once the exercise is complete, all checks should pass.
The image pull verifies the managed image's provenance and checksum; do not
bypass failed verification with an untrusted-image flag.

To stop and remove your lab when finished:

```sh
labctl lab stop LX001
labctl lab rm LX001
```

Removal deletes the lab's work. If cleanup refuses an operation, retain the
error and state for diagnosis rather than deleting files or libvirt resources
manually.

## Update or uninstall

Install an updated, reviewed checkout with `pipx install --force /path/to/labctl`.
Before `pipx uninstall labctl`, remove any unwanted labs with `labctl lab rm`.
Uninstalling the application does not remove VMs, images or state.

See [configuration](docs/configuration.md) and
[troubleshooting](docs/troubleshooting.md) for non-default setups.

Verification requirements and historical Fedora test scope:
[platform verification](docs/platform-verification.md). Installation
success does not certify every bundled exercise.
