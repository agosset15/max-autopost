import json
import logging
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Webhook (Telegram)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://example.com/webhook/tg
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is required in .env")

TELEGRAM_WEBHOOK_PATH: str = os.getenv("TELEGRAM_WEBHOOK_PATH", "/webhook/tg")
MAX_WEBHOOK_PATH: str = os.getenv("MAX_WEBHOOK_PATH", "/webhook/max")

TELEGRAM_WEBHOOK_URL = urlparse(WEBHOOK_URL)._replace(path=TELEGRAM_WEBHOOK_PATH).geturl()
MAX_WEBHOOK_URL = (
    os.getenv("MAX_WEBHOOK_URL") or urlparse(WEBHOOK_URL)._replace(path=MAX_WEBHOOK_PATH).geturl()
)

WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "9154"))

# Shared secret verifying inbound webhook requests (TG: X-Telegram-Bot-Api-Secret-Token,
# MAX: X-Max-Bot-Api-Secret). If unset, a random one is generated per process — webhooks
# are re-registered with it at startup, so set it explicitly only if you need it stable.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = secrets.token_urlsafe(32)
    logger.warning("WEBHOOK_SECRET not set; generated a random ephemeral secret for this run.")

PROXY_URL = os.getenv("PROXY_URL", None)

BOTAPI_URL = os.getenv("BOTAPI_URL", None)
BOTAPI_FILE_URL = os.getenv("BOTAPI_FILE_URL", None)
is_local_api = BOTAPI_URL is not None

# maxapi points at the host MAX currently asks bots to use. Set this only to
# pin another one — to fall back if that host misbehaves, or to move ahead of a
# library release when MAX changes it again.
MAX_API_URL = os.getenv("MAX_API_URL", None)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MESSAGE_MAP_TTL = int(os.getenv("MESSAGE_MAP_TTL", str(10 * 24 * 60 * 60)))

_CHANNELS_FILE = os.getenv("CHANNELS_FILE", "channels.json")

try:
    with open(_CHANNELS_FILE, encoding="utf-8") as _f:
        _entries = json.load(_f)
except FileNotFoundError:
    raise RuntimeError(
        f"Channel mapping file '{_CHANNELS_FILE}' not found. "
        "Copy channels.example.json to channels.json and fill in your IDs."
    )


@dataclass(frozen=True)
class Route:
    """One directed source -> destination edge.

    Every entry in channels.json produces one Route per direction it enables.
    Both endpoints may appear in many routes: one tg_id can fan out to several
    max_id, and one max_id can fan out to several tg_id.
    """

    tg_id: int
    max_id: int
    allowed_user_ids: frozenset[int] | None = None
    allowed_thread_ids: frozenset[int] | None = None
    sign_names: bool = False


def _opt_set(value: list[str] | None) -> frozenset[int] | None:
    return frozenset(int(x) for x in value) if value else None


# tg_id -> routes fed by channel_post updates
TG_CHANNEL_ROUTES: dict[int, list[Route]] = {}
# tg_id -> routes fed by group/supergroup message updates
TG_GROUP_ROUTES: dict[int, list[Route]] = {}
# max_id -> routes fed by MAX message updates
MAX_ROUTES: dict[int, list[Route]] = {}

# (map name, tg_id, max_id) already seen — duplicate edges would double-post
_seen: set[tuple[str, int, int]] = set()


def _add(table: dict[int, list[Route]], name: str, key: int, route: Route) -> None:
    edge = (name, route.tg_id, route.max_id)
    if edge in _seen:
        raise RuntimeError(
            f"Duplicate {name} route tg_id={route.tg_id} max_id={route.max_id} "
            f"in '{_CHANNELS_FILE}' — it would forward the same message twice."
        )
    _seen.add(edge)
    table.setdefault(key, []).append(route)


