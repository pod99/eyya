#!/usr/bin/env python3
"""Collect private migration metadata without printing credentials.

The output includes Docker environment variables and other secrets. Keep it
in a private archive, never commit it or serve it over unauthenticated HTTP.
This script performs read-only inspection of the production services.
"""
import datetime
import json
import os
import subprocess
from pathlib import Path

os.umask(0o077)
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
output = Path('/root') / ('eyya-inventory-' + stamp)
output.mkdir(mode=0o700)

def capture(name, command):
    result = subprocess.run(command, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    (output / name).write_bytes(result.stdout)
    (output / (name + '.stderr')).write_bytes(result.stderr)
    return result

containers = capture('container-ids.txt', ['docker', 'ps', '-aq'])
ids = containers.stdout.decode().split()
if ids:
    capture('containers-private.json', ['docker', 'inspect', *ids])
volumes = capture('volume-names.txt', ['docker', 'volume', 'ls', '-q'])
names = volumes.stdout.decode().split()
if names:
    capture('volumes.json', ['docker', 'volume', 'inspect', *names])
capture('running-container-ids.txt', ['docker', 'ps', '-q'])
capture('images.json', ['docker', 'image', 'ls', '--no-trunc',
                        '--format', '{{json .}}'])
capture('mounts.json', ['findmnt', '-J'])
capture('filesystem-space.txt', ['df', '-hT'])
capture('addresses.json', ['ip', '-j', 'address'])
capture('routes.json', ['ip', '-j', 'route'])
capture('root-crontab-private.txt', ['crontab', '-l'])
capture('services.txt', ['systemctl', 'list-units', '--type=service',
                          '--all', '--no-pager'])
capture('timers.txt', ['systemctl', 'list-timers', '--all', '--no-pager'])
capture('unit-files.txt', ['systemctl', 'list-unit-files', '--no-pager'])
capture('packages.txt', ['dpkg-query', '-W'])
capture('nginx-private.txt', ['nginx', '-T'])
capture('firewall-v4.txt', ['iptables-save'])
capture('firewall-v6.txt', ['ip6tables-save'])
capture('docker-info.txt', ['docker', 'info'])
capture('host.txt', ['hostnamectl'])
summary = {'created_utc': stamp, 'container_count': len(ids),
           'volume_count': len(names), 'private_inventory_directory': str(output)}
(output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary))
