"""python -m agent_harness starts the local web application; cli is optional."""

import argparse
from dataclasses import replace
from pathlib import Path

from .config import HarnessConfig, make_client


def main():
    parser = argparse.ArgumentParser(description="Agent Harness — local web and CLI hosts")
    parser.add_argument("interface", nargs="?", choices=("web", "api", "cli"), default="web")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=3000)
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    args = parser.parse_args()
    config = replace(HarnessConfig.from_env(), workdir=args.workdir.resolve())
    if not config.workdir.is_dir():
        parser.error("--workdir must be an existing directory")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 1 <= args.web_port <= 65535:
        parser.error("--web-port must be between 1 and 65535")
    if args.interface == "web" and args.dev and args.port == args.web_port:
        parser.error("API and frontend must use different ports")
    if args.interface == "cli":
        from .interfaces.cli import run

        run(config, make_client)
    elif args.interface == "api":
        import uvicorn
        from .interfaces.web.app import create_app

        origins = (f"http://127.0.0.1:{args.web_port}", f"http://localhost:{args.web_port}")
        print(f"Agent API: http://127.0.0.1:{args.port}")
        uvicorn.run(
            create_app(config, make_client, frontend_origins=origins),
            host="127.0.0.1",
            port=args.port,
            timeout_graceful_shutdown=3,
        )
    else:
        from .interfaces.web.host import serve_web

        try:
            serve_web(
                config,
                make_client,
                api_port=args.port,
                web_port=args.web_port,
                development=args.dev,
            )
        except RuntimeError as error:
            parser.error(str(error))


if __name__ == "__main__":
    main()
