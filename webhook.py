"""
PPP webhook: MikroTik PPP profile on-up / on-down scripts tell LQ-Sync about
a session at once, so the subscriber is added or removed within seconds
instead of at the next poll.

On the router:
  - a /system script "lqsync-webhook" that POSTs the event to LQ-Sync with
    the router's own token (X-LQSync-Token header), and
  - one line at the top of every PPP profile's on-up and on-down script that
    calls it with the session's user, IP and caller-id. Existing profile
    scripts are kept.

This module renders those scripts (to paste into the terminal), installs,
removes and checks them over the RouterOS API, and parses the events the
routers send. The sync service (updatecsv.py) acts on the events.
"""
import ipaddress
import json
import secrets

SCRIPT_NAME = "lqsync-webhook"
SCRIPT_COMMENT = "LQ-Sync PPP webhook (managed by LQ-Sync)"
WEBHOOK_PATH = "/api/webhook/mikrotik"
TOKEN_HEADER = "X-LQSync-Token"
EVENTS = ("up", "down", "test")
# PPP profile script → event it reports
PROFILE_HOOKS = {"on-up": "up", "on-down": "down"}
# Every profile line this app adds contains MARKER; the line starts MARKER_OFFSET characters before it.
_HOOK_HEAD = ":do {:local lq [:parse ["
MARKER = f"/system script get {SCRIPT_NAME} source"
MARKER_OFFSET = len(_HOOK_HEAD)


def new_token():
    return secrets.token_hex(16)


def webhook_url(base_url):
    return base_url.rstrip("/") + WEBHOOK_PATH


def ros_escape(text):
    """Text as the inside of a RouterOS double-quoted string."""
    out = []
    for ch in text:
        if ch in '\\"$?':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


# ── Router-side scripts ─────────────────────────────────────────────────────

def function_source(base_url, token):
    """Source of /system script lqsync-webhook. Run without arguments it sends a test event."""
    return "\r\n".join([
        "# LQ-Sync PPP webhook. Managed by LQ-Sync: reinstall it from LQ-Sync instead of editing it.",
        "# PPP profile on-up / on-down scripts call it with event, user, ip and cid.",
        f':local url "{ros_escape(webhook_url(base_url))}"',
        f':local token "{ros_escape(token)}"',
        ':local ev $event',
        ':if ([:len $ev] = 0) do={ :set ev "test" }',
        ':local body ("{\\"event\\":\\"" . $ev . "\\",\\"service\\":\\"ppp\\",\\"user\\":\\"" . $user'
        ' . "\\",\\"ip\\":\\"" . $ip . "\\",\\"caller_id\\":\\"" . $cid . "\\"}")',
        ':do {',
        f'    /tool fetch url=$url http-method=post http-header-field=("Content-Type: application/json,'
        f'{TOKEN_HEADER}: " . $token) http-data=$body output=none',
        '} on-error={ :log warning ("LQ-Sync webhook " . $ev . " " . $user . " failed: " . $url) }',
    ])


def hook_line(event):
    """The line added to a PPP profile's on-up (event "up") or on-down ("down") script."""
    return (f'{_HOOK_HEAD}{MARKER}]]; $lq event="{event}" user=$user ip=$"remote-address" '
            f'cid=$"caller-id"}} on-error={{}}')


def without_hook(script):
    """A profile script with this app's lines removed (unchanged when there are none)."""
    if MARKER not in (script or ""):
        return script or ""
    lines = script.replace("\r\n", "\n").split("\n")
    kept = [line for line in lines if MARKER not in line]
    while kept and not kept[0].strip():
        kept.pop(0)
    return "\r\n".join(kept)


def with_hook(script, event):
    """A profile script with this app's line for event first, and the rest kept."""
    rest = without_hook(script)
    return hook_line(event) + ("\r\n" + rest if rest.strip() else "")


def terminal_install(base_url, token, router_name):
    """Commands to paste into the router's terminal to add or update the webhook."""
    up, down = hook_line("up"), hook_line("down")
    return "\n".join([
        f"# LQ-Sync PPP webhook for {router_name}: paste into this router's terminal (RouterOS 6.43+ or 7).",
        f"# Adds /system script {SCRIPT_NAME} and one line at the top of every PPP profile's on-up and",
        "# on-down script (existing scripts are kept), then sends a test call to LQ-Sync.",
        "{",
        f'/system script remove [find name="{SCRIPT_NAME}"]',
        f'/system script add name="{SCRIPT_NAME}" comment="{ros_escape(SCRIPT_COMMENT)}" '
        f'source="{ros_escape(function_source(base_url, token))}"',
        f':local up "{ros_escape(up)}"',
        f':local down "{ros_escape(down)}"',
        f':local mark "{ros_escape(MARKER)}"',
        ':foreach prof in=[/ppp profile find] do={',
        '    :local s [/ppp profile get $prof on-up]',
        '    :if ([:typeof [:find $s $mark]] = "nil") do={',
        '        :if ([:len $s] = 0) do={ /ppp profile set $prof on-up=$up } else={ /ppp profile set $prof on-up=($up . "\\r\\n" . $s) }',
        '    }',
        '    :set s [/ppp profile get $prof on-down]',
        '    :if ([:typeof [:find $s $mark]] = "nil") do={',
        '        :if ([:len $s] = 0) do={ /ppp profile set $prof on-down=$down } else={ /ppp profile set $prof on-down=($down . "\\r\\n" . $s) }',
        '    }',
        '}',
        ':log info "LQ-Sync PPP webhook installed"',
        f':local lq [:parse [/system script get {SCRIPT_NAME} source]]',
        '$lq',
        ':put "LQ-Sync PPP webhook installed; a test call was sent"',
        "}",
    ]) + "\n"


