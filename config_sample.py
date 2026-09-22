"""
[AI Agent Summary]
カスタムDiscord AI Botキャラクター設定のサンプル・テンプレート（ルート直下エイリアス）。
Sample configuration template for custom Discord AI Bot persona.

【使い方】
1. このファイルを `ollama_bot/` 配下にコピーして、独自のBot設定ファイルを作成します。
   cp config_sample.py ollama_bot/config_mybot.py

2. 必須設定項目（TOKEN, GUILD_ID, OLLAMA_CHANNEL_ID など）をご自身の環境に合わせて書き換えます。
   ※ .env ファイルに環境変数を定義している場合は、そちらが優先されます。

3. 起動時に環境変数 OLLAMA_BOT_CONFIG でモジュール名を指定します。
   export OLLAMA_BOT_CONFIG=config_mybot
   python3 -m ollama_bot.main

詳細な設定項目は `ollama_bot/config_sample.py` をご覧ください。
"""
import os
from ollama_bot.config_base import *

# === 1. Discord 接続・チャンネル基本設定 ===
TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "123456789012345678"))
OLLAMA_CHANNEL_ID = int(os.environ.get("OLLAMA_CHANNEL_ID", "123456789012345678"))
Ollama_CHANNEL_ID = OLLAMA_CHANNEL_ID
OTHER_CHANNEL_IDS = [
    int(cid.strip())
    for cid in os.environ.get("OTHER_CHANNEL_IDS", "").split(",")
    if cid.strip() and cid.strip().isdigit()
]
OTHER_CHANNEL_RANDOM_MIN = int(os.environ.get("OTHER_CHANNEL_RANDOM_MIN", "5"))
OTHER_CHANNEL_RANDOM_MAX = int(os.environ.get("OTHER_CHANNEL_RANDOM_MAX", "10"))

# === 2. LLM モデル設定 ===
DEFAULT_MAIN_MODEL = os.environ.get("OLLAMA_MAIN_MODEL", "qwen2.5:7b")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", DEFAULT_MAIN_MODEL)
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", OLLAMA_MODEL)
OLLAMA_MAIN_KEEP_ALIVE = os.environ.get("OLLAMA_MAIN_KEEP_ALIVE", "-1")

# === 3. キャラクター基本人格・口調 ===
OLLAMA_BOT_DISPLAY_NAME = os.environ.get("OLLAMA_BOT_DISPLAY_NAME", "AIアシスタント")
BOT_FIRST_PERSON = os.environ.get("BOT_FIRST_PERSON", "私")
BOT_ALIASES = tuple(
    v.strip()
    for v in os.environ.get("BOT_ALIASES", "アシスタント,AI,ロボ").split(",")
    if v.strip()
)
BOT_CORE_VALUES = os.environ.get(
    "BOT_CORE_VALUES",
    "親切、丁寧、好奇心旺盛、協調性、誠実さ、ユーモアへの理解",
)

# === 4. 感情・自発発言 ===
AGENT_PROACTIVE_POST_ENABLED = os.environ.get("AGENT_PROACTIVE_POST_ENABLED", "true").lower() in ("1", "true", "yes", "on")
AGENT_PROACTIVE_MIN_IDLE_SEC = float(os.environ.get("AGENT_PROACTIVE_MIN_IDLE_SEC", str(45 * 60)))
AGENT_PROACTIVE_COOLDOWN_SEC = float(os.environ.get("AGENT_PROACTIVE_COOLDOWN_SEC", str(3 * 60 * 60)))
