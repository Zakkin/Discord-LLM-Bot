# lib/config_utils.py
from __future__ import annotations

from typing import Any, TypeVar

from ollama_bot import config_loader

T = TypeVar("T")


import os
import re

def get_config() -> Any:
    """現在ロードされている設定オブジェクトを取得する。"""
    return getattr(config_loader, "config", None)


def get_bot_config_name() -> str:
    """現在ロードされているBot設定の名前（例: 'config_img', 'config_test-server'）を取得する。"""
    raw = os.environ.get("OLLAMA_BOT_CONFIG", "").strip()
    if not raw:
        conf = get_config()
        if conf is not None:
            raw = getattr(conf, "__name__", "").split(".")[-1]
    if not raw:
        raw = getattr(config_loader, "CONFIG_MODULE", "").split(".")[-1]
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip()
    return cleaned or "default"


def get_bot_config_prefix() -> str:
    """現在のBot設定から環境変数用のプレフィックス（例: 'config_img' -> 'IMG', 'config_test-server' -> 'TEST_SERVER'）を取得する。"""
    name = get_bot_config_name()
    if name.startswith("config_"):
        name = name[7:]
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").upper()
    return cleaned


def resolve_bot_token() -> str:
    """現在のBotコンフィグに対応するDiscord Botトークンを優先解決する。

    優先順位:
    1. 個別プレフィックス付き環境変数（例: IMG_DISCORD_TOKEN, IMG_TOKEN, TEST_SERVER_DISCORD_TOKEN）
    2. コンフィグオブジェクト自身の TOKEN 属性（ダミー文字列除外）
    3. 共通環境変数 DISCORD_TOKEN
    """
    prefix = get_bot_config_prefix()
    candidates: list[str] = []
    if prefix:
        candidates.extend([f"{prefix}_DISCORD_TOKEN", f"{prefix}_TOKEN"])
    for key in candidates:
        val = os.environ.get(key, "").strip()
        if val and val != "YOUR_DISCORD_BOT_TOKEN_HERE":
            return val

    conf = get_config()
    if conf is not None:
        token = getattr(conf, "TOKEN", None)
        if token and str(token).strip() and str(token).strip() != "YOUR_DISCORD_BOT_TOKEN_HERE":
            return str(token).strip()

    return os.environ.get("DISCORD_TOKEN", "").strip()


def resolve_bot_guild_id() -> int:
    """現在のBotコンフィグに対応するギルドIDを優先解決する。

    優先順位:
    1. 個別プレフィックス付き環境変数（例: IMG_DISCORD_GUILD_ID, IMG_GUILD_ID）
    2. 共通環境変数 DISCORD_GUILD_ID / GUILD_ID
    3. コンフィグオブジェクト自身の GUILD_ID 属性
    4. cfg_int("GUILD_ID", 0)
    """
    prefix = get_bot_config_prefix()
    candidates: list[str] = []
    if prefix:
        candidates.extend([f"{prefix}_DISCORD_GUILD_ID", f"{prefix}_GUILD_ID"])
    for key in candidates:
        val = os.environ.get(key, "").strip()
        if val and val.isdigit():
            return int(val)

    common_env = os.environ.get("DISCORD_GUILD_ID", "").strip() or os.environ.get("GUILD_ID", "").strip()
    if common_env and common_env.isdigit():
        return int(common_env)

    conf = get_config()
    if conf is not None:
        gid = getattr(conf, "GUILD_ID", None) or getattr(conf, "DISCORD_GUILD_ID", None)
        if gid is not None:
            try:
                return int(gid)
            except (TypeError, ValueError):
                pass
    return cfg_int("GUILD_ID", 0)


def cfg_emotion_bio_enabled() -> bool:
    """現在のコンフィグにおいて感情プロフィール（About Me / description）同期が有効かを判定する。

    優先順位:
    1. EMOTION_BIO_TARGET_CONFIGS が指定されている場合、現在のコンフィグが含まれているかを検証。
       含まれていなければ False。
    2. 個別プレフィックス付き環境変数（例: IMG_EMOTION_BIO_ENABLED, TEST_SERVER_EMOTION_BIO_ENABLED）
    3. 個別コンフィグ内の EMOTION_BIO_ENABLED 定義
    4. 共通環境変数 EMOTION_BIO_ENABLED
    5. デフォルト True
    """
    target_configs = os.environ.get("EMOTION_BIO_TARGET_CONFIGS", "").strip()
    if not target_configs:
        conf = get_config()
        if conf is not None:
            target_configs = str(getattr(conf, "EMOTION_BIO_TARGET_CONFIGS", "") or "").strip()
    if target_configs:
        current_name = get_bot_config_name()
        prefix = get_bot_config_prefix().lower()
        allowed: set[str] = set()
        for item in target_configs.split(","):
            c = item.strip()
            if c:
                norm = c.lower()
                allowed.add(norm)
                if norm.startswith("config_"):
                    allowed.add(norm[7:])
                else:
                    allowed.add(f"config_{norm}")
        if current_name.lower() not in allowed and prefix not in allowed:
            return False

    prefix = get_bot_config_prefix()
    if prefix:
        scoped_key = f"{prefix}_EMOTION_BIO_ENABLED"
        if scoped_key in os.environ:
            return os.environ[scoped_key].strip().lower() in {"1", "true", "yes", "on"}

    conf = get_config()
    if conf is not None and hasattr(conf, "EMOTION_BIO_ENABLED"):
        val = getattr(conf, "EMOTION_BIO_ENABLED")
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in {"1", "true", "yes", "on"}

    return cfg_bool("EMOTION_BIO_ENABLED", True)


