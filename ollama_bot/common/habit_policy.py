# ollama_bot/common/habit_policy.py
"""
習慣学習（Habit Policy）モジュール。

文脈キーを生成し、返信スタイルの習慣値を管理する。
習慣は熟考結果を少しだけ押す補助であり、決定権は持たない。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .memory_store import MemoryStore

log = logging.getLogger("ollama_bot.common.habit_policy")

# --------------------------------------------------------------------------- #
# デフォルトスタイル（config で上書き可能なフォールバック）
# --------------------------------------------------------------------------- #

_DEFAULT_STYLES = "direct_reply,short_reaction,give_steps_first,validate_first"
_DEFAULT_STYLE = "direct_reply"
_DEFAULT_WEIGHT = 0.15
_EMA_ALPHA = 0.15   # 移動平均係数（最近の報酬を 15% 反映）
_DECAY_RATE = 0.995  # 1日あたりの忘却率


from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Config ヘルパー
# --------------------------------------------------------------------------- #

def get_allowed_styles(cfg: Any) -> list[str]:
    """設定から使用可能な action_style リストを取得する。"""
    raw = str(cfg("HABIT_ACTION_STYLES", _DEFAULT_STYLES) or _DEFAULT_STYLES)
    return [s.strip() for s in raw.split(",") if s.strip()] or [_DEFAULT_STYLE]


def get_default_style(cfg: Any) -> str:
    """設定からデフォルトの action_style を取得する。"""
    default = str(cfg("HABIT_DEFAULT_STYLE", _DEFAULT_STYLE) or _DEFAULT_STYLE).strip()
    allowed = get_allowed_styles(cfg)
    return default if default in allowed else (allowed[0] if allowed else _DEFAULT_STYLE)


def get_habit_weight(cfg: Any) -> float:
    """設定から習慣の影響係数を取得する（0〜1）。"""
    try:
        return max(0.0, min(1.0, float(cfg("HABIT_STYLE_WEIGHT", _DEFAULT_WEIGHT) or _DEFAULT_WEIGHT)))
    except (TypeError, ValueError):
        return _DEFAULT_WEIGHT


def action_style_bonus_from_user_profile(
    profile: dict[str, Any] | None,
    *,
    allowed_styles: list[str],
) -> dict[str, float]:
    """
    ユーザー別の習慣プロファイルを action_style の弱いボーナスに変換する。
    0.5 を中立として、はっきりした傾向だけを少し押す。
    """
    if not profile or not allowed_styles:
        return {}

    def _score(key: str) -> float:
        try:
            return max(0.0, min(1.0, float(profile.get(key, 0.5) or 0.5)))
        except (TypeError, ValueError):
            return 0.5

    try:
        if int(profile.get("evidence_count", 0) or 0) <= 0:
            return {}
    except (TypeError, ValueError):
        return {}

    bonus: dict[str, float] = {}

    def _add(style: str, value: float) -> None:
        if style in allowed_styles and value > 0.0:
            bonus[style] = max(bonus.get(style, 0.0), min(value, 1.0))

    tone_casual = _score("tone_casual")
    response_detail = _score("response_detail")
    joke_receptivity = _score("joke_receptivity")

    if response_detail >= 0.62:
        _add("give_steps_first", (response_detail - 0.5) * 1.2)
    elif response_detail <= 0.38:
        _add("short_reaction", (0.5 - response_detail) * 1.2)

    if joke_receptivity >= 0.62:
        _add("tease_then_answer", (joke_receptivity - 0.5) * 1.2)
    elif joke_receptivity <= 0.38:
        _add("validate_first", (0.5 - joke_receptivity) * 0.8)

    if tone_casual >= 0.62:
        _add("direct_reply", (tone_casual - 0.5) * 0.6)
    elif tone_casual <= 0.38:
        _add("validate_first", (0.5 - tone_casual) * 0.6)

    return bonus


def _pick_first_allowed(allowed: list[str], *candidates: str, default: str) -> str:
    for candidate in candidates:
        if candidate in allowed:
            return candidate
    return default


# --------------------------------------------------------------------------- #
# 文脈キー生成
# --------------------------------------------------------------------------- #

def _affect_bucket(emotion_state: Any | None) -> str:
    """感情状態を粗いバケットラベルに変換する。"""
    if emotion_state is None:
        return "neutral"
    try:
        anger = float(getattr(emotion_state, "anger", 0.0) or 0.0)
        sadness = float(getattr(emotion_state, "sadness", 0.0) or 0.0)
        joy = float(getattr(emotion_state, "joy", 0.0) or 0.0)
        if anger >= 0.6:
            return "neg_high"
        if sadness >= 0.5:
            return "neg_mid"
        if joy >= 0.6:
            return "pos_high"
        dominant = str(getattr(emotion_state, "dominant_emotion", "") or "").lower()
        if dominant in ("anger", "fear", "disgust"):
            return "neg_mid"
        if dominant in ("joy", "anticipation"):
            return "pos_mid"
    except Exception:
        pass
    return "neutral"


def _social_bucket(relationship_score: float | None) -> str:
    """関係性スコアを粗いバケットラベルに変換する。"""
    if relationship_score is None:
        return "unknown"
    try:
        score = float(relationship_score)
        if score >= 0.7:
            return "close"
        if score >= 0.3:
            return "mid"
        return "distant"
    except (TypeError, ValueError):
        return "unknown"


def _topic_bucket(topic: str) -> str:
    """トピックラベルを短縮して正規化する。"""
    if not topic:
        return "general"
    # 長すぎる場合は先頭 20 文字に切り詰め
    return topic[:20].strip() or "general"


def build_context_key(
    *,
    user_id: int | None,
    channel_id: int | None,
    topic: str = "",
    relationship_score: float | None = None,
    emotion_state: Any | None = None,
) -> str:
    """
    習慣学習用の文脈キーを生成する。

    形式: "u:{uid}|c:{cid}|t:{topic}|s:{social}|a:{affect}"
    """
    uid = str(user_id) if user_id is not None else "anon"
    cid = str(channel_id) if channel_id is not None else "dm"
    t = _topic_bucket(topic)
    s = _social_bucket(relationship_score)
    a = _affect_bucket(emotion_state)
    return f"u:{uid}|c:{cid}|t:{t}|s:{s}|a:{a}"


# --------------------------------------------------------------------------- #
# intent_info → action_style の変換
# --------------------------------------------------------------------------- #

def extract_action_style_from_intent(
    intent_info: dict[str, Any],
    deliberation_info: dict[str, Any],
    *,
    cfg,
) -> str:
    """
    既存の intent_info / deliberation_info から返信スタイルを決定する。
    LLM 追加呼び出しなし。許可されたスタイルの中から最適を選ぶ。
    """
    allowed = get_allowed_styles(cfg)
    default = get_default_style(cfg)

    # 会話破綻や前提の食い違い・訂正がある場合（共感の押し付けを避け、やわらかい確認や直接返答を優先）
    if deliberation_info.get("is_breakdown"):
        return _pick_first_allowed(allowed, "clarify_gently", "direct_reply", default=default)

    # 明確化が必要なケース
    if deliberation_info.get("needs_clarification") or intent_info.get("should_clarify"):
        return _pick_first_allowed(allowed, "clarify_gently", "validate_first", default=default)

    # 感情的フォーカスがある（共感優先）
    emotion_focus = str(deliberation_info.get("emotion_focus", "") or "")
    if emotion_focus and emotion_focus not in ("なし", "none", ""):
        return _pick_first_allowed(allowed, "validate_first", "short_reaction", default=default)

    # 直接依頼・手順系の質問
    intent = str(intent_info.get("intent", "") or "").lower()
    strategy = str(deliberation_info.get("reply_strategy", "") or "").strip().lower()
    if intent in ("request", "question") or strategy.startswith("step") or "手順" in strategy or "結論" in strategy:
        return _pick_first_allowed(allowed, "give_steps_first", "direct_reply", default=default)

    # 短い雑談
    if intent in ("greeting", "monologue"):
        return _pick_first_allowed(allowed, "short_reaction", "validate_first", default=default)

    # 挑発的ツッコミスタイル（tease_then_answer設定時）
    if "tease_then_answer" in allowed and intent in ("statement", ""):
        if not strategy or "軽" in strategy or "雑談" in strategy:
            return "tease_then_answer"

    return default


def build_action_style_instruction(action_style: str) -> str:
    instructions = {
        "tease_then_answer": "最初に軽いツッコミを一言だけ入れてから、本題に短く答えてください。煽りすぎは禁止です。",
        "give_steps_first": "結論か手順を先に短く示し、そのあと必要なら一言だけ補足してください。",
        "short_reaction": "短い反応や相槌を優先し、無理に説明を増やしすぎないでください。",
        "direct_reply": "普段どおり、回りくどくせず自然に直接答えてください。",
        "validate_first": "相手の言葉をそのまま復唱（オウム返し）せず、相手の気持ちや状況を自分の言葉で短く受け止めてから答えてください。",
        "clarify_gently": "断定せず、やわらかく確認してから返答を組み立ててください。",
    }
    return str(instructions.get(str(action_style or "").strip(), instructions["direct_reply"]))


# --------------------------------------------------------------------------- #
# DB アクセス（habit_records テーブル）
# --------------------------------------------------------------------------- #

async def get_habit_bonus(
    store: MemoryStore,
    *,
    persona: str,
    context_key: str,
) -> dict[str, float]:
    """
    context_key に一致する全 action_style の value マップを返す。
    レコードがなければ {} を返す。
    """
    await store._ensure_initialized()
    conn = await store._connect()
    try:
        cur = await conn.cursor()
        await cur.execute(
            """
            SELECT action_style, value
            FROM habit_records
            WHERE persona = ? AND context_key = ?
            """,
            (persona, context_key),
        )
        rows = await cur.fetchall()
        return {str(row["action_style"]): float(row["value"]) for row in rows}
    except Exception:
        log.exception("get_habit_bonus failed persona=%r context_key=%r", persona, context_key)
        return {}
    finally:
        await conn.close()


async def update_habit(
    store: MemoryStore,
    *,
    persona: str,
    context_key: str,
    action_style: str,
    reward: float,
) -> None:
    """
    習慣値を EMA で更新する。
    new_value = old_value * (1 - alpha) + reward * alpha
    """
    reward = max(0.0, min(1.0, float(reward)))
    now = datetime.now(timezone.utc).isoformat()

    await store._ensure_initialized()
    conn = await store._connect()
    try:
        cur = await conn.cursor()
        await cur.execute(
            """
            SELECT id, value, count FROM habit_records
            WHERE persona = ? AND context_key = ? AND action_style = ?
            LIMIT 1
            """,
            (persona, context_key, action_style),
        )
        row = await cur.fetchone()

        if row:
            old_value = float(row["value"])
            new_value = old_value * (1 - _EMA_ALPHA) + reward * _EMA_ALPHA
            new_count = int(row["count"]) + 1
            await cur.execute(
                """
                UPDATE habit_records
                SET value = ?, count = ?, last_reward = ?, last_used_at = ?
                WHERE id = ?
                """,
                (new_value, new_count, reward, now, int(row["id"])),
            )
        else:
            initial_value = 0.5 * (1 - _EMA_ALPHA) + reward * _EMA_ALPHA
            await cur.execute(
                """
                INSERT INTO habit_records
                    (persona, context_key, action_style, count, value, last_reward, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (persona, context_key, action_style, 1, initial_value, reward, now),
            )
        await conn.commit()
        log.debug(
            "update_habit persona=%r context_key=%r style=%r reward=%.3f",
            persona, context_key, action_style, reward,
        )
    except Exception:
        log.exception("update_habit failed persona=%r context_key=%r style=%r", persona, context_key, action_style)
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# リランク（習慣ボーナスで候補を微調整）
# --------------------------------------------------------------------------- #