for _e in _entries:
    _entry_type = _e.get("type", "channel")
    _direction = _e.get("direction", "tg_to_max")
    _tg_id = int(_e["tg_id"])
    _max_id = int(_e["max_id"])

    _sign_names = bool(_e.get("sign_names", False))
    _bidirectional = bool(_e.get("bidirectional", False))
    _allowed_users = _opt_set(_e.get("allowed_user_ids"))
    _allowed_threads = _opt_set(_e.get("allowed_thread_ids"))

    if _entry_type not in ("channel", "group"):
        raise RuntimeError(f"Unknown entry type: {_entry_type!r}")

    if _bidirectional:
        if _entry_type != "group":
            raise RuntimeError(
                f"Entry tg_id={_tg_id} max_id={_max_id}: bidirectional only supported for type=group."
            )
        if "direction" in _e:
            logger.warning(
                "Entry tg_id=%d max_id=%d: bidirectional=true, direction=%r ignored.",
                _tg_id,
                _max_id,
                _direction,
            )
        _add(
            TG_GROUP_ROUTES,
            "tg_to_max",
            _tg_id,
            Route(
                tg_id=_tg_id,
                max_id=_max_id,
                allowed_user_ids=_allowed_users,
                allowed_thread_ids=_allowed_threads,
                sign_names=_sign_names,
            ),
        )
        _add(
            MAX_ROUTES,
            "max_to_tg",
            _max_id,
            Route(
                tg_id=_tg_id,
                max_id=_max_id,
                allowed_user_ids=_allowed_users,
                allowed_thread_ids=None,
                sign_names=_sign_names,
            ),
        )
        continue

    if _direction == "max_to_tg":
        if _allowed_threads:
            logger.warning(
                "Entry tg_id=%d max_id=%d is max_to_tg; "
                "allowed_thread_ids are ignored in max_to_tg direction.",
                _tg_id,
                _max_id,
            )
        _add(
            MAX_ROUTES,
            "max_to_tg",
            _max_id,
            Route(
                tg_id=_tg_id,
                max_id=_max_id,
                allowed_user_ids=_allowed_users,
                allowed_thread_ids=None,
                sign_names=_sign_names,
            ),
        )
        continue

    if _direction != "tg_to_max":
        raise RuntimeError(f"Unknown direction: {_direction!r}")

    if _entry_type == "channel":
        if _sign_names:
            logger.warning(
                "Entry tg_id=%d max_id=%d: sign_names ignored (type=channel, only type=group supported).",
                _tg_id,
                _max_id,
            )
        if _allowed_users or _allowed_threads:
            logger.warning(
                "Entry tg_id=%d max_id=%d: allowed_user_ids/allowed_thread_ids ignored "
                "(type=channel — channel posts carry no sender or thread).",
                _tg_id,
                _max_id,
            )
        # Channel posts have no from_user and no thread, so filters cannot apply.
        _add(TG_CHANNEL_ROUTES, "tg_to_max", _tg_id, Route(tg_id=_tg_id, max_id=_max_id))
    else:
        _add(
            TG_GROUP_ROUTES,
            "tg_to_max",
            _tg_id,
            Route(
                tg_id=_tg_id,
                max_id=_max_id,
                allowed_user_ids=_allowed_users,
                allowed_thread_ids=_allowed_threads,
                sign_names=_sign_names,
            ),
        )

if not TG_CHANNEL_ROUTES and not TG_GROUP_ROUTES and not MAX_ROUTES:
    raise RuntimeError(f"'{_CHANNELS_FILE}' contains no mappings.")

# Pairs wired in both directions (bidirectional groups, or two mirrored
# entries). In such a pair a MAX message may be either an original or the copy
# of a TG message, so propagating a removal needs the post state to tell which.
MIRRORED_PAIRS: frozenset[tuple[int, int]] = frozenset(
    (tg_id, max_id)
    for name, tg_id, max_id in _seen
    if name == "tg_to_max" and ("max_to_tg", tg_id, max_id) in _seen
)

# A channel mirrored in both directions loops: the bot's own channel_post comes
# back as an update. Groups are safe — TG does not deliver the bot's own messages.
for _tg_id, _routes in TG_CHANNEL_ROUTES.items():
    for _r in _routes:
        if any(_back.tg_id == _tg_id for _back in MAX_ROUTES.get(_r.max_id, ())):
            logger.warning(
                "Mirrored channel pair tg_id=%d max_id=%d (tg_to_max + max_to_tg): "
                "channel posts made by the bot will be forwarded back and loop.",
                _tg_id,
                _r.max_id,
            )
