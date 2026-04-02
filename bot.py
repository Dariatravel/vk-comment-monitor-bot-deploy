import os
import re
import sqlite3
import sys
import time
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

import requests


HELP_TEXT = (
    "Я слежу за комментариями к постам ВК.\n\n"
    "Что можно отправить:\n"
    "- доступ <секретный_код>\n"
    "- ссылку на пост VK, например https://vk.com/wall-123_456\n"
    "- список\n"
    "- удалить 2\n"
    "- удалить https://vk.com/wall-123_456\n"
    "- стоп\n"
    "- помощь"
)

URL_PATTERN = re.compile(r"wall(-?\d+)_(-?\d+)")


class BotError(Exception):
    pass


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def require_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise BotError(f"Не заполнена переменная окружения {name}")
    return value


def parse_bool_env(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise BotError(f"Некорректное булево значение для {name}: {value}")


def normalize_url(owner_id: int, post_id: int) -> str:
    return f"https://vk.com/wall{owner_id}_{post_id}"


def parse_post_reference(text: str) -> Optional[Tuple[int, int, str]]:
    match = URL_PATTERN.search(text)
    if not match:
        return None
    owner_id = int(match.group(1))
    post_id = int(match.group(2))
    return owner_id, post_id, normalize_url(owner_id, post_id)


def chunked(items: List[dict], size: int) -> Iterable[List[dict]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


@dataclass
class Config:
    group_id: int
    group_token: str
    reader_token: str
    allowed_user_id: int
    strict_dialog_mode: bool
    reader_token_ttl_seconds: int
    reader_token_warn_before_seconds: int
    access_code: str
    api_version: str
    check_interval_seconds: int
    message_check_interval_seconds: int
    database_path: Path


class Storage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    post_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    last_seen_comment_id INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(owner_id, post_id)
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_ref INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(post_ref, user_id)
                );

                CREATE TABLE IF NOT EXISTS dialog_state (
                    user_id INTEGER PRIMARY KEY,
                    last_seen_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id INTEGER PRIMARY KEY,
                    authorized_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def add_subscription(
        self,
        owner_id: int,
        post_id: int,
        url: str,
        user_id: int,
        last_seen_comment_id: int,
    ) -> Tuple[bool, bool]:
        now = datetime.utcnow().isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM posts WHERE owner_id = ? AND post_id = ?",
                (owner_id, post_id),
            ).fetchone()

            created_post = existing is None
            if created_post:
                connection.execute(
                    """
                    INSERT INTO posts (owner_id, post_id, url, added_at, last_seen_comment_id, is_active)
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (owner_id, post_id, url, now, last_seen_comment_id),
                )
                post_ref = connection.execute(
                    "SELECT id FROM posts WHERE owner_id = ? AND post_id = ?",
                    (owner_id, post_id),
                ).fetchone()["id"]
            else:
                post_ref = existing["id"]
                connection.execute(
                    "UPDATE posts SET is_active = 1, url = ? WHERE id = ?",
                    (url, post_ref),
                )

            existing_subscription = connection.execute(
                "SELECT id FROM subscriptions WHERE post_ref = ? AND user_id = ?",
                (post_ref, user_id),
            ).fetchone()
            created_subscription = existing_subscription is None
            if created_subscription:
                connection.execute(
                    """
                    INSERT INTO subscriptions (post_ref, user_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (post_ref, user_id, now),
                )

        return created_post, created_subscription

    def list_user_posts(self, user_id: int) -> List[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT posts.id, posts.owner_id, posts.post_id, posts.url, posts.added_at
                FROM subscriptions
                JOIN posts ON posts.id = subscriptions.post_ref
                WHERE subscriptions.user_id = ? AND posts.is_active = 1
                ORDER BY subscriptions.created_at ASC
                """,
                (user_id,),
            ).fetchall()

    def remove_subscription(self, user_id: int, owner_id: int, post_id: int) -> bool:
        with self._connect() as connection:
            post = connection.execute(
                "SELECT id FROM posts WHERE owner_id = ? AND post_id = ? AND is_active = 1",
                (owner_id, post_id),
            ).fetchone()
            if post is None:
                return False

            deleted = connection.execute(
                "DELETE FROM subscriptions WHERE post_ref = ? AND user_id = ?",
                (post["id"], user_id),
            ).rowcount
            self._deactivate_if_unused(connection, post["id"])
            return deleted > 0

    def clear_user_subscriptions(self, user_id: int) -> int:
        with self._connect() as connection:
            post_refs = [
                row["post_ref"]
                for row in connection.execute(
                    "SELECT post_ref FROM subscriptions WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
            ]
            deleted = connection.execute(
                "DELETE FROM subscriptions WHERE user_id = ?",
                (user_id,),
            ).rowcount
            for post_ref in post_refs:
                self._deactivate_if_unused(connection, post_ref)
            return deleted

    def _deactivate_if_unused(self, connection: sqlite3.Connection, post_ref: int) -> None:
        remaining = connection.execute(
            "SELECT COUNT(*) AS count FROM subscriptions WHERE post_ref = ?",
            (post_ref,),
        ).fetchone()["count"]
        if remaining == 0:
            connection.execute(
                "UPDATE posts SET is_active = 0 WHERE id = ?",
                (post_ref,),
            )

    def get_monitored_posts(self) -> List[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT
                    posts.id,
                    posts.owner_id,
                    posts.post_id,
                    posts.url,
                    posts.last_seen_comment_id,
                    GROUP_CONCAT(subscriptions.user_id) AS user_ids
                FROM posts
                JOIN subscriptions ON subscriptions.post_ref = posts.id
                WHERE posts.is_active = 1
                GROUP BY posts.id, posts.owner_id, posts.post_id, posts.url, posts.last_seen_comment_id
                ORDER BY posts.id ASC
                """
            ).fetchall()

    def update_last_seen_comment_id(self, post_ref: int, comment_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE posts SET last_seen_comment_id = ? WHERE id = ?",
                (comment_id, post_ref),
            )

    def get_last_seen_message_id(self, user_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_seen_message_id FROM dialog_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["last_seen_message_id"]) if row else 0

    def set_last_seen_message_id(self, user_id: int, message_id: int) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dialog_state (user_id, last_seen_message_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen_message_id = excluded.last_seen_message_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, message_id, now),
            )

    def is_authorized(self, user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM authorized_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row is not None

    def authorize_user(self, user_id: int) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO authorized_users (user_id, authorized_at)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id, now),
            )

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_locked_dialog_peer_id(self) -> Optional[int]:
        value = self.get_setting("locked_dialog_peer_id")
        return int(value) if value is not None else None

    def set_locked_dialog_peer_id(self, peer_id: int) -> None:
        self.set_setting("locked_dialog_peer_id", str(peer_id))


