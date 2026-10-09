"""
[AI Agent Summary]
このファイルは、ユーザーとの過去の対話行動履歴（普段の発言、Botの出来事評価・感情）を分析し、
LLMを用いてユーザーに対する本音の一言コメント（所感）を生成・更新するロジックを担当します。
This file handles analyzing past interaction history and generating personal impressions/thoughts using LLM.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ...common.config_helpers import cfg, cfg_float, cfg_int
from ...common.ollama_helpers import call_ollama_json
from harness.agent.prompt_parser.parser import (
    _strip_think_blocks,
    looks_like_copied_log_appraisal,
    looks_like_placeholder_thought,
)
from harness.agent.prompt_parser.prompt_manager.relationship_prompts import (
    build_relationship_thought_prompt,
    format_interaction_logs_for_prompt,
)
from ..ollama_chat_types import UserRelationship
from .relationships_logic import _clamp_relationship_value, _fallback_user_thought

log = logging.getLogger("ollama_bot.ollama_chat.emotion.thought")



def sanitize_relationship_thought(text: str) -> str:
    """生成された一言コメントをサニタイズ・整形する。プレースホルダーや不正テキストは空文字を返す。"""
    if not text:
        return ""

    # 思考タグ除去
    cleaned = _strip_think_blocks(text).strip()

    # 余分な引用符・鉤括弧・中括弧を除去
    cleaned = re.sub(r'^[「"\'【\s]+', "", cleaned)
    cleaned = re.sub(r'[」"\'】\s]+$', "", cleaned)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip()

    # 連続スペース除去
    cleaned = re.sub(r"\s+", " ", cleaned)

    # 最大80文字に制限
    if len(cleaned) > 80:
        cleaned = cleaned[:77] + "..."

    if looks_like_placeholder_thought(cleaned):
        return ""

    return cleaned


_FALLBACK_THOUGHT_SET: set[str] = {
    "強い憎しみと嫌悪感があり、二度と顔も見たくない相手。",
    "腹が立つことも多いけれど、なぜか放っておけない複雑な相手。",
    "何かと棘を感じていて、強い反感を抱いている。",
    "かけがえのない無二の存在で、心から信頼できる大切な相棒！",
    "すごく気が合って頼りになる、大好きな相棒！",
    "とても好印象で、一緒に話していて心から楽しい相手。",
    "いつも気軽に話せて楽しい、親しい仲間。",
    "話すのは楽しいけど、まだ少し警戒してるかも。",
    "好意的で話しやすい人だなと思っている。",
    "真面目でとても信頼できる人。",
    "完全な敵対心と拒絶感があり、全く関わりたくない相手。",
    "強い警戒心があって、あまり不用意には近づきたくない相手。",
    "少し刺々しさを感じていて、距離を置いて接したい。",
    "少し素っ気なく接しがちだけど、悪意はなさそう。",
    "何か裏があるんじゃないかと少し疑いながら見ている。",
    "まだ少し打ち解けられていない感じがする。",
    "まだ知り合って間もない、普通のお知り合い。",
}


def is_fallback_thought(text: str | None) -> bool:
    """指定された所感テキストが初期定型文（フォールバック）またはプレースホルダーかどうかを判定する。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    if cleaned in _FALLBACK_THOUGHT_SET:
        return True
    if looks_like_placeholder_thought(cleaned):
        return True
    return False


_RELATIONSHIP_THOUGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string",
            "description": "相手に向けたキャラクター本人の自然なセリフ（1文）",
        },
    },
    "required": ["thought"],
}


def _resolve_relationship_thought_model(model: str | None = None) -> str:
    """一言所感生成に使用するモデルを解決する。キャラクター会話用モデルを優先。"""
    candidates = [
        model,
        cfg("RELATIONSHIP_THOUGHT_MODEL", ""),
        cfg("OLLAMA_MODEL", ""),
        cfg("DEFAULT_MAIN_MODEL", ""),
        cfg("OLLAMA_MIDDLE_MODEL", ""),
    ]
    for c in candidates:
        val = str(c or "").strip()
        if val:
            return val
    return "img-local-qwen36-a3b-jpn"


