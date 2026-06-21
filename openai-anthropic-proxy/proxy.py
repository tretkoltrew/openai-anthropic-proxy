import json
import logging
import math
import os
import ipaddress
import secrets
import sqlite3
import time
import uuid
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context


load_dotenv()

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"

UPSTREAM_PROVIDER = os.getenv("UPSTREAM_PROVIDER", "anthropic").strip().lower()
ANTHROPIC_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MODEL = os.getenv(
    "DEFAULT_MODEL",
    DEEPSEEK_DEFAULT_MODEL if UPSTREAM_PROVIDER == "deepseek" else ANTHROPIC_DEFAULT_MODEL,
)
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOKEN_LIMIT = int(os.getenv("DEFAULT_TOKEN_LIMIT", "1000000"))
REQUEST_TIMEOUT_SECONDS = 300
COUNT_TIMEOUT_SECONDS = 30
DB_PATH = os.getenv("DATABASE_PATH", "proxy.db")
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "1048576"))
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}
ADMIN_ALLOWED_IPS = [item.strip() for item in os.getenv("ADMIN_ALLOWED_IPS", "").split(",") if item.strip()]
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_CHAT_COMPLETIONS_URL = f"{DEEPSEEK_BASE_URL}/chat/completions"

SUPPORTED_MODELS = {
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
    "deepseek-chat",
    "deepseek-reasoner",
}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
logger = logging.getLogger("openai_anthropic_proxy")


def now_unix() -> int:
    return int(time.time())


def current_period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mask_key(api_key: Optional[str]) -> str:
    if not api_key:
        return "none"
    return f"{api_key[:8]}..."


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def request_ip_for_access_control() -> str:
    if TRUST_PROXY_HEADERS:
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return request.remote_addr or ""


def ip_matches_any(ip_value: str, allowed_values: List[str]) -> bool:
    if not allowed_values:
        return True
    try:
        parsed_ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False

    for allowed in allowed_values:
        try:
            if "/" in allowed and parsed_ip in ipaddress.ip_network(allowed, strict=False):
                return True
            if parsed_ip == ipaddress.ip_address(allowed):
                return True
        except ValueError:
            continue
    return False


@app.after_request
def add_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    response.headers.pop("Server", None)
    return response


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(413)
def request_too_large(_error):
    return json_error("Request body is too large", "invalid_request_error", 413)


@app.errorhandler(Exception)
def internal_error(error):
    logger.exception("Unhandled error: %s", error)
    return json_error("Internal server error", "server_error", 500)


def parse_positive_int(value: Any, field_name: str, default: Optional[int] = None) -> Tuple[Optional[int], Optional[str]]:
    if value is None:
        return default, None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, f"{field_name} must be an integer"
    if parsed <= 0:
        return None, f"{field_name} must be greater than zero"
    return parsed, None


def parse_float(value: Any, field_name: str, default: float) -> Tuple[Optional[float], Optional[str]]:
    if value is None:
        return default, None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, f"{field_name} must be a number"
    return parsed, None


def parse_bool(value: Any, field_name: str) -> Tuple[Optional[bool], Optional[str]]:
    if isinstance(value, bool):
        return value, None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True, None
        if lowered in {"false", "0", "no", "off"}:
            return False, None
    if isinstance(value, int) and value in {0, 1}:
        return bool(value), None
    return None, f"{field_name} must be a boolean"


def init_db() -> None:
    with open_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_limit INTEGER NOT NULL,
                used_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_tokens INTEGER NOT NULL DEFAULT 0,
                system_prompt TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                current_period TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_reset_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                period TEXT NOT NULL,
                client_ip TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                reserved_tokens INTEGER NOT NULL DEFAULT 0,
                charged_tokens INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        ensure_column(conn, "api_keys", "reserved_tokens", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "api_keys", "system_prompt", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "usage_logs", "reserved_tokens", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "usage_logs", "charged_tokens", "INTEGER NOT NULL DEFAULT 0")

        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("global_system_prompt", os.getenv("GLOBAL_SYSTEM_PROMPT", "")),
        )

        for user_key in split_csv(os.getenv("VALID_KEYS", "")):
            conn.execute(
                """
                INSERT OR IGNORE INTO api_keys
                    (key, name, token_limit, used_tokens, reserved_tokens, system_prompt,
                     enabled, current_period, created_at, updated_at, last_reset_at)
                VALUES (?, ?, ?, 0, 0, '', 1, ?, ?, ?, ?)
                """,
                (
                    user_key,
                    f"seed-{mask_key(user_key)}",
                    DEFAULT_TOKEN_LIMIT,
                    current_period(),
                    now_unix(),
                    now_unix(),
                    now_unix(),
                ),
            )


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def json_error(message: str, error_type: str, status_code: int):
    return jsonify({"error": {"message": message, "type": error_type}}), status_code


