"""Whitelisted host proxy capture and shared cloud-init integration."""

from __future__ import annotations

import json
import os
import shlex
import unicodedata
from typing import Any

PROXY_NAMES = ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy")

# Run only in the guest, before setup. Keep vendor files intact; SetEnv is a
# first-directive-wins option, so merge its existing effective values rather
# than silently masking unrelated vendor settings with a new drop-in.
_LOGIN_INSTALLER = r"""#!/usr/bin/python3
import json, os, subprocess, sys
from pathlib import Path
try:
    with open('/etc/labctl/proxy.json') as stream:
        proxies = json.load(stream)
    result = subprocess.run(['/usr/sbin/sshd', '-T'], check=True,
                            capture_output=True, text=True)
    values = dict(line[len('setenv '):].split('=', 1)
                  for line in result.stdout.splitlines()
                  if line.startswith('setenv ') and '=' in line)
    values.update(proxies)
    entries = ['"' + name + '=' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
               for name, value in values.items()]
    target = Path('/etc/ssh/sshd_config.d/00-labctl-proxy.conf')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('SetEnv ' + ' '.join(entries) + '\n')
    target.chmod(0o644)
    result = subprocess.run(['/usr/sbin/sshd', '-T'], check=True,
                            capture_output=True, text=True)
    effective = dict(line[len('setenv '):].split('=', 1)
                     for line in result.stdout.splitlines()
                     if line.startswith('setenv ') and '=' in line)
    if any(effective.get(name) != value for name, value in values.items()):
        raise ValueError('proxy configuration not effective')
    subprocess.run(['/usr/bin/systemctl', 'reload', 'sshd'], check=True,
                   capture_output=True)
except Exception:
    sys.exit('labctl: proxy login configuration failed')
"""


def configure_proxy(document: dict[str, Any], *, extra_bypass: tuple[str, ...] = ()) -> None:
    """Add proxy data and a setup entry point without putting values in argv."""
    values: dict[str, str] = {}
    for name in PROXY_NAMES:
        value = os.environ.get(name, os.environ.get(name.upper()))
        if value is not None:
            if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in value):
                raise ValueError("proxy environment contains an unsupported control character")
            values[name] = values[name.upper()] = value
    if any(values.get(name) for name in PROXY_NAMES[:-1]):
        bypass = values.get("no_proxy", "")
        bypass = ",".join(filter(None, [bypass, "localhost,127.0.0.1,::1", *extra_bypass]))
        values["no_proxy"] = values["NO_PROXY"] = bypass
    if not values:
        return
    document["write_files"].append(
        {
            "path": "/etc/labctl/proxy.json",
            "permissions": "0644",
            "owner": "root:root",
            "content": json.dumps(values),
        }
    )
    document["runcmd"] = [["/usr/local/sbin/labctl-proxy-setup"]]
    scripts = {
        "/usr/local/sbin/labctl-proxy-login": _LOGIN_INSTALLER,
        "/usr/local/sbin/labctl-proxy-setup": (
            "#!/usr/bin/python3\n"
            "import json, os, subprocess, sys\n"
            "try:\n"
            "    with open('/etc/labctl/proxy.json') as stream:\n"
            "        values = json.load(stream)\n"
            "    subprocess.run(['/usr/local/sbin/labctl-proxy-login'], check=True,\n"
            "                   capture_output=True)\n"
            "    os.execve('/usr/local/sbin/labctl-setup', "
            "['/usr/local/sbin/labctl-setup'], dict(os.environ, **values))\n"
            "except Exception:\n"
            "    sys.exit('labctl: proxy setup failed')\n"
        ),
        "/etc/systemd/user-environment-generators/90-labctl-proxy": (
            "#!/usr/bin/python3\n"
            "import json, shlex, sys\n"
            "try:\n"
            "    with open('/etc/labctl/proxy.json') as stream:\n"
            "        values = json.load(stream)\n"
            "    for name, value in values.items():\n"
            "        print(name + '=' + shlex.quote(value))\n"
            "except Exception:\n"
            "    sys.exit('labctl: proxy environment failed')\n"
        ),
    }
    for path, content in scripts.items():
        document["write_files"].append(
            {"path": path, "permissions": "0755", "owner": "root:root", "content": content}
        )
    configs = {
        "/etc/profile.d/labctl-proxy.sh": "".join(
            f"export {name}={shlex.quote(value)}\n" for name, value in values.items()
        ),
        "/etc/sudoers.d/90-labctl-proxy": (
            'Defaults env_keep += "'
            + " ".join(case for name in PROXY_NAMES for case in (name, name.upper()))
            + '"\n'
        ),
    }
    for path, content in configs.items():
        document["write_files"].append(
            {"path": path, "permissions": "0644", "owner": "root:root", "content": content}
        )