async def generate_user_relationship_thought(
    rel: UserRelationship,
    logs: list[dict[str, Any]],
    *,
    bot_name: str = "AI",
    user_name: str = "相手",
    model: str | None = None,
) -> str | None:
    """LLMを呼び出してユーザーに対する一言所感を生成する。失敗時は None を返す。"""
    affinity = _clamp_relationship_value(rel.affinity)
    trust = _clamp_relationship_value(rel.trust)
    affection = _clamp_relationship_value(getattr(rel, "affection", 0.0) or 0.0)
    hatred = _clamp_relationship_value(getattr(rel, "hatred", 0.0) or 0.0)

    log.info(
        "[所感生成開始] ユーザー=%s パラメータ(affinity=%+.2f, trust=%+.2f, affection=%+.2f, hatred=%+.2f) 対話ログ件数=%d",
        user_name, affinity, trust, affection, hatred, len(logs),
    )

    prompt = build_relationship_thought_prompt(
        rel,
        logs,
        bot_name=bot_name,
        user_name=user_name,
    )

    model_name = _resolve_relationship_thought_model(model)
    if not model_name:
        log.warning("[所感生成中止] 使用可能なモデルが解決できませんでした。")
        return None

    timeout_sec = float(cfg("RELATIONSHIP_THOUGHT_TIMEOUT_SEC", 60.0) or 60.0)
    num_predict = max(int(cfg("RELATIONSHIP_THOUGHT_NUM_PREDICT", 256) or 256), 128)
    temperature = float(cfg("RELATIONSHIP_THOUGHT_TEMPERATURE", 0.4) or 0.4)

    try:
        log.info(
            "[所感LLM呼出] モデル=%s num_predict=%d temperature=%.2f prompt_len=%d",
            model_name, num_predict, temperature, len(prompt),
        )
        payload = await call_ollama_json(
            prompt,
            system_prompt=(
                f"あなたは「{bot_name}」本人です。キャラクターの口調と設定を厳格に守り、"
                f"相手に対する本音の気持ちをキャラクターのセリフとして直接出力してください。"
            ),
            model=model_name,
            think=False,
            schema=_RELATIONSHIP_THOUGHT_SCHEMA,
            retries=max(int(cfg("RELATIONSHIP_THOUGHT_RETRIES", 1) or 1), 0),
            timeout_sec=timeout_sec,
            num_predict=num_predict,
            temperature=temperature,
        )
        if isinstance(payload, dict):
            raw_thought = str(payload.get("thought", "") or "").strip()
            log.info("[所感LLM応答] raw=%r", raw_thought)
            cleaned = sanitize_relationship_thought(raw_thought)
            if not cleaned:
                log.warning("[所感棄却] サニタイズ後に空文字またはプレースホルダーとして判定されました: raw=%r", raw_thought)
            elif looks_like_copied_log_appraisal(cleaned, logs):
                log.warning("[所感棄却] 対話ログの受け止めメモ丸写しと判定されました: raw=%r cleaned=%r", raw_thought, cleaned)
            else:
                log.info("[所感採用] ユーザー %s の一言所感を生成・採用しました: %r", user_name, cleaned)
                return cleaned
    except Exception as e:
        log.warning("[所感LLM呼出失敗] %r", e)

    return None


def should_update_user_thought(
    rel: UserRelationship,
    *,
    current_ts: float,
    interval_sec: float | None = None,
    interaction_interval: int | None = None,
) -> bool:
    """一言コメントのLLM更新が必要かどうかを判定する。"""
    # thought が空、または初期定型文、または一度もLLM生成されていない場合は最優先で更新
    current_thought = str(rel.thought or "").strip()
    if not current_thought or is_fallback_thought(current_thought):
        return True

    if float(getattr(rel, "last_thought_generated_ts", 0.0) or 0.0) <= 0.0:
        return True

    cfg_interval = float(
        interval_sec
        if interval_sec is not None
        else cfg_float("RELATIONSHIP_THOUGHT_UPDATE_INTERVAL_SEC", 3600.0)
    )
    cfg_interactions = int(
        interaction_interval
        if interaction_interval is not None
        else cfg_int("RELATIONSHIP_THOUGHT_INTERACTION_INTERVAL", 5)
    )

    elapsed = current_ts - float(rel.last_thought_generated_ts or 0.0)
    interactions = int(getattr(rel, "interaction_count", 0) or 0)

    # 前回の生成から指定時間経過し、かつ1回以上の対話がある場合
    if elapsed >= cfg_interval and interactions >= 1:
        return True

    # 対話回数が指定回数以上蓄積している場合
    if interactions >= cfg_interactions:
        return True

    return False