def extract_bearer_token() -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header[7:].strip()


def client_ip() -> str:
    return request_ip_for_access_control()


def anthropic_headers(api_key: str) -> Dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def bearer_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def get_upstream_api_key() -> Optional[str]:
    if UPSTREAM_PROVIDER == "deepseek":
        return os.getenv("DEEPSEEK_API_KEY")
    return os.getenv("ANTHROPIC_API_KEY")


def get_upstream_name() -> str:
    return UPSTREAM_PROVIDER if UPSTREAM_PROVIDER in {"anthropic", "deepseek"} else "anthropic"


def get_setting(name: str, default: str = "") -> str:
    with open_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (name,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(name: str, value: str) -> None:
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (name, value),
        )


def compose_system_prompt(key_row: sqlite3.Row) -> str:
    parts = [
        get_setting("global_system_prompt", "").strip(),
        str(key_row["system_prompt"] or "").strip(),
    ]
    return "\n\n".join(part for part in parts if part)


def row_to_key_info(row: sqlite3.Row, include_secret: bool = False) -> Dict[str, Any]:
    available = max(int(row["token_limit"]) - int(row["used_tokens"]) - int(row["reserved_tokens"]), 0)
    result = {
        "masked_key": mask_key(row["key"]),
        "name": row["name"],
        "token_limit": int(row["token_limit"]),
        "used_tokens": int(row["used_tokens"]),
        "reserved_tokens": int(row["reserved_tokens"]),
        "available_tokens": available,
        "system_prompt": row["system_prompt"],
        "enabled": bool(row["enabled"]),
        "current_period": row["current_period"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "last_reset_at": int(row["last_reset_at"]),
    }
    if include_secret:
        result["key"] = row["key"]
    return result


def reset_key_if_new_period(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    period = current_period()
    if row["current_period"] == period:
        return row

    conn.execute(
        """
        UPDATE api_keys
        SET used_tokens = 0, reserved_tokens = 0, current_period = ?, last_reset_at = ?, updated_at = ?
        WHERE key = ?
        """,
        (period, now_unix(), now_unix(), row["key"]),
    )
    updated = conn.execute("SELECT * FROM api_keys WHERE key = ?", (row["key"],)).fetchone()
    if updated is None:
        raise RuntimeError("API key disappeared during period reset")
    return updated


def get_key_for_request() -> Tuple[Optional[sqlite3.Row], Optional[Tuple[Response, int]]]:
    token = extract_bearer_token()
    if not token:
        return None, (jsonify({"error": "Invalid API key"}), 401)

    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (token,)).fetchone()
        if row is None:
            return None, (jsonify({"error": "Invalid API key"}), 401)

        row = reset_key_if_new_period(conn, row)
        if not bool(row["enabled"]):
            return None, json_error("API key is disabled", "key_disabled", 403)
        if available_tokens(row) <= 0:
            return None, json_error("Token quota exceeded", "quota_exceeded", 429)
        return row, None


def require_admin() -> Optional[Tuple[Response, int]]:
    if not ip_matches_any(request_ip_for_access_control(), ADMIN_ALLOWED_IPS):
        return jsonify({"error": "Not found"}), 404

    admin_key = os.getenv("ADMIN_KEY")
    provided_key = extract_bearer_token()
    if not admin_key or provided_key != admin_key:
        return jsonify({"error": "Not found"}), 404
    return None


def available_tokens(row: sqlite3.Row) -> int:
    return max(int(row["token_limit"]) - int(row["used_tokens"]) - int(row["reserved_tokens"]), 0)


def reserve_quota(api_key: str, input_tokens: int, requested_max_tokens: int) -> Tuple[Optional[Dict[str, int]], Optional[Tuple[Response, int]]]:
    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (api_key,)).fetchone()
        if row is None:
            return None, (jsonify({"error": "Invalid API key"}), 401)

        row = reset_key_if_new_period(conn, row)
        if not bool(row["enabled"]):
            return None, json_error("API key is disabled", "key_disabled", 403)

        available = available_tokens(row)
        if input_tokens >= available:
            return None, json_error("Token quota exceeded", "quota_exceeded", 429)

        allowed_max_tokens = min(requested_max_tokens, available - input_tokens)
        reserved_tokens = input_tokens + allowed_max_tokens
        if reserved_tokens <= 0:
            return None, json_error("Token quota exceeded", "quota_exceeded", 429)

        conn.execute(
            """
            UPDATE api_keys
            SET reserved_tokens = reserved_tokens + ?, updated_at = ?
            WHERE key = ?
            """,
            (reserved_tokens, now_unix(), api_key),
        )

    return {
        "input_tokens": input_tokens,
        "max_tokens": allowed_max_tokens,
        "reserved_tokens": reserved_tokens,
    }, None


