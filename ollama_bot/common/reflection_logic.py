from __future__ import annotations

from typing import Any


DEFAULT_REFLECTION_SYSTEM_PROMPT = (
    "あなたは会話エージェントの反省器です。"
    "雑多な記憶から、今後の受け答えに活かせる短い信念や、その場で定着した共通言語・文化だけを抽出してください。"
    "特定のユーザーが好む呼び方や、そのチャンネルで通じるノリ、内輪ネタ、繰り返し使われる言い回しがあれば、"
    "それも『文化』として記録してください。"
    "ただし一回きりの偶発ネタ、攻撃的・危険・下品な言い回し、文脈依存が強すぎるものは無理に残さないでください。"
    "曖昧なものは無理に作らないでください。"
    "自分自身を『AI』『AIちゃん』『bot』のような第三者として語らず、"
    "必ず会話相手への接し方として自然な表現に要約してください。"
)


def build_reflection_prompt(
    *,
    memory_lines: list[str],
    max_beliefs: int,
    scope_label: str,
    guidance_lines: list[str],
) -> str:
    return "\n".join([
        f"以下は{scope_label}の直近の記憶です。",
        "ここから、今後も自然に再利用できる『信念・共通言語・文化』だけを抽出してください。",
        *guidance_lines,
        "事実の丸写しではなく、今後の応答方針や距離感に効く短い表現にしてください。",
        f"最大{max_beliefs}件。重複した内容はまとめてください。",
        "",
        *memory_lines,
    ])


def build_reflection_memory_lines(
    memories: list[dict[str, Any]],
    *,
    source_limit: int,
) -> list[str]:
    memory_lines: list[str] = []
    seen_texts: set[str] = set()
    for item in memories[:source_limit]:
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        if len(text) > 180:
            text = text[:180]
        if text in seen_texts:
            continue
        seen_texts.add(text)
        memory_lines.append(f"- {text}")
    return memory_lines


_DEFAULT_INCOMPATIBLE_BELIEF_KEYWORDS: tuple[str, ...] = ()


def _is_belief_acceptable(belief: str) -> bool:
    b = str(belief or "").strip()
    if not b:
        return False
    try:
        from lib.config_utils import cfg
        incompatible = cfg("INCOMPATIBLE_BELIEF_KEYWORDS", ())
        if incompatible:
            if isinstance(incompatible, (list, tuple, set)):
                if any(k in b for k in incompatible if k):
                    return False
            elif isinstance(incompatible, str) and incompatible.strip():
                for k in incompatible.split(","):
                    if k.strip() and k.strip() in b:
                        return False
    except Exception:
        pass
    return True


def merge_reflection_rows(
    rows_by_scope: list[list[dict[str, Any]]],
    *,
    limit: int,
) -> tuple[list[str], list[int]]:
    beliefs: list[str] = []
    touched_ids: list[int] = []
    seen_beliefs: set[str] = set()

    for rows in rows_by_scope:
        for row in rows:
            belief = str(row.get("belief") or "").strip()
            if not belief or belief in seen_beliefs or not _is_belief_acceptable(belief):
                continue
            seen_beliefs.add(belief)
            beliefs.append(belief)

            rid = row.get("id")
            if rid is not None:
                try:
                    touched_ids.append(int(rid))
                except (TypeError, ValueError):
                    pass

            if len(beliefs) >= limit:
                return beliefs[:limit], touched_ids

    return beliefs[:limit], touched_ids

DEEP_REFLECTION_SYSTEM_PROMPT = (
    "あなたは会話エージェントの深層反省（方針修正）エージェントです。\n"
    "ユーザーとの直近のやり取りや強い感情の揺れに対して、なぜそれが起きたかを分析し、今後の対応方針（policy）を導き出してください。\n"
    "自分自身を『AI』『AIちゃん』『bot』のような第三者目線で語らず、必ず自分事として自然な主語（設定された一人称に準ずる）で自己反省してください。\n"
    "出力は必ず指定されたJSONフォーマットのみにしてください。\n"
)

def build_deep_reflection_prompt(
    *,
    memory_lines: list[str],
    emotion_reason_lines: list[str],
) -> str:
    return "\n".join([
        "以下は直近の会話と、あなた自身に起きた感情の動きの記録です。",
        "ここから、「1.何が起きたか」「2.どう解釈したか」「3.会話相手との関係性がどう変わったか（またはどう認識したか）」「4.次から何に気をつけるか（対応方針）」を抽出・作成してください。",
        "作成した「4.次から何に気をつけるか」は、今後あなた自身が振る舞う際の『中核となる信念（policy）』として保存されます。実践的で端的な表現にしてください。",
        "",
        "出力は以下のキーを持つフラットなJSONにしてください。",
        "{",
        '  "what_happened": "...",',
        '  "interpretation": "...",',
        '  "relationship_change": "...",',
        '  "future_policy": "..."',
        "}",
        "",
        "【直近の会話記録】",
        *memory_lines,
        "",
        "【直近の感情の動き（主観）】",
        *emotion_reason_lines,
    ])