def rerank_with_habit(
    *,
    style: str,
    habit_bonus: dict[str, float],
    allowed_styles: list[str],
    habit_weight: float = _DEFAULT_WEIGHT,
) -> str:
    """
    熟考層が選んだスタイルを習慣ボーナスで微調整する。
    habit_weight が小さいほど習慣の影響が弱い。
    """
    if not allowed_styles:
        return style
    if not habit_bonus or habit_weight <= 0.0:
        return style if style in allowed_styles else allowed_styles[0]

    # スタイル初期スコア（熟考層の選択が base 1.0）
    candidates = {s: (1.0 if s == style else 0.7) for s in allowed_styles}
    for s, bonus in habit_bonus.items():
        if s in candidates:
            candidates[s] += habit_weight * bonus

    best = max(candidates, key=lambda k: candidates[k])
    return best


# --------------------------------------------------------------------------- #
# 報酬計算
# --------------------------------------------------------------------------- #

def compute_turn_reward(
    *,
    user_replied_quickly: bool = False,
    conversation_continued: bool = True,
    no_negative_follow: bool = True,
    user_valence_improved: bool | None = None,
    got_reaction: bool = False,
) -> float:
    """
    0〜1 の範囲で報酬スコアを計算する。
    各シグナルの重みは小さく抑え、安定した更新を優先する。
    """
    reward = 0.10
    reward += 0.25 * float(user_replied_quickly)
    reward += 0.20 * float(no_negative_follow)
    reward += 0.20 * float(conversation_continued)
    if user_valence_improved is not None:
        reward += 0.15 * float(user_valence_improved)
    else:
        reward += 0.08
    reward += 0.10 * float(got_reaction)
    return max(0.0, min(1.0, reward))


