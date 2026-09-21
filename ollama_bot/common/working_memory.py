# ollama_bot/common/working_memory.py
"""
作業記憶（Working Memory）の毎ターン再構成モジュール。

LLM を使わず直近の会話バッファから重要情報を抽出して working_context を生成する。
保存対象ではなく「今このターンで返答するために必要な情報」のみを保持する。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..ollama_chat_.ollama_chat_types import EmotionState


_SPECIFIC_RECALL_MARKERS = (
    "さっき",
    "続き",
    "つづき",
    "前の",
    "前回",
    "この前",
    "あの件",
    "その件",
    "例の",
    "覚えてる",
    "覚えてます",
    "前に話した",
    "前言ってた",
)


def _extract_topic_keywords(texts: list[str], max_words: int = 5) -> str:
    """直近テキストから名詞・カタカナ語・キーワードを簡易抽出してトピックラベルを作る。"""
    combined = " ".join(texts[-3:]) if texts else ""
    if not combined:
        return "general"

    # カタカナ連続（固有名詞）
    katakana = re.findall(r"[ァ-ヶヴー]{3,}", combined)
    # 漢字+ひらがな複合（動作や状態）
    kanji_phrases = re.findall(r"[\u4e00-\u9fff]{2,}(?:[\u3041-\u309f]{1,3})?", combined)

    candidates = katakana[:2] + kanji_phrases[:3]
    label = "・".join(dict.fromkeys(candidates))[:40]  # 重複除去
    return label or "general"


def _detect_bot_question(bot_text: str) -> bool:
    """bot が質問（問いかけ）で終わっているか判定する。感嘆リアクション（！？等）は除外。"""
    if not bot_text:
        return False
    stripped = bot_text.strip()
    # 絵文字等の末尾記号を除去して末尾30文字を検査
    cleaned = re.sub(r"[\s\U00010000-\U0010ffff\u2600-\u27bf\U0001f300-\U0001f9ff]+$", "", stripped)
    if not cleaned:
        return False
    # 感嘆符付き疑問（！？、?!）や驚きリアクション語尾は感嘆表現として除外
    if re.search(r"[!！][?？]|[?？][!！]|…[?？!！]|(?:ちゃう|ちゃうん|なん|です|ます)か[!！?？…]+$", cleaned):
        return False
    return bool(re.search(r"[?？]$|か[？?]?$|どう[?？]|ない[?？]|あった[?？]|教えて[?？]?|どれ[?？]|どこ[?？]|いつ[?？]|誰[?？]|なに[?？]|何[?？]", cleaned[-30:]))


def _detect_open_loops(bot_text: str, user_texts_recent: list[str]) -> list[str]:
    """bot の発言から未解決ループ（未回答問いかけ・約束）を検出する。"""
    loops: list[str] = []
    if not bot_text:
        return loops

    # bot が質問して、その後ユーザーが答えていない可能性
    if _detect_bot_question(bot_text):
        last_user = (user_texts_recent[-1] if user_texts_recent else "").strip()
        # ユーザー側からの逆質問・確認（?？を含む）なら、未回答ではなくユーザーからの問いかけ
        is_user_asking = bool(re.search(r"[?？]", last_user))
        # 単なる相槌・スタンプ・感嘆符のみの場合だけ「まだ答えていない」とみなす
        is_mere_acknowledgement = bool(
            re.fullmatch(r"[😊😂🤔💦w笑ｗ！!?？\s]+", last_user, re.UNICODE)
            or last_user in {"うん", "はい", "へえ", "へー", "ふーん", "そう", "あ", "お", "わあ", "おー"}
        )
        if not is_user_asking and is_mere_acknowledgement:
            loops.append(f"未回答の問い: {bot_text.strip()[-60:]}")

    # 約束系の表現を検出
    commitment_patterns = (
        r"(あとで|後で|また|今度|次に).*(話す|教える|聞く|確認する|調べる)",
        r"(してみる|やってみる|考えておく|調べておく|あとで送る|あとで出す)",
    )
    for pat in commitment_patterns:
        if re.search(pat, bot_text):
            loops.append(f"bot の約束: {bot_text.strip()[-60:]}")
            break

    return loops


def _emotion_state_to_dict(emotion_state: EmotionState | None) -> dict[str, Any]:
    """EmotionState → シンプルな辞書へ変換。"""
    if emotion_state is None:
        return {}
    result: dict[str, Any] = {}
    for key in ("joy", "anticipation", "anger", "disgust", "sadness", "surprise", "fear"):
        val = getattr(emotion_state, key, None)
        if val is not None:
            try:
                result[key] = round(float(val), 3)
            except (TypeError, ValueError):
                pass
    return result


def _extract_user_texts_from_turns(recent_turns: list[dict[str, Any]]) -> list[str]:
    """recent_turns から user のテキストだけを抽出する。"""
    user_texts: list[str] = []
    for turn in recent_turns:
        role = str(turn.get("role", "")).lower()
        if role in ("user", "human"):
            text = str(turn.get("text", "") or turn.get("content", "") or "").strip()
            if text:
                user_texts.append(text)
    return user_texts


def build_working_context(
    recent_turns: list[dict[str, Any]],
    *,
    channel_summary: str | None = None,
    emotion_state: EmotionState | None = None,
    last_bot_text: str = "",
    future_prompts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    毎ターン再構成する作業記憶を返す。

    Parameters
    ----------
    recent_turns : 直近の会話ターンリスト（各要素は {"role": "user"|"assistant", "text": str}）
    channel_summary : channel_summaries から取得した要約テキスト
    emotion_state : 現在の感情状態
    last_bot_text : 直前の bot 発言テキスト
    future_prompts : 将来記憶のマッチ結果（Phase 2 で使用）

    Returns
    -------
    dict:
        topic: 推定トピックラベル
        open_loops: 未解決の問いや約束
        latest_user_state: 感情スコア辞書（ユーザー由来）
        latest_bot_commitments: bot が約束した事項
        recent_turns: recent_turns そのまま
        channel_summary: channel_summary そのまま
        future_prompts: 将来記憶マッチ（Phase 2）
        _bot_asked_question: bot が最後に質問したかどうか
    """
    user_texts = _extract_user_texts_from_turns(recent_turns)
    all_texts = [str(t.get("text", "") or t.get("content", "") or "") for t in recent_turns]

    topic = _extract_topic_keywords(all_texts)
    open_loops = _detect_open_loops(last_bot_text, user_texts)
    bot_asked_question = _detect_bot_question(last_bot_text)

    # bot の約束を open_loops から分離してリスト化
    bot_commitments = [loop[len("bot の約束: "):] for loop in open_loops if loop.startswith("bot の約束: ")]
    open_loops_filtered = [loop for loop in open_loops if not loop.startswith("bot の約束: ")]

    return {
        "topic": topic,
        "open_loops": open_loops_filtered,
        "latest_user_state": _emotion_state_to_dict(emotion_state),
        "latest_bot_commitments": bot_commitments,
        "recent_turns": recent_turns,
        "channel_summary": channel_summary,
        "future_prompts": future_prompts or [],
        "_bot_asked_question": bot_asked_question,
        "_built_at": datetime.now(timezone.utc).isoformat(),
    }