def resolve_emotion_bio_label() -> str:
    """現在のBotコンフィグ用のプロフィールラベル（例: 'img', 'test', 'AIコイトフクマル'）を取得する。"""
    prefix = get_bot_config_prefix()
    if prefix:
        scoped_key = f"{prefix}_EMOTION_BIO_LABEL"
        if scoped_key in os.environ and os.environ[scoped_key].strip():
            return os.environ[scoped_key].strip()

    if "EMOTION_BIO_LABEL" in os.environ and os.environ["EMOTION_BIO_LABEL"].strip():
        return os.environ["EMOTION_BIO_LABEL"].strip()

    conf = get_config()
    if conf is not None and hasattr(conf, "EMOTION_BIO_LABEL"):
        val = str(getattr(conf, "EMOTION_BIO_LABEL", "") or "").strip()
        if val:
            return val

    # コンフィグ名から短縮名を自動解決（例: config_img -> img, config_test -> test）
    config_name = get_bot_config_name()
    short_name = config_name[7:] if config_name.startswith("config_") else config_name
    return short_name


def cfg_emotion_bio_multi_config_merge_enabled() -> bool:
    """同一Botトークン環境で複数コンフィグの感情プロフィールを1つの自己紹介欄に合体（マージ）するかどうか。

    デフォルトは False。
    False の場合、各Botは自分自身のラベルと感情・理由セクションのみをプロフィールに出力します。
    明示的に複数Botの感情を1つのプロフィール欄に併記したい場合のみ True に設定します。
    """
    prefix = get_bot_config_prefix()
    if prefix:
        scoped_merge = f"{prefix}_EMOTION_BIO_MULTI_CONFIG_MERGE"
        if scoped_merge in os.environ:
            return os.environ[scoped_merge].strip().lower() in {"1", "true", "yes", "on"}

    if "EMOTION_BIO_MULTI_CONFIG_MERGE" in os.environ:
        return os.environ["EMOTION_BIO_MULTI_CONFIG_MERGE"].strip().lower() in {"1", "true", "yes", "on"}

    conf = get_config()
    if conf is not None and hasattr(conf, "EMOTION_BIO_MULTI_CONFIG_MERGE"):
        val = getattr(conf, "EMOTION_BIO_MULTI_CONFIG_MERGE")
        if isinstance(val, bool):
            return val
        if val is not None and str(val).strip():
            return str(val).strip().lower() in {"1", "true", "yes", "on"}

    return False


def cfg_emotion_bio_multi_config_enabled() -> bool:
    """後方互換: cfg_emotion_bio_multi_config_merge_enabled へ委譲。"""
    return cfg_emotion_bio_multi_config_merge_enabled()


def resolve_isolated_path(
    cfg_key: str,
    default_pattern: str,
    legacy_fallback_config: str = "config_img",
    custom_cfg: Any = None,
) -> str:
    """コンフィグごとに自動分離されたファイルパスを解決する。

    明示的に設定値（環境変数または個別コンフィグ内の定義）が存在する場合はそれを優先する。
    未指定の場合は default_pattern（例: '/var/lib/ollama-bot/memory_{config_name}.db'）に
    現在の config_name を埋め込んで自動分離する。

    既存データとの後方互換性：
    legacy_fallback_config と一致し、かつ元の共有パス（例: '/var/lib/ollama-bot/memory.db'）が
    既に実在し、分離先ファイルがまだ存在しない場合は、既存データを壊さないよう元の共有パスを採用する。
    """
    conf = get_config()
    explicit = os.environ.get(cfg_key)
    if explicit:
        explicit_str = explicit.strip()
        if "{config_name}" in explicit_str:
            return explicit_str.format(config_name=get_bot_config_name())
        return explicit_str

    from ollama_bot import config_base
    base_val = getattr(config_base, cfg_key, None)

    cfg_fn = custom_cfg or cfg
    val = cfg_fn(cfg_key, None)
    if val is not None and val != base_val:
        return str(val).strip()

    config_name = get_bot_config_name()
    isolated_path = default_pattern.format(config_name=config_name)

    # 後方互換性チェック: legacy_fallback_config の場合、元のパスが存在すればそれを優先
    if config_name == legacy_fallback_config and base_val:
        legacy_path = str(base_val).strip()
        if os.path.exists(legacy_path) and not os.path.exists(isolated_path):
            return legacy_path

    return isolated_path


