import logging
import sys

from aiohttp import web

from .config import Settings
from .web import create_web_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP client INFO logs include Telegram's bot-token-bearing API URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    web.run_app(create_web_app(settings), host="0.0.0.0", port=settings.port, access_log=None)


if __name__ == "__main__":
    main()