# --------------------------------------------------------------------------- #
# 忘却（日次バックグラウンド）
# --------------------------------------------------------------------------- #

async def decay_old_habits(store: MemoryStore, *, persona: str) -> int:
    """
    最後に使用した日数に応じて習慣値を自然減衰させる。
    value *= DECAY_RATE ** days_since(last_used_at)
    戻り値: 更新したレコード数
    """
    await store._ensure_initialized()
    conn = await store._connect()
    updated = 0
    try:
        cur = await conn.cursor()
        await cur.execute(
            "SELECT id, value, last_used_at FROM habit_records WHERE persona = ?",
            (persona,),
        )
        rows = await cur.fetchall()
        now = datetime.now(timezone.utc)

        for row in rows:
            try:
                last_used = datetime.fromisoformat(
                    str(row["last_used_at"]).replace("Z", "+00:00")
                )
                if last_used.tzinfo is None:
                    last_used = last_used.replace(tzinfo=timezone.utc)
                days = max(0.0, (now - last_used).total_seconds() / 86400)
                new_value = float(row["value"]) * (_DECAY_RATE ** days)
                if abs(new_value - float(row["value"])) > 1e-6:
                    await cur.execute(
                        "UPDATE habit_records SET value = ? WHERE id = ?",
                        (new_value, int(row["id"])),
                    )
                    updated += 1
            except Exception:
                log.exception("decay single row failed id=%r", row["id"])

        if updated > 0:
            await conn.commit()
        log.info("decay_old_habits persona=%r updated=%d", persona, updated)
        return updated
    except Exception:
        log.exception("decay_old_habits failed persona=%r", persona)
        return 0
    finally:
        await conn.close()


__all__ = [
    "get_allowed_styles",
    "get_default_style",
    "get_habit_weight",
    "action_style_bonus_from_user_profile",
    "build_context_key",
    "extract_action_style_from_intent",
    "build_action_style_instruction",
    "get_habit_bonus",
    "update_habit",
    "rerank_with_habit",
    "compute_turn_reward",
    "decay_old_habits",
]
