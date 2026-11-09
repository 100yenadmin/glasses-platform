"""`hermes glasses`: pair a device in one command, list devices, revoke one, or try it from the terminal.

Pairing creates a random token, adds `user:device=token` to GLASSES_DEVICES in the profile's .env (which Hermes
reads before config.yaml, so the entries already in config.yaml are carried over), and prints the connect URL,
with a QR code when the `qrcode` package is installed. The gateway reads devices at start: restart it afterwards.
"""
import ipaddress
import os
import re
import secrets
import socket
from urllib.parse import urlsplit

from .devices import parse_devices

NAME = re.compile(r'[A-Za-z0-9_.-]{1,64}')
WILDCARD = {'', '0.0.0.0', '::', '[::]'}


def _setting(key, default=''):
    """The value the gateway will use: GLASSES_<KEY> from the environment or .env, else config.yaml."""
    from hermes_cli.config import get_env_value, load_config
    value = get_env_value('GLASSES_' + key.upper())
    if value not in (None, ''):
        return value
    platform = ((load_config() or {}).get('platforms') or {}).get('glasses') or {}
    value = (platform.get('extra') or {}).get(key)
    return default if value in (None, '') else value


def _yaml_devices():
    from hermes_cli.config import load_config
    platform = ((load_config() or {}).get('platforms') or {}).get('glasses') or {}
    return str((platform.get('extra') or {}).get('devices') or '')


def _entries():
    """The configured device list as (user, device, token), in order. GLASSES_TOKEN is not part of it."""
    spec = str(_setting('devices') or '')
    table = parse_devices(spec)  # raises on a malformed list, before anything is written
    return [(identity.user, identity.device, token) for token, identity in table.items()]


def _save(entries):
    from hermes_cli.config import remove_env_value, save_env_value
    if entries:
        save_env_value('GLASSES_DEVICES', ','.join(f'{u}:{d}={t}' for u, d, t in entries))
    else:
        remove_env_value('GLASSES_DEVICES')


def _lan_address():
    """This machine's address on the local network: a private address when it has one (a VPN can make the
    default route's address a public one). The UDP connect sends nothing."""
    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('10.255.255.255', 1))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        candidates += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    usable = [a for a in candidates if not ipaddress.ip_address(a).is_loopback]
    private = [a for a in usable if ipaddress.ip_address(a).is_private]
    return (private or usable or ['localhost'])[0]


def connect_url(token, host=None, port=None, scheme='ws'):
    host = host or str(_setting('host', '0.0.0.0'))
    if host in WILDCARD:
        host = _lan_address()
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    port = int(port or _setting('port', 8765))
    path = str(_setting('path', '/voice'))
    return f'{scheme}://{host}:{port}{path}?token={token}'


def _qr(text):
    try:
        import qrcode
    except ImportError:
        return None
    code = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    code.add_data(text)
    code.make(fit=True)
    rows = code.get_matrix()
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    glyph = {(True, True): '█', (True, False): '▀', (False, True): '▄', (False, False): ' '}
    return '\n'.join(''.join(glyph[top, bottom] for top, bottom in zip(rows[i], rows[i + 1]))
                     for i in range(0, len(rows), 2))


def pair(args):
    user, device = args.user, args.device
    if not (NAME.fullmatch(user) and NAME.fullmatch(device)):
        print('User and device names may use letters, digits, "_", "." and "-" (up to 64).')
        return 2
    entries = _entries()
    existing = [e for e in entries if e[:2] == (user, device)]
    if existing and not args.replace:
        print(f'{user}:{device} is already paired. Pass --replace to give it a new token '
              '(the old one stops working).')
        return 1
    token = secrets.token_urlsafe(32)
    entries = [e for e in entries if e[:2] != (user, device)] + [(user, device, token)]
    _save(entries)
    if token not in str(_setting('devices') or ''):  # Hermes refuses .env writes on some managed installs
        print('Could not save the new device to the profile .env (see the message above). Nothing was paired.')
        return 1
    xiaozhi = getattr(args, 'xiaozhi', False)
    scheme = ('https' if args.wss else 'http') if xiaozhi else ('wss' if args.wss else 'ws')
    url = connect_url(token, args.host, args.port, scheme)
    if xiaozhi:
        parts = urlsplit(url)
        url = f'{parts.scheme}://{parts.netloc}/xiaozhi/ota/{token}'
    print(f'Paired {user}:{device}. It shares one conversation with every other device of {user}.\n')
    picture = None if args.no_qr or xiaozhi else _qr(url)
    if picture:
        print(picture + '\n')
    elif not (args.no_qr or xiaozhi):
        print('(No QR code: the qrcode package is missing from the plugin\'s environment. Reinstall in a terminal '
              'and answer y: `hermes plugins install 100yenadmin/glasses-platform --force --enable`; or use the URL.)\n')
    if xiaozhi:
        print(f'OTA address:\n  {url}\n')
        print('Paste it into the device Wi-Fi setup page: "OTA address" under advanced settings; then push to talk.')
    else:
        print(f'Connect URL (open it in the glasses app, or scan the code):\n  {url}\n')
    print('Anyone with this URL can talk to your agent: keep it private. Revoke it with '
          f'`hermes glasses revoke {user} {device}`.')
    print('Restart the gateway to load the new device: `hermes gateway restart`.')
    if not args.host and str(_setting('host', '0.0.0.0')) in WILDCARD:
        print('The address is this machine on your local network. Your phone must be on the same network, '
              'or pass --host (for example a Tailscale name).')
    return 0