def finalize_reservation(
    api_key: str,
    model: str,
    reserved_tokens: int,
    input_tokens: int,
    output_tokens: int,
    status: str,
    charge_reserved: bool = False,
) -> None:
    actual_tokens = max(int(input_tokens), 0) + max(int(output_tokens), 0)
    charged_tokens = reserved_tokens if charge_reserved else actual_tokens

    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE api_keys
            SET
                reserved_tokens = CASE
                    WHEN reserved_tokens >= ? THEN reserved_tokens - ?
                    ELSE 0
                END,
                used_tokens = used_tokens + ?,
                updated_at = ?
            WHERE key = ?
            """,
            (reserved_tokens, reserved_tokens, charged_tokens, now_unix(), api_key),
        )
        conn.execute(
            """
            INSERT INTO usage_logs
                (api_key, created_at, period, client_ip, model, input_tokens, output_tokens,
                 total_tokens, reserved_tokens, charged_tokens, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                api_key,
                now_unix(),
                current_period(),
                client_ip(),
                model,
                max(int(input_tokens), 0),
                max(int(output_tokens), 0),
                actual_tokens,
                reserved_tokens,
                charged_tokens,
                status,
            ),
        )

    log_request(api_key, model, input_tokens, output_tokens, charged_tokens, status)


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item["text"]))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def normalize_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    anthropic_messages: List[Dict[str, str]] = []

    for item in messages:
        role = item.get("role")
        if role == "system":
            continue

        content = content_to_text(item.get("content"))
        mapped_role = "assistant" if role == "assistant" else "user"
        if anthropic_messages and anthropic_messages[-1]["role"] == mapped_role:
            anthropic_messages[-1]["content"] = f"{anthropic_messages[-1]['content']}\n\n{content}".strip()
        else:
            anthropic_messages.append({"role": mapped_role, "content": content})

    return anthropic_messages


def normalize_openai_messages(messages: List[Dict[str, Any]], system_prompt: str) -> List[Dict[str, str]]:
    openai_messages: List[Dict[str, str]] = []
    if system_prompt:
        openai_messages.append({"role": "system", "content": system_prompt})

    for item in messages:
        role = item.get("role")
        if role == "system":
            continue
        mapped_role = "assistant" if role == "assistant" else "user"
        openai_messages.append({"role": mapped_role, "content": content_to_text(item.get("content"))})

    return openai_messages


def validate_chat_request(payload: Any) -> Optional[Tuple[str, int]]:
    if not isinstance(payload, dict):
        return "Request body must be a JSON object", 400
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array", 400
    for message in messages:
        if not isinstance(message, dict) or "role" not in message or "content" not in message:
            return "each message must contain role and content", 400
    if not any(message.get("role") != "system" for message in messages if isinstance(message, dict)):
        return "messages must contain at least one non-system message", 400
    return None


def choose_model(requested_model: Any) -> str:
    if isinstance(requested_model, str) and requested_model.strip():
        if get_upstream_name() == "deepseek":
            return requested_model.strip()
        return requested_model if requested_model in SUPPORTED_MODELS else DEFAULT_MODEL
    return DEFAULT_MODEL


def conservative_token_estimate(messages: List[Dict[str, str]], system_prompt: str) -> int:
    text_parts = [system_prompt]
    text_parts.extend(message["content"] for message in messages)
    char_count = sum(len(part) for part in text_parts)
    message_overhead = 12 * max(len(messages), 1)
    return max(math.ceil(char_count / 2) + message_overhead, 1)