class VkApi:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "vk-comment-monitor/1.0"})

    def _api_call(self, method: str, params: dict, token: Optional[str] = None) -> dict:
        payload = dict(params)
        payload["access_token"] = token or self.config.group_token
        payload["v"] = self.config.api_version
        response = self.session.post(
            f"https://api.vk.com/method/{method}",
            data=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            error = data["error"]
            raise BotError(
                f"VK API error {error.get('error_code')}: {error.get('error_msg')}"
            )
        return data["response"]

    def send_message(self, user_id: int, text: str) -> None:
        self._api_call(
            "messages.send",
            {
                "user_id": user_id,
                "message": text,
                "random_id": int(uuid4().int % 2_000_000_000),
            },
        )

    def get_conversations(self, count: int = 100) -> List[dict]:
        response = self._api_call(
            "messages.getConversations",
            {"count": count, "filter": "all"},
        )
        return response.get("items", [])

    def get_history(self, peer_id: int, count: int = 20) -> List[dict]:
        response = self._api_call(
            "messages.getHistory",
            {"peer_id": peer_id, "count": count},
        )
        return response.get("items", [])

    def get_latest_comment_id(self, owner_id: int, post_id: int) -> int:
        try:
            response = self._api_call(
                "wall.getComments",
                {
                    "owner_id": owner_id,
                    "post_id": post_id,
                    "count": 1,
                    "sort": "desc",
                },
                token=self.config.reader_token,
            )
        except BotError as error:
            self._raise_reader_token_hint(error)
        items = response.get("items", [])
        if not items:
            return 0
        return int(items[0]["id"])

    def get_new_comments(
        self,
        owner_id: int,
        post_id: int,
        last_seen_comment_id: int,
    ) -> Tuple[List[dict], Dict[int, dict], Dict[int, dict]]:
        comments: List[dict] = []
        profiles: Dict[int, dict] = {}
        groups: Dict[int, dict] = {}
        offset = 0

        while True:
            try:
                response = self._api_call(
                    "wall.getComments",
                    {
                        "owner_id": owner_id,
                        "post_id": post_id,
                        "count": 100,
                        "offset": offset,
                        "sort": "desc",
                        "extended": 1,
                        "fields": "screen_name",
                    },
                    token=self.config.reader_token,
                )
            except BotError as error:
                self._raise_reader_token_hint(error)
            items = response.get("items", [])
            for profile in response.get("profiles", []):
                profiles[int(profile["id"])] = profile
            for group in response.get("groups", []):
                groups[int(group["id"])] = group

            stop = False
            for item in items:
                comment_id = int(item["id"])
                if comment_id <= last_seen_comment_id:
                    stop = True
                    break
                comments.append(item)

            if stop or len(items) < 100:
                break
            offset += 100

        comments.sort(key=lambda item: int(item["id"]))
        return comments, profiles, groups

    def _raise_reader_token_hint(self, error: BotError) -> None:
        message = str(error)
        uses_group_token_for_reader = self.config.reader_token == self.config.group_token
        if "VK API error 27" in message and uses_group_token_for_reader:
            raise BotError(
                "Не хватает VK_READER_TOKEN. Для чужих постов нужен пользовательский токен "
                "с правом wall (добавьте переменную VK_READER_TOKEN в Railway)."
            ) from error
        if "VK API error 5" in message and "another ip address" in message.lower():
            raise BotError(
                "VK_READER_TOKEN привязан к другому IP. Выпустите новый токен и сразу обновите "
                "переменную VK_READER_TOKEN в Railway, не используя этот токен локально."
            ) from error
        raise error


class MonitorBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(config.database_path)
        self.vk = VkApi(config)
        if (
            self.config.strict_dialog_mode
            and self.storage.get_locked_dialog_peer_id() is None
            and self.storage.is_authorized(self.config.allowed_user_id)
        ):
            self.storage.set_locked_dialog_peer_id(self.config.allowed_user_id)
        self._sync_reader_token_tracking()

    def _sync_reader_token_tracking(self) -> None:
        if self.config.reader_token == self.config.group_token:
            return
        token_hash = sha256(self.config.reader_token.encode("utf-8")).hexdigest()
        saved_hash = self.storage.get_setting("reader_token_hash")
        if saved_hash == token_hash:
            return
        now_ts = int(time.time())
        self.storage.set_setting("reader_token_hash", token_hash)
        self.storage.set_setting("reader_token_seen_at", str(now_ts))
        self.storage.set_setting("reader_token_notify_stage", "none")

    def check_reader_token_health(self, now_ts: Optional[int] = None) -> None:
        if self.config.reader_token == self.config.group_token:
            return
        if self.config.reader_token_ttl_seconds <= 0:
            return

        self._sync_reader_token_tracking()
        seen_at_raw = self.storage.get_setting("reader_token_seen_at")
        if seen_at_raw is None:
            return
        try:
            seen_at = int(seen_at_raw)
        except ValueError:
            return

        current_ts = int(time.time()) if now_ts is None else int(now_ts)
        expires_at = seen_at + self.config.reader_token_ttl_seconds
        warning_at = expires_at - self.config.reader_token_warn_before_seconds
        notify_stage = self.storage.get_setting("reader_token_notify_stage") or "none"

        if current_ts >= expires_at and notify_stage != "expired":
            expires_label = datetime.utcfromtimestamp(expires_at).strftime("%d.%m %H:%M UTC")
            message = (
                "VK_READER_TOKEN истёк. Мониторинг комментариев чужих постов может перестать работать.\n"
                f"Ожидаемое время истечения: {expires_label}\n"
                "Обновите переменную VK_READER_TOKEN в Railway."
            )
            try:
                self.vk.send_message(self.config.allowed_user_id, message)
                self.storage.set_setting("reader_token_notify_stage", "expired")
            except (BotError, requests.RequestException) as error:
                print(f"Не удалось отправить уведомление об истечении VK_READER_TOKEN: {error}", file=sys.stderr)
            return

        if current_ts >= warning_at and notify_stage == "none":
            seconds_left = max(0, expires_at - current_ts)
            hours_left = seconds_left // 3600
            minutes_left = (seconds_left % 3600) // 60
            expires_label = datetime.utcfromtimestamp(expires_at).strftime("%d.%m %H:%M UTC")
            message = (
                "VK_READER_TOKEN скоро истечёт.\n"
                f"Осталось примерно: {hours_left} ч {minutes_left} мин\n"
                f"Ожидаемое время истечения: {expires_label}\n"
                "Подготовьте новый токен и обновите переменную VK_READER_TOKEN в Railway."
            )
            try:
                self.vk.send_message(self.config.allowed_user_id, message)
                self.storage.set_setting("reader_token_notify_stage", "warning")
            except (BotError, requests.RequestException) as error:
                print(f"Не удалось отправить предупреждение об истечении VK_READER_TOKEN: {error}", file=sys.stderr)

    def run(self) -> None:
        next_scan_at = 0.0
        next_message_poll_at = 0.0
        next_reader_token_health_check_at = 0.0

        print("Бот запущен. Проверяю сообщения и комментарии.")
        while True:
            now = time.time()
            if now >= next_scan_at:
                self.scan_posts()
                next_scan_at = now + self.config.check_interval_seconds
            if now >= next_message_poll_at:
                self.poll_messages()
                next_message_poll_at = now + self.config.message_check_interval_seconds
            if now >= next_reader_token_health_check_at:
                self.check_reader_token_health(now_ts=int(now))
                next_reader_token_health_check_at = now + 60

            time.sleep(1)

    def poll_messages(self) -> None:
        user_id = self.config.allowed_user_id
        try:
            history = self.vk.get_history(user_id, count=50)
        except (BotError, requests.RequestException) as error:
            print(f"Не удалось проверить сообщения: {error}", file=sys.stderr)
            return

        if not history:
            return

        latest_message_id = max(int(message.get("id", 0)) for message in history)
        if latest_message_id <= 0:
            return

        saved_message_id = self.storage.get_last_seen_message_id(user_id)
        if saved_message_id == 0:
            self.storage.set_last_seen_message_id(user_id, latest_message_id)
            return
        if latest_message_id <= saved_message_id:
            return

        new_messages = [
            message
            for message in history
            if int(message.get("id", 0)) > saved_message_id and int(message.get("out", 0)) == 0
        ]
        new_messages.sort(key=lambda message: int(message["id"]))

        for message in new_messages:
            self.handle_incoming_message(user_id, message)

        self.storage.set_last_seen_message_id(user_id, latest_message_id)

    def handle_incoming_message(self, user_id: int, message: dict) -> None:
        if user_id != self.config.allowed_user_id:
            return
        peer_id = int(message.get("peer_id", user_id))
        if self.config.strict_dialog_mode:
            locked_peer_id = self.storage.get_locked_dialog_peer_id()
            if locked_peer_id is not None and peer_id != locked_peer_id:
                return
        text = (message.get("text") or "").strip()

        try:
            reply = self.handle_message(user_id, text, peer_id=peer_id)
        except BotError as error:
            reply = f"Ошибка: {error}"
        except Exception as error:  # noqa: BLE001
            print(f"Непредвиденная ошибка: {error}", file=sys.stderr)
            reply = "Не получилось обработать сообщение. Попробуйте ещё раз."

        if reply:
            self.vk.send_message(user_id, reply)

    def handle_message(self, user_id: int, text: str, peer_id: Optional[int] = None) -> str:
        if user_id != self.config.allowed_user_id:
            return ""

        effective_peer_id = peer_id if peer_id is not None else user_id
        locked_peer_id = self.storage.get_locked_dialog_peer_id() if self.config.strict_dialog_mode else None
        if self.config.strict_dialog_mode and locked_peer_id is not None and effective_peer_id != locked_peer_id:
            return ""

        normalized = text.lower()
        access_command = f"доступ {self.config.access_code}".lower()

        if normalized == access_command:
            if self.config.strict_dialog_mode and locked_peer_id is None:
                self.storage.set_locked_dialog_peer_id(effective_peer_id)
            self.storage.authorize_user(user_id)
            if self.config.strict_dialog_mode and locked_peer_id is None:
                return (
                    "Доступ открыт. Диалог закреплён в жёстком режиме. "
                    "Команды принимаются только из этого диалога."
                )
            return "Доступ открыт. Теперь можно присылать ссылку на пост или команду `помощь`."

        if not self.storage.is_authorized(user_id):
            return "Доступ ограничен. Отправьте команду: доступ <секретный_код>"

        if not text:
            return HELP_TEXT
        if normalized in {"help", "помощь", "старт", "start"}:
            return HELP_TEXT
        if normalized in {"список", "list"}:
            return self.render_post_list(user_id)
        if normalized == "стоп":
            count = self.storage.clear_user_subscriptions(user_id)
            if count == 0:
                return "У вас нет активных постов в мониторинге."
            return f"Мониторинг остановлен. Удалено постов: {count}."
        if normalized.startswith("удалить "):
            return self.handle_remove(user_id, text[8:].strip())

        reference = parse_post_reference(text)
        if reference is None:
            return HELP_TEXT

        owner_id, post_id, canonical_url = reference
        latest_comment_id = self.vk.get_latest_comment_id(owner_id, post_id)
        _, created_subscription = self.storage.add_subscription(
            owner_id=owner_id,
            post_id=post_id,
            url=canonical_url,
            user_id=user_id,
            last_seen_comment_id=latest_comment_id,
        )

        if created_subscription:
            return (
                "Пост добавлен в мониторинг.\n"
                f"{canonical_url}\n"
                "Новые уведомления будут приходить сюда."
            )
        return "Этот пост уже есть у вас в мониторинге."

    def render_post_list(self, user_id: int) -> str:
        rows = self.storage.list_user_posts(user_id)
        if not rows:
            return "У вас пока нет постов в мониторинге."

        lines = ["Ваши посты в мониторинге:"]
        for index, row in enumerate(rows, start=1):
            lines.append(f"{index}. {row['url']}")
        lines.append("")
        lines.append("Чтобы удалить пост, отправьте: удалить 2")
        return "\n".join(lines)

    def handle_remove(self, user_id: int, target: str) -> str:
        if not target:
            return "После команды 'удалить' укажите номер из списка или ссылку."

        if target.isdigit():
            rows = self.storage.list_user_posts(user_id)
            index = int(target)
            if index < 1 or index > len(rows):
                return "Такого номера нет в списке."
            row = rows[index - 1]
            removed = self.storage.remove_subscription(
                user_id=user_id,
                owner_id=int(row["owner_id"]),
                post_id=int(row["post_id"]),
            )
            if removed:
                return f"Пост удалён из мониторинга:\n{row['url']}"
            return "Не удалось удалить пост."

        reference = parse_post_reference(target)
        if reference is None:
            return "Не удалось распознать ссылку на пост."
        owner_id, post_id, canonical_url = reference
        removed = self.storage.remove_subscription(user_id, owner_id, post_id)
        if removed:
            return f"Пост удалён из мониторинга:\n{canonical_url}"
        return "Этот пост не найден в вашем мониторинге."

    def scan_posts(self) -> None:
        rows = self.storage.get_monitored_posts()
        for row in rows:
            post_ref = int(row["id"])
            owner_id = int(row["owner_id"])
            post_id = int(row["post_id"])
            last_seen_comment_id = int(row["last_seen_comment_id"])
            user_ids = [int(value) for value in row["user_ids"].split(",") if value]
            user_ids = [user_id for user_id in user_ids if user_id == self.config.allowed_user_id]
            if not user_ids:
                continue

            try:
                comments, profiles, groups = self.vk.get_new_comments(
                    owner_id=owner_id,
                    post_id=post_id,
                    last_seen_comment_id=last_seen_comment_id,
                )
            except BotError as error:
                print(
                    f"Не удалось проверить {row['url']}: {error}",
                    file=sys.stderr,
                )
                continue
            except requests.RequestException as error:
                print(
                    f"Сетевая ошибка при проверке {row['url']}: {error}",
                    file=sys.stderr,
                )
                continue

            if not comments:
                continue

            for batch in chunked(comments, 5):
                for user_id in user_ids:
                    message = self.render_comment_batch(
                        owner_id=owner_id,
                        post_id=post_id,
                        comments=batch,
                        profiles=profiles,
                        groups=groups,
                    )
                    try:
                        self.vk.send_message(user_id, message)
                    except BotError as error:
                        print(
                            f"Не удалось отправить уведомление пользователю {user_id}: {error}",
                            file=sys.stderr,
                        )

            newest_comment_id = int(comments[-1]["id"])
            self.storage.update_last_seen_comment_id(post_ref, newest_comment_id)

    def render_comment_batch(
        self,
        owner_id: int,
        post_id: int,
        comments: List[dict],
        profiles: Dict[int, dict],
        groups: Dict[int, dict],
    ) -> str:
        lines = [f"Новые комментарии к посту:\n{normalize_url(owner_id, post_id)}"]
        for comment in comments:
            comment_id = int(comment["id"])
            author = self.resolve_author_name(int(comment["from_id"]), profiles, groups)
            created_at = datetime.fromtimestamp(int(comment["date"])).strftime("%d.%m %H:%M")
            text = self.prepare_comment_text(comment)
            comment_url = f"{normalize_url(owner_id, post_id)}?reply={comment_id}"
            lines.append("")
            lines.append(f"Автор: {author}")
            lines.append(f"Время: {created_at}")
            lines.append(f"Текст: {text}")
            lines.append(f"Ссылка: {comment_url}")
        return "\n".join(lines)

    def resolve_author_name(
        self,
        from_id: int,
        profiles: Dict[int, dict],
        groups: Dict[int, dict],
    ) -> str:
        if from_id > 0 and from_id in profiles:
            profile = profiles[from_id]
            return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        if from_id < 0 and abs(from_id) in groups:
            return groups[abs(from_id)].get("name", f"Сообщество {abs(from_id)}")
        return f"ID {from_id}"

    def prepare_comment_text(self, comment: dict) -> str:
        text = (comment.get("text") or "").strip()
        if text:
            return text
        attachments = comment.get("attachments") or []
        if attachments:
            return "[Комментарий без текста, но с вложением]"
        return "[Без текста]"


def build_config() -> Config:
    load_env(Path(__file__).with_name(".env"))

    group_id = int(require_env("VK_GROUP_ID"))
    group_token = require_env("VK_GROUP_TOKEN")
    reader_token = os.getenv("VK_READER_TOKEN", "").strip() or group_token
    allowed_user_id = int(require_env("ALLOWED_USER_ID"))
    strict_dialog_mode = parse_bool_env(os.getenv("STRICT_DIALOG_MODE", "1"), "STRICT_DIALOG_MODE")
    reader_token_ttl_seconds = int(os.getenv("VK_READER_TOKEN_TTL_SECONDS", "86400"))
    reader_token_warn_before_seconds = int(os.getenv("VK_READER_TOKEN_WARN_BEFORE_SECONDS", "10800"))
    access_code = require_env("ACCESS_CODE")
    api_version = os.getenv("VK_API_VERSION", "5.199")
    check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "90"))
    message_check_interval_seconds = int(os.getenv("MESSAGE_CHECK_INTERVAL_SECONDS", "5"))
    database_path = Path(os.getenv("DATABASE_PATH", "monitor.sqlite3"))

    if not database_path.is_absolute():
        database_path = Path(__file__).parent / database_path
    if reader_token_ttl_seconds < 0:
        raise BotError("VK_READER_TOKEN_TTL_SECONDS не может быть отрицательным")
    if reader_token_warn_before_seconds < 0:
        raise BotError("VK_READER_TOKEN_WARN_BEFORE_SECONDS не может быть отрицательным")

    return Config(
        group_id=group_id,
        group_token=group_token,
        reader_token=reader_token,
        allowed_user_id=allowed_user_id,
        strict_dialog_mode=strict_dialog_mode,
        reader_token_ttl_seconds=reader_token_ttl_seconds,
        reader_token_warn_before_seconds=reader_token_warn_before_seconds,
        access_code=access_code,
        api_version=api_version,
        check_interval_seconds=check_interval_seconds,
        message_check_interval_seconds=message_check_interval_seconds,
        database_path=database_path,
    )


def main() -> int:
    try:
        config = build_config()
        bot = MonitorBot(config)
        bot.run()
    except KeyboardInterrupt:
        print("\nБот остановлен.")
        return 0
    except BotError as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return 1
    except requests.RequestException as error:
        print(f"Сетевая ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
