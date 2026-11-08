# ollama_bot/common/user_interest_logic.py
from __future__ import annotations

import logging
import re
from typing import Any

from ollama_bot.common.json_compat import dumps as json_dumps
from harness.agent.memory_manager import MemoryStore
from ollama_bot.common.ollama_helpers import call_ollama_json, truncate_text

log = logging.getLogger("ollama_bot.common.user_interest_logic")

USER_INTEREST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
      "has_interest": {"type": "boolean"},
      "interests": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "topic": {"type": "string"},
            "detail": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "interest_level": {"type": "number"},
          },
          "required": ["topic", "detail", "interest_level"],
          "additionalProperties": False,
        },
      },
    },
    "required": ["has_interest", "interests"],
    "additionalProperties": False,
}

USER_INTEREST_SYSTEM_PROMPT = (
    "あなたは会話からユーザーの興味・関心事（好きな作品、ゲーム、趣味、食べ物、熱中している話題、今度やりたいこと等）を抽出する専門AIです。\n"
    "ユーザーが好意・熱意・関心を示しているトピックがあれば抽出してください。\n"
    "【抽出ルール】\n"
    "1. topic: 対象の名前（例: 'Apex Legends', 'ラーメン二郎', '映画鑑賞', 'Pythonプログラミング'）を簡潔に出力。\n"
    "2. detail: どのように関心を持っているかの補足（例: '新シーズンを夜通しやり込んでいる', '週末に食べに行く予定を話していた'）。\n"
    "3. keywords: 関連するキーワードを1〜3個（例: ['ゲーム', 'FPS']）。\n"
    "4. interest_level: 関心の強さ・熱量（0.0〜1.0）。\n"
    "5. 【除外ルール（過剰抽出の厳禁）】以下のような発言・内容は興味関心として絶対に抽出せず除外してください:\n"
    "   - 一時的な仕様への愚痴・不満・文句（『めんどくせえ』『動かない』等）。\n"
    "   - 単なるトラブルシューティングのやり取り、設定確認。\n"
    "   - 偶発的なニュースへのツッコミは除外すること。\n"
    "   - 単なる挨拶、相槌、一時的な日常報告（'お腹すいた'、'眠い'等で特定の関心事がないもの）、ボットへの操作指示。\n"
    "6. 【判定基準】熱意や恒久的な好意が示されていない発言では、無理に関心事とみなさず必ず has_interest: false, interests: [] を返してください。\n"
    "7. 出力は必ず指定のJSONのみで行ってください。"
)


def _join_context_lines(context_lines: list[str], limit: int = 8) -> str:
    lines = [str(line or "").strip() for line in (context_lines or []) if str(line or "").strip()]
    return "\n".join(lines[-limit:])


async def extract_user_interests_from_context(
    *,
    cfg: Any,
    user_text: str,
    context_lines: list[str] | None = None,
) -> list[dict[str, Any]]:
    text = str(user_text or "").strip()
    if not text or len(text) < 3:
        return []

    # 明らかな短文相槌やメタコマンドはスキップ
    if re.match(r"^(?:はい|いいえ|うん|ああ|おやすみ|おはよう|こんにちは|わかった|ありがとう|草|w+)[!！?？\s]*$", text):
        return []

    context_str = _join_context_lines(context_lines or [], limit=8)
    prompt = (
        f"[直近の会話文脈]\n{context_str or '(なし)'}\n\n"
        f"[最新のユーザー発言]\n{truncate_text(text, 500)}\n\n"
        "上のユーザー発言から、ユーザーが関心を持っているトピック・趣味・好みを抽出してください。\n"
        "※注意: 一時的な仕様への愚痴・不満・文句（『めんどくせえ』『動かない』等）、単なるトラブルシューティングのやり取り、偶発的なニュースへのツッコミは除外すること。熱意や恒久的な好意が示されていない発言では has_interest: false を返してください。"
    )

    model = str(cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_MODEL", "")) or "").strip()
    timeout_sec = float(cfg("USER_INTERESTS_EXTRACT_TIMEOUT_SEC", 60.0) or 60.0)
    min_score = float(cfg("USER_INTERESTS_MIN_SCORE", 0.3) or 0.3)

    try:
        result = await call_ollama_json(
            prompt,
            system_prompt=USER_INTEREST_SYSTEM_PROMPT,
            schema=USER_INTEREST_SCHEMA,
            think=False,
            model=model,
            temperature=0.1,
            timeout_sec=timeout_sec,
            retries=1,
            num_predict=256,
        )
    except Exception as e:
        log.warning("extract_user_interests_from_context failed: %r", e)
        return []

    if not isinstance(result, dict) or not result.get("has_interest"):
        return []

    raw_interests = list(result.get("interests") or [])
    valid_interests: list[dict[str, Any]] = []
    for item in raw_interests:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "").strip()
        if not topic or len(topic) > 80:
            continue
        detail = str(item.get("detail") or "").strip()
        level = float(item.get("interest_level") or 0.5)
        if level < min_score:
            continue
        raw_kw = item.get("keywords")
        keywords = [str(k).strip() for k in raw_kw if str(k).strip()] if isinstance(raw_kw, list) else []
        valid_interests.append({
            "topic": topic,
            "detail": detail,
            "keywords": keywords[:5],
            "interest_level": max(0.0, min(1.0, level)),
        })
    return valid_interests


async def update_user_interests(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    context_lines: list[str] | None = None,
    participant_user_ids: list[int] | None = None,
) -> list[int]:
    if not cfg("USER_INTERESTS_ENABLED", True) or store is None:
        return []
    if user_id is None and not participant_user_ids:
        return []

    interests = await extract_user_interests_from_context(
        cfg=cfg,
        user_text=user_text,
        context_lines=context_lines,
    )
    if not interests:
        return []

    persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip()
    saved_ids: list[int] = []

    target_uids: list[int] = []
    if user_id is not None:
        target_uids.append(user_id)
    if participant_user_ids:
        for p_uid in participant_user_ids:
            if isinstance(p_uid, int) and p_uid not in target_uids:
                target_uids.append(p_uid)

    for uid in target_uids:
        is_primary = (uid == user_id)
        for item in interests:
            try:
                base_level = float(item.get("interest_level", 0.5))
                level = base_level if is_primary else max(0.2, round(base_level * 0.7, 2))
                interest_id = await store.upsert_user_interest(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=uid,
                    topic=item["topic"],
                    detail=item.get("detail"),
                    keywords=item.get("keywords"),
                    interest_level=level,
                )
                saved_ids.append(interest_id)
            except Exception as e:
                log.warning("failed to upsert user interest: %r topic=%r uid=%s", e, item.get("topic"), uid)

    return saved_ids


def format_user_interest_item_for_prompt(item: dict[str, Any]) -> str:
    topic = str(item.get("topic") or "").strip()
    detail = str(item.get("detail") or "").strip()
    user_id = item.get("user_id")
    user_tag = f"<@{user_id}>" if user_id else "メンバー"
    if detail:
        return f"- {user_tag}の関心事: 『{topic}』（補足: {detail}）"
    return f"- {user_tag}の関心事: 『{topic}』"


def format_user_interests_for_prompt(items: list[dict[str, Any]], limit: int = 3) -> str:
    lines = [format_user_interest_item_for_prompt(it) for it in (items or [])[:limit] if str(it.get("topic") or "").strip()]
    return "\n".join(lines)