def devices(args):
    entries = _entries()
    single = str(_setting('token') or '').strip()
    if not entries and not single:
        print('No devices paired yet. Run `hermes glasses pair <user> [<device>]`.')
        return 0
    for user, device, _ in entries:
        print(f'{user}:{device}')
    if single:
        print('owner:default (GLASSES_TOKEN)')
    return 0


def revoke(args):
    entries = _entries()
    kept = [e for e in entries if e[:2] != (args.user, args.device)]
    if len(kept) == len(entries):
        print(f'{args.user}:{args.device} is not in GLASSES_DEVICES.')
        return 1
    if not kept and _yaml_devices().strip():
        # An empty GLASSES_DEVICES counts as unset, so config.yaml's devices would apply again.
        print(f'{args.user}:{args.device} is the last device, and config.yaml also lists devices '
              '(platforms.glasses.extra.devices) that would apply again. Remove them there, then run this again.')
        return 1
    _save(kept)
    if any(e[:2] == (args.user, args.device) for e in _entries()):  # the .env write was refused
        print(f'Could not update the profile .env (see the message above); {args.user}:{args.device} is still paired.')
        return 1
    print(f'Revoked {args.user}:{args.device}. Restart the gateway (`hermes gateway restart`) to disconnect it.')
    yaml = parse_devices(_yaml_devices()) if _yaml_devices().strip() else {}
    if any((i.user, i.device) == (args.user, args.device) for i in yaml.values()):
        print('config.yaml still lists it; the .env list overrides it, but remove it there too.')
    return 0


def _simulator():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent / 'sim' / 'glasses_sim.py'
    spec = importlib.util.spec_from_file_location('glasses_platform_sim', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def try_it(args):
    """Talk to the agent from this terminal as a pair of glasses with a camera, through the running gateway."""
    import asyncio
    import aiohttp
    tokens = [t for u, d, t in _entries() if (u, d) == (args.user, args.device)]
    if not tokens:
        print(f'{args.user}:{args.device} is not paired. Run `hermes glasses pair {args.user} {args.device}`, '
              'then `hermes gateway restart`.')
        return 1
    host = str(_setting('host', '0.0.0.0'))
    url = connect_url(tokens[0], '127.0.0.1' if host in WILDCARD else host)
    sim = _simulator()
    photo = args.photo or sim.DEFAULT_PHOTO
    print(f'Connecting as {args.user}:{args.device}: glasses whose camera sees {os.path.basename(str(photo))}.')
    print('Type what you would say and press Enter; an empty line quits. Try "What am I looking at?" or '
          '"Remind me in one minute to stretch."\n')
    try:
        code = asyncio.run(sim.chat(url.split('?')[0], tokens[0], device=args.device, photo=photo))
    except (aiohttp.ClientError, OSError) as error:
        print(f'Could not reach the gateway at {url.split("?")[0]} ({type(error).__name__}). '
              'Is it running? `hermes gateway restart`')
        return 2
    except KeyboardInterrupt:
        return 0
    if code == 3:
        print('The gateway refused the token. Restart it so it loads newly paired devices: `hermes gateway restart`.')
    return code


def setup(parser):
    sub = parser.add_subparsers(dest='glasses_command', metavar='{pair,devices,revoke,try}')
    p = sub.add_parser('pair', help='Pair a device: create its token and print the connect URL and QR code')
    p.add_argument('user', nargs='?', default='owner', help='Whose agent conversation the device joins (default owner)')
    p.add_argument('device', nargs='?', default='glasses', help='A name for this device (default glasses)')
    p.add_argument('--host', help='Address the device should connect to (default: this machine on the LAN)')
    p.add_argument('--port', type=int, help='Port in the URL (default: the configured port, 8765)')
    p.add_argument('--wss', action='store_true', help='Print a wss:// URL, for a TLS proxy in front of the gateway')
    p.add_argument('--xiaozhi', action='store_true', help='Print an ESP32 xiaozhi OTA address instead of a QR code')
    p.add_argument('--replace', action='store_true', help='Give an already paired device a new token')
    p.add_argument('--no-qr', action='store_true', help='Print the URL only')
    p.set_defaults(glasses_func=pair)
    d = sub.add_parser('devices', help='List paired devices (tokens are not shown)')
    d.set_defaults(glasses_func=devices)
    r = sub.add_parser('revoke', help="Remove a device's token")
    r.add_argument('user')
    r.add_argument('device')
    r.set_defaults(glasses_func=revoke)
    t = sub.add_parser('try', help='Talk to your agent from this terminal, as a pair of glasses with a camera')
    t.add_argument('user', nargs='?', default='owner')
    t.add_argument('device', nargs='?', default='glasses', help='A paired device to connect as (default glasses)')
    t.add_argument('--photo', help='The picture the camera "sees" (default: a sample airport photo)')
    t.set_defaults(glasses_func=try_it)


def main(args):
    func = getattr(args, 'glasses_func', None)
    if func is None:
        print('Usage: hermes glasses {pair,devices,revoke,try} (see `hermes glasses --help`)')
        return 2
    try:
        return func(args)
    except ValueError as error:
        print(f'GLASSES_DEVICES is not valid ({error}); fix it in your .env or config.yaml first.')
        return 1


def register(ctx):
    if callable(getattr(ctx, 'register_cli_command', None)):
        ctx.register_cli_command('glasses', 'Smart glasses: pair a device, list or revoke devices', setup, main,
                                 description=__doc__)