def format_working_context_for_prompt(ctx: dict[str, Any]) -> list[str]:
    """
    working_context をプロンプト注入用のテキスト行リストに変換する。
    空・無意味な項目は省略する。
    """
    parts: list[str] = []

    open_loops = ctx.get("open_loops") or []
    if open_loops:
        parts.append("【未解決の問い・話題】")
        for loop in open_loops[:3]:
            parts.append(f"- {loop}")

    commitments = ctx.get("latest_bot_commitments") or []
    if commitments:
        parts.append("【あなたが言ったこと/約束】")
        for c in commitments[:2]:
            parts.append(f"- {c}")

    future_prompts = ctx.get("future_prompts") or []
    if future_prompts:
        parts.append("【将来記憶（優先対応）】")
        for fp in future_prompts[:2]:
            intent = str(fp.get("intent", "")).strip()
            if intent:
                parts.append(f"- {intent}")

    return parts


def needs_specific_recall(user_text: str, working_ctx: dict[str, Any] | None = None) -> bool:
    text = str(user_text or "").strip()
    if not text:
        return False

    if any(marker in text for marker in _SPECIFIC_RECALL_MARKERS):
        return True

    topic = str((working_ctx or {}).get("topic", "") or "")
    if topic and len(text) <= 18 and any(token in text for token in ("それ", "あれ", "その話", "あの話")):
        return True

    future_prompts = (working_ctx or {}).get("future_prompts") or []
    if future_prompts and any(token in text for token in ("その件", "続き", "どうなった", "結果")):
        return True

    return False


__all__ = [
    "build_working_context",
    "format_working_context_for_prompt",
    "needs_specific_recall",
]