def cfg_persona_namespace(custom_cfg: Any = None) -> str:
    """現在のBotのペルソナ名前空間を取得する。

    明示的に MEMORY_PERSONA_NAMESPACE が設定されている場合はそれを優先。
    未設定の場合は get_bot_config_name() から自動生成（プレフィックス 'config_' を除去）し、
    各Botインスタンスごとに完全に独立したペルソナ名前空間を保証する。
    """
    cfg_fn = custom_cfg or cfg
    explicit = cfg_fn("MEMORY_PERSONA_NAMESPACE", None)
    if explicit:
        return str(explicit).strip()
    name = get_bot_config_name()
    if name.startswith("config_"):
        name = name[7:]
    return name or "default"


def cfg(name: str, default: Any = None) -> Any:
    """設定値を取得する。"""
    conf = get_config()
    return getattr(conf, name, default) if conf is not None else default


def __getattr__(name: str) -> Any:
    """モジュール属性（config等）の動的委譲。"""
    if name == "config":
        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def cfg_str(name: str, default: str = "") -> str:
    """文字列設定値を安全に取得しトリムして返す。None時はdefaultを返す。"""
    value = cfg(name, default)
    return str(default if value is None else value).strip()


def cfg_bool(name: str, default: bool = False) -> bool:
    """真偽値設定値を取得する。"""
    value = cfg(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def cfg_int(name: str, default: int = 0) -> int:
    """整数設定値を取得する。変換失敗時はdefaultを返す。"""
    try:
        return int(cfg(name, default))
    except (TypeError, ValueError):
        return default


def cfg_float(name: str, default: float = 0.0) -> float:
    """浮動小数点設定値を取得する。変換失敗時はdefaultを返す。"""
    try:
        return float(cfg(name, default))
    except (TypeError, ValueError):
        return default


def cfg_int_set(name: str) -> set[int]:
    """整数セット設定値を取得する。環境変数等でカンマ区切り文字列が渡された場合も正しく処理する。"""
    raw = cfg(name, []) or []
    # 文字列が渡された場合（環境変数等）はカンマ分割してからパースする
    if isinstance(raw, str):
        raw = [v.strip() for v in raw.split(",") if v.strip()]
    result: set[int] = set()
    for value in raw:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def cfg_command_ignored_user_ids() -> set[int]:
    """コマンド使用を禁止するユーザーIDのセットを取得する。

    COMMAND_IGNORE_USER_IDS または IGNORE_COMMAND_USER_IDS の両方をサポートする。
    """
    return cfg_int_set("COMMAND_IGNORE_USER_IDS") | cfg_int_set("IGNORE_COMMAND_USER_IDS")


def is_user_command_blocked(user_id: int | None) -> bool:
    """指定されたユーザーがコマンド（スラッシュコマンド、プレフィックスコマンド等）の実行を制限されているかを判定する。

    IGNORE_USER_IDS（完全無視）または COMMAND_IGNORE_USER_IDS（コマンド制限）に含まれる場合に True を返す。
    """
    if user_id is None:
        return False
    return user_id in cfg_int_set("IGNORE_USER_IDS") or user_id in cfg_command_ignored_user_ids()


def model_supports_thinking() -> bool:
    """現在のモデルが思考タグ出力をサポートするかを判定する。"""
    model = str(cfg("OLLAMA_MODEL", "") or "").lower()
    non_thinking_markers = (
        "-instruct",
        ":instruct",
        "instruct-",
        "non-thinking",
    )
    return not any(marker in model for marker in non_thinking_markers)


def cfg_primary_channel_id(default: int = 0) -> int:
    """プライマリチャットチャンネルIDを取得する。"""
    cfg_obj = get_config()
    fallback = getattr(cfg_obj, "OLLAMA_CHANNEL_ID", default) if cfg_obj is not None else default
    value = cfg("OLLAMA_CHANNEL_ID", fallback)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def cfg_managed_channel_ids() -> set[int]:
    """Botが管理対象とするすべてのチャンネルIDのセットを取得する。"""
    result = set(cfg_int_set("OTHER_CHANNEL_IDS"))
    primary_id = cfg_primary_channel_id(0)
    if primary_id > 0:
        result.add(primary_id)
    return {int(v) for v in result if int(v) > 0}