def terminal_uninstall(router_name):
    """Commands to paste into the router's terminal to remove the webhook."""
    return "\n".join([
        f"# Remove the LQ-Sync PPP webhook from {router_name}: paste into this router's terminal.",
        "{",
        f':local mark "{ros_escape(MARKER)}"',
        ':local strip do={',
        '    :local r $s',
        '    :local i [:find $r $m]',
        '    :while ([:typeof $i] = "num") do={',
        f'        :local a ($i - {MARKER_OFFSET})',
        '        :if ($a < 0) do={ :set a 0 }',
        '        :local e [:find $r "\\n" $i]',
        '        :if ([:typeof $e] = "nil") do={ :set e [:len $r] }',
        '        :set r ([:pick $r 0 $a] . [:pick $r ($e + 1) [:len $r]])',
        '        :set i [:find $r $m]',
        '    }',
        '    :return $r',
        '}',
        ':foreach prof in=[/ppp profile find] do={',
        '    /ppp profile set $prof on-up=[$strip s=[/ppp profile get $prof on-up] m=$mark]',
        '    /ppp profile set $prof on-down=[$strip s=[/ppp profile get $prof on-down] m=$mark]',
        '}',
        f'/system script remove [find name="{SCRIPT_NAME}"]',
        ':put "LQ-Sync PPP webhook removed"',
        "}",
    ]) + "\n"


# ── RouterOS API (install / remove / check) ─────────────────────────────────

def _id(entry):
    return entry.get("id") or entry.get(".id")


def _norm(source):
    return (source or "").replace("\r\n", "\n").strip()


def status(api, base_url, token):
    """
    What is installed on the router (read-only, works with the polling account):
    {script: missing|current|outdated, profiles, hooked_up, hooked_down, unhooked: [names]}.
    """
    found = api.get_resource("/system/script").get(name=SCRIPT_NAME)
    if not found:
        script = "missing"
    elif base_url and token and _norm(found[0].get("source")) == _norm(function_source(base_url, token)):
        script = "current"
    else:
        script = "outdated"
    profiles = api.get_resource("/ppp/profile").get()
    up = [p for p in profiles if MARKER in (p.get("on-up") or "")]
    down = [p for p in profiles if MARKER in (p.get("on-down") or "")]
    hooked = {p.get("name") for p in up} & {p.get("name") for p in down}
    return {
        "script": script, "profiles": len(profiles), "hooked_up": len(up), "hooked_down": len(down),
        "unhooked": sorted(p.get("name", "") for p in profiles if p.get("name") not in hooked),
    }


def install(api, base_url, token):
    """
    Add or update the script and put the hook line first in every PPP
    profile's on-up and on-down script. Needs an API user with write access.
    Returns {changed: [profile names], profiles}.
    """
    scripts = api.get_resource("/system/script")
    source = function_source(base_url, token)
    found = scripts.get(name=SCRIPT_NAME)
    if found:
        scripts.set(id=_id(found[0]), source=source, comment=SCRIPT_COMMENT)
    else:
        scripts.add(name=SCRIPT_NAME, source=source, comment=SCRIPT_COMMENT)

    profiles = api.get_resource("/ppp/profile")
    changed, all_profiles = [], profiles.get()
    for p in all_profiles:
        updates = {}
        for key, event in PROFILE_HOOKS.items():
            current = p.get(key) or ""
            new = with_hook(current, event)
            if new != current:
                updates[key] = new
        if updates:
            profiles.set(id=_id(p), **updates)
            changed.append(p.get("name", ""))
    return {"changed": changed, "profiles": len(all_profiles)}


def uninstall(api):
    """Remove the hook lines from every PPP profile and delete the script."""
    profiles = api.get_resource("/ppp/profile")
    changed = []
    for p in profiles.get():
        updates = {key: without_hook(p.get(key) or "") for key in PROFILE_HOOKS
                   if MARKER in (p.get(key) or "")}
        if updates:
            profiles.set(id=_id(p), **updates)
            changed.append(p.get("name", ""))
    scripts = api.get_resource("/system/script")
    for s in scripts.get(name=SCRIPT_NAME):
        scripts.remove(id=_id(s))
    return {"changed": changed}


def send_test(api):
    """Have the router run the script with no arguments, which sends a test call to LQ-Sync."""
    api.get_resource("/system/script").call("run", {"number": SCRIPT_NAME})


# ── Incoming events ─────────────────────────────────────────────────────────

def router_for_token(routers, token):
    """The router (from deduped config routers) whose webhook_token is token, or None."""
    if not token:
        return None
    hit = None
    for r in routers:
        known = str(r.get("webhook_token") or "")
        # Compare against every router so the time taken does not reveal which token is close.
        if known and secrets.compare_digest(known.encode(), token.encode()):
            hit = r
    return hit


def parse_event(raw):
    """
    The event a router sent: {event, service, user, ip, caller_id}. A body
    that is not the expected JSON (e.g. a user name with a quote broke it)
    gives event "unknown", which rescans that router.
    """
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        data = None
    if not isinstance(data, dict):
        return {"event": "unknown", "service": "", "user": "", "ip": "", "caller_id": ""}
    event = str(data.get("event") or "").strip().lower()
    ip = str(data.get("ip") or "").strip()
    try:
        ip = str(ipaddress.ip_address(ip)) if ip else ""
    except ValueError:
        ip = ""
    return {
        "event": event if event in EVENTS else "unknown",
        "service": str(data.get("service") or "")[:16],
        "user": str(data.get("user") or "").strip()[:128],
        "ip": ip,
        "caller_id": str(data.get("caller_id") or "")[:64],
    }
