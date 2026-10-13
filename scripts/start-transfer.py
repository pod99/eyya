#!/usr/bin/env python3
"""Start a temporary outbound transport to an existing authenticated SSH service.

Uses the already installed official cloudflared binary. Does not modify SSH,
firewall rules, credentials, or service startup configuration. Stop the PID in
/root/eyya-transfer.pid after exporting. SSH host-key verification is required
on the client; the tunnel's public hostname is not an authentication mechanism.
"""
import os
import subprocess
from pathlib import Path

os.umask(0o077)
log = Path('/root/eyya-transfer.log')
with log.open('w') as output:
    process = subprocess.Popen([
        '/usr/sbin/runuser', '-u', 'nobody', '--',
        '/opt/eyya-mail-fallback/cloudflared', 'tunnel', '--no-autoupdate',
        '--protocol', 'quic', '--url', 'tcp://127.0.0.1:2222',
    ], stdout=output, stderr=output, start_new_session=True)
Path('/root/eyya-transfer.pid').write_text(str(process.pid) + '\n')
print('Temporary transport PID:', process.pid)
print('Read /root/eyya-transfer.log for the assigned hostname.')
