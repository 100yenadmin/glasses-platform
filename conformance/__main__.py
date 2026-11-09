"""Run with python -m conformance; see conformance/README.md."""
import argparse
import asyncio
from aiohttp import web
from . import check_server, fake_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check-server')
    check.add_argument('--url', required=True)
    check.add_argument('--token', required=True)
    check.add_argument('--http')
    check.add_argument('--full', action='store_true')
    check.add_argument('--timeout', type=float, default=10)
    check.add_argument('--json', action='store_true')
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8766)
    serve.add_argument('--token', required=True)
    serve.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if args.command == 'check-server':
        report = asyncio.run(check_server(args.url, args.token, args.http, args.full, timeout=args.timeout))
        report.print(args.json)
        raise SystemExit(0 if report.ok else 1)

    async def run():
        app = fake_app(args.token, on_report=lambda report: report.print(args.json))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, '127.0.0.1', args.port)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            print(f'READY {port}', flush=True)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