def count_anthropic_input_tokens(
    anthropic_api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str,
) -> int:
    payload: Dict[str, Any] = {"model": model, "messages": messages}
    if system_prompt:
        payload["system"] = system_prompt

    try:
        response = requests.post(
            ANTHROPIC_COUNT_TOKENS_URL,
            headers=anthropic_headers(anthropic_api_key),
            json=payload,
            timeout=COUNT_TIMEOUT_SECONDS,
        )
        if response.ok:
            return int(response.json().get("input_tokens") or 0)
        logger.warning("Token count failed with status %s: %s", response.status_code, response.text)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Token count failed: %s", exc)

    return conservative_token_estimate(messages, system_prompt)


def build_anthropic_payload(
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str,
    max_tokens: int,
    temperature: float,
    stream: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if system_prompt:
        payload["system"] = system_prompt
    return payload


def build_deepseek_payload(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
    stream: bool,
) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }


def deepseek_usage(data: Dict[str, Any]) -> Dict[str, int]:
    usage = data.get("usage") if isinstance(data, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def normalize_openai_response(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    if "usage" not in data or not isinstance(data["usage"], dict):
        data["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if "model" not in data:
        data["model"] = model
    if "object" not in data:
        data["object"] = "chat.completion"
    if "created" not in data:
        data["created"] = now_unix()
    if "id" not in data:
        data["id"] = f"chatcmpl-{uuid.uuid4().hex}"
    return data


def extract_text_from_anthropic(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type", "text") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def openai_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def finish_reason_from_anthropic(stop_reason: Optional[str]) -> str:
    return "length" if stop_reason == "max_tokens" else "stop"


def transform_anthropic_response(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    usage = openai_usage(data.get("usage") or {})
    return {
        "id": f"chatcmpl-{data.get('id') or uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": now_unix(),
        "model": data.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": extract_text_from_anthropic(data.get("content")),
                },
                "finish_reason": finish_reason_from_anthropic(data.get("stop_reason")),
            }
        ],
        "usage": usage,
    }


def transform_anthropic_error(response: requests.Response):
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else {}
        message = error.get("message") if isinstance(error, dict) else response.text
        error_type = error.get("type") if isinstance(error, dict) else "anthropic_error"
    except ValueError:
        message = response.text or "Anthropic API error"
        error_type = "anthropic_error"
    return jsonify({"error": {"message": message, "type": error_type}}), response.status_code


def transform_openai_compatible_error(response: requests.Response):
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else {}
        if isinstance(error, dict):
            message = error.get("message", "Upstream API error")
            error_type = error.get("type", "upstream_error")
        else:
            message = str(error or response.text or "Upstream API error")
            error_type = "upstream_error"
    except ValueError:
        message = response.text or "Upstream API error"
        error_type = "upstream_error"
    return jsonify({"error": {"message": message, "type": error_type}}), response.status_code


def log_request(
    api_key: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    charged_tokens: int,
    status: str,
) -> None:
    logger.info(
        json.dumps(
            {
                "time": utc_timestamp(),
                "client_ip": client_ip(),
                "api_key": mask_key(api_key),
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "charged_tokens": charged_tokens,
                "status": status,
            },
            ensure_ascii=False,
        )
    )


def sse(data: Any) -> str:
    if isinstance(data, str):
        return f"data: {data}\n\n"
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


def openai_stream_chunk(
    chunk_id: str,
    model: str,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    choice: Dict[str, Any] = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": now_unix(),
        "model": model,
        "choices": [choice],
    }


def iter_anthropic_sse_lines(response: requests.Response) -> Iterable[Dict[str, Any]]:
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping invalid Anthropic stream event: %s", data)


def iter_openai_sse_lines(response: requests.Response) -> Iterable[Any]:
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            yield "[DONE]"
            break
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping invalid OpenAI-compatible stream event: %s", data)


def stream_openai_response(
    anthropic_response: requests.Response,
    api_key: str,
    model: str,
    reservation: Dict[str, int],
) -> Generator[str, None, None]:
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    input_tokens = reservation["input_tokens"]
    output_tokens = 0
    finish_reason = "stop"
    completed = False
    status = "ok"

    yield sse(openai_stream_chunk(chunk_id, model, {"role": "assistant", "content": ""}))

    try:
        for event in iter_anthropic_sse_lines(anthropic_response):
            event_type = event.get("type")

            if event_type == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or input_tokens)
                continue

            if event_type == "content_block_delta":
                delta = event.get("delta") or {}
                text = delta.get("text")
                if text:
                    yield sse(openai_stream_chunk(chunk_id, model, {"content": text}))
                continue

            if event_type == "message_delta":
                usage = event.get("usage") or {}
                output_tokens = int(usage.get("output_tokens") or output_tokens)
                finish_reason = finish_reason_from_anthropic((event.get("delta") or {}).get("stop_reason"))
                continue

            if event_type == "message_stop":
                completed = True
                continue

            if event_type == "error":
                status = "stream_error"
                error = event.get("error") or {}
                yield sse(
                    {
                        "error": {
                            "message": error.get("message", "Anthropic stream error"),
                            "type": error.get("type", "anthropic_error"),
                        }
                    }
                )
                break

        yield sse(openai_stream_chunk(chunk_id, model, {}, finish_reason))
        yield sse("[DONE]")
    except GeneratorExit:
        status = "stream_interrupted"
        raise
    finally:
        finalize_reservation(
            api_key=api_key,
            model=model,
            reserved_tokens=reservation["reserved_tokens"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status=status if completed or status != "ok" else "stream_incomplete",
            charge_reserved=not completed,
        )
        anthropic_response.close()


def stream_deepseek_response(
    upstream_response: requests.Response,
    api_key: str,
    model: str,
    reservation: Dict[str, int],
) -> Generator[str, None, None]:
    input_tokens = reservation["input_tokens"]
    output_tokens = 0
    completed = False
    status = "ok"

    try:
        for event in iter_openai_sse_lines(upstream_response):
            if event == "[DONE]":
                completed = True
                yield sse("[DONE]")
                break

            if isinstance(event, dict):
                usage = event.get("usage")
                if isinstance(usage, dict):
                    input_tokens = int(usage.get("prompt_tokens") or input_tokens)
                    output_tokens = int(usage.get("completion_tokens") or output_tokens)
                yield sse(event)
    except GeneratorExit:
        status = "stream_interrupted"
        raise
    finally:
        # DeepSeek/OpenAI-compatible streams often omit usage, so charge the reservation
        # unless usage was present and the stream completed cleanly.
        has_usage = output_tokens > 0
        finalize_reservation(
            api_key=api_key,
            model=model,
            reserved_tokens=reservation["reserved_tokens"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status=status if completed or status != "ok" else "stream_incomplete",
            charge_reserved=not (completed and has_usage),
        )
        upstream_response.close()


def generate_proxy_key() -> str:
    return f"sk-proxy-{secrets.token_urlsafe(32)}"


@app.post("/v1/chat/completions")
def chat_completions():
    key_row, auth_error = get_key_for_request()
    if auth_error is not None:
        return auth_error

    upstream = get_upstream_name()
    upstream_api_key = get_upstream_api_key()
    if not upstream_api_key:
        return json_error(f"{upstream.upper()} API key is not configured", "server_error", 500)

    payload = request.get_json(silent=True)
    validation_error = validate_chat_request(payload)
    if validation_error:
        message, status_code = validation_error
        return json_error(message, "invalid_request_error", status_code)

    requested_max_tokens, max_tokens_error = parse_positive_int(payload.get("max_tokens"), "max_tokens", DEFAULT_MAX_TOKENS)
    if max_tokens_error:
        return json_error(max_tokens_error, "invalid_request_error", 400)

    temperature, temperature_error = parse_float(payload.get("temperature"), "temperature", DEFAULT_TEMPERATURE)
    if temperature_error:
        return json_error(temperature_error, "invalid_request_error", 400)

    stream = bool(payload.get("stream"))
    model = choose_model(payload.get("model"))
    system_prompt = compose_system_prompt(key_row)

    if upstream == "deepseek":
        messages = normalize_openai_messages(payload["messages"], system_prompt)
        input_tokens = conservative_token_estimate(messages, "")
    else:
        messages = normalize_anthropic_messages(payload["messages"])
        input_tokens = count_anthropic_input_tokens(upstream_api_key, model, messages, system_prompt)

    reservation, reserve_error = reserve_quota(key_row["key"], input_tokens, requested_max_tokens)
    if reserve_error is not None:
        return reserve_error

    if upstream == "deepseek":
        upstream_payload = build_deepseek_payload(
            model=model,
            messages=messages,
            max_tokens=reservation["max_tokens"],
            temperature=temperature,
            stream=stream,
        )
        upstream_url = DEEPSEEK_CHAT_COMPLETIONS_URL
        upstream_headers = bearer_headers(upstream_api_key)
    else:
        upstream_payload = build_anthropic_payload(
            model=model,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=reservation["max_tokens"],
            temperature=temperature,
            stream=stream,
        )
        upstream_url = ANTHROPIC_MESSAGES_URL
        upstream_headers = anthropic_headers(upstream_api_key)

    try:
        upstream_response = requests.post(
            upstream_url,
            headers=upstream_headers,
            json=upstream_payload,
            stream=stream,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        finalize_reservation(key_row["key"], model, reservation["reserved_tokens"], 0, 0, "api_connection_error")
        return json_error(str(exc), "api_connection_error", 502)

    if not upstream_response.ok:
        finalize_reservation(key_row["key"], model, reservation["reserved_tokens"], 0, 0, "upstream_error")
        if upstream == "deepseek":
            return transform_openai_compatible_error(upstream_response)
        return transform_anthropic_error(upstream_response)

    if stream:
        stream_generator = (
            stream_deepseek_response(upstream_response, key_row["key"], model, reservation)
            if upstream == "deepseek"
            else stream_openai_response(upstream_response, key_row["key"], model, reservation)
        )
        return Response(
            stream_with_context(stream_generator),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        data = upstream_response.json()
    except ValueError:
        finalize_reservation(key_row["key"], model, reservation["reserved_tokens"], 0, 0, "invalid_upstream_response")
        return json_error("Upstream returned an invalid JSON response", "upstream_error", 502)

    result = normalize_openai_response(data, model) if upstream == "deepseek" else transform_anthropic_response(data, model)
    usage = result["usage"]
    finalize_reservation(
        api_key=key_row["key"],
        model=result["model"],
        reserved_tokens=reservation["reserved_tokens"],
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        status="ok",
    )
    return jsonify(result)


@app.get("/v1/models")
def list_models():
    _, auth_error = get_key_for_request()
    if auth_error is not None:
        return auth_error

    created = now_unix()
    models = (
        ["deepseek-chat", "deepseek-reasoner"]
        if get_upstream_name() == "deepseek"
        else [
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307",
        ]
    )
    return jsonify(
        {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "created": created, "owned_by": get_upstream_name()}
                for model in models
            ],
        }
    )


@app.post("/admin/keys/create")
def admin_create_key():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "client").strip()
    token_limit, token_limit_error = parse_positive_int(payload.get("token_limit"), "token_limit", DEFAULT_TOKEN_LIMIT)
    if token_limit_error:
        return json_error(token_limit_error, "invalid_request_error", 400)

    new_key = str(payload.get("key") or generate_proxy_key()).strip()
    system_prompt = str(payload.get("system_prompt") or "")

    with open_db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO api_keys
                    (key, name, token_limit, used_tokens, reserved_tokens, system_prompt,
                     enabled, current_period, created_at, updated_at, last_reset_at)
                VALUES (?, ?, ?, 0, 0, ?, 1, ?, ?, ?, ?)
                """,
                (new_key, name, token_limit, system_prompt, current_period(), now_unix(), now_unix(), now_unix()),
            )
        except sqlite3.IntegrityError:
            return json_error("API key already exists", "duplicate_key", 409)

        row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (new_key,)).fetchone()

    return jsonify({"ok": True, "key": new_key, "record": row_to_key_info(row, include_secret=False)})


@app.get("/admin/keys")
def admin_list_keys():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    with open_db() as conn:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return jsonify({"data": [row_to_key_info(row, include_secret=False) for row in rows]})


@app.post("/admin/keys/update")
def admin_update_key():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    payload = request.get_json(silent=True) or {}
    key = str(payload.get("key") or "").strip()
    if not key:
        return json_error("key is required", "invalid_request_error", 400)

    updates: List[str] = []
    values: List[Any] = []

    if "name" in payload:
        updates.append("name = ?")
        values.append(str(payload["name"]))
    if "token_limit" in payload:
        token_limit, token_limit_error = parse_positive_int(payload["token_limit"], "token_limit")
        if token_limit_error:
            return json_error(token_limit_error, "invalid_request_error", 400)
        updates.append("token_limit = ?")
        values.append(token_limit)
    if "system_prompt" in payload:
        updates.append("system_prompt = ?")
        values.append(str(payload["system_prompt"] or ""))
    if "enabled" in payload:
        enabled, enabled_error = parse_bool(payload["enabled"], "enabled")
        if enabled_error:
            return json_error(enabled_error, "invalid_request_error", 400)
        updates.append("enabled = ?")
        values.append(1 if enabled else 0)

    if not updates:
        return json_error("nothing to update", "invalid_request_error", 400)

    updates.append("updated_at = ?")
    values.append(now_unix())
    values.append(key)

    with open_db() as conn:
        conn.execute(f"UPDATE api_keys SET {', '.join(updates)} WHERE key = ?", values)
        row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (key,)).fetchone()
        if row is None:
            return jsonify({"error": "API key not found"}), 404

    return jsonify({"ok": True, "record": row_to_key_info(row, include_secret=False)})


@app.post("/admin/keys/remove")
def admin_remove_key():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    payload = request.get_json(silent=True) or {}
    key = str(payload.get("key") or "").strip()
    if not key:
        return json_error("key is required", "invalid_request_error", 400)

    with open_db() as conn:
        cursor = conn.execute("DELETE FROM api_keys WHERE key = ?", (key,))
    return jsonify({"ok": True, "removed": cursor.rowcount > 0, "key": mask_key(key)})


@app.post("/admin/keys/reset")
def admin_reset_key():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    payload = request.get_json(silent=True) or {}
    key = payload.get("key")

    with open_db() as conn:
        if key:
            conn.execute(
                """
                UPDATE api_keys
                SET used_tokens = 0, reserved_tokens = 0, current_period = ?, last_reset_at = ?, updated_at = ?
                WHERE key = ?
                """,
                (current_period(), now_unix(), now_unix(), str(key)),
            )
            row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (str(key),)).fetchone()
            if row is None:
                return jsonify({"error": "API key not found"}), 404
            return jsonify({"ok": True, "record": row_to_key_info(row, include_secret=False)})

        conn.execute(
            """
            UPDATE api_keys
            SET used_tokens = 0, reserved_tokens = 0, current_period = ?, last_reset_at = ?, updated_at = ?
            """,
            (current_period(), now_unix(), now_unix()),
        )

    return jsonify({"ok": True, "reset": "all"})


@app.get("/admin/usage")
def admin_usage():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    key = request.args.get("key")
    period = request.args.get("period") or current_period()

    query = """
        SELECT api_key, period, COUNT(*) AS requests,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(total_tokens) AS actual_tokens,
               SUM(charged_tokens) AS charged_tokens
        FROM usage_logs
        WHERE period = ?
    """
    params: List[Any] = [period]
    if key:
        query += " AND api_key = ?"
        params.append(key)
    query += " GROUP BY api_key, period ORDER BY charged_tokens DESC"

    with open_db() as conn:
        rows = conn.execute(query, params).fetchall()

    return jsonify(
        {
            "period": period,
            "data": [
                {
                    "masked_key": mask_key(row["api_key"]),
                    "requests": int(row["requests"] or 0),
                    "input_tokens": int(row["input_tokens"] or 0),
                    "output_tokens": int(row["output_tokens"] or 0),
                    "actual_tokens": int(row["actual_tokens"] or 0),
                    "charged_tokens": int(row["charged_tokens"] or 0),
                }
                for row in rows
            ],
        }
    )


@app.get("/admin/settings")
def admin_get_settings():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    return jsonify({"global_system_prompt": get_setting("global_system_prompt", "")})


@app.post("/admin/settings/system-prompt")
def admin_set_system_prompt():
    admin_error = require_admin()
    if admin_error is not None:
        return admin_error

    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("system_prompt") or "")
    set_setting("global_system_prompt", prompt)
    return jsonify({"ok": True, "global_system_prompt": prompt})


@app.get("/health")
def health():
    return jsonify({"ok": True})


init_db()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "3456"))
    host = os.getenv("BIND_HOST", "127.0.0.1")
    app.run(host=host, port=port, threaded=True)
