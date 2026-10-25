"""
Test package setup, run before any test module imports the app.

Points the app at a throwaway home directory and a missing lqos.conf, so
tests never read this machine's settings.json, /etc/lqos.conf or LibreQoS
files. Run from the repo root:

    python3 -m unittest discover -s tests -t . -v
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix='lqsync-test-')

os.environ['LQSYNC_HOME'] = HOME
os.environ['LQSYNC_LQOS_CONF'] = os.path.join(HOME, 'missing-lqos.conf')
os.environ['LQSYNC_SYSFS'] = os.path.join(HOME, 'missing-sysfs')

# libreqos_dir inside HOME: no VERSION_STRING there, so no node ids are emitted.
with open(os.path.join(HOME, 'settings.json'), 'w') as f:
    json.dump({"scanner": {"libreqos_dir": HOME}}, f)

if REPO not in sys.path:
    sys.path.insert(0, REPO)
