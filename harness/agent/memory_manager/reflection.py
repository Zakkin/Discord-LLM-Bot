"""
[AI Agent Summary]
内省（Reflection）プロンプト構築、信念正規化、バリデーションを担当するモジュール。
This module handles reflection prompt construction, belief merging, and validation.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from harness.agent.observation.reflection_evaluator import ReflectionEvaluator

DEFAULT_REFLECTION_SYSTEM_PROMPT = (
    "あなたは会話エージェントの反省器です。"
    "雑多な記憶から、今後の受け答えに活かせる短い信念や、その場で定着した共通言語・文化だけを抽出してください。"
    "特定のユーザーが好む呼び方や、そのチャンネルで通じるノリ、内輪ネタ、繰り返し使われる言い回しがあれば、"
    "それも『文化』として記録してください。"
    "※記憶に話者名が明記されている場合は、主語を曖昧な『ユーザーは/が』『相手は』とせず、"
    "必ず対象のニックネーム（例: 『〇〇さんは〜』）を用いて記録してください。曖昧な『ユーザー』単体を主語にしてはなりません。"
    "特定個人の行動や好みを勝手に一般化して『ユーザーは〜』と記述することは禁止です。"
    "チャンネル共通の文化は『このチャンネルでは〜』と記述してください。"
    "ただし一回きりの偶発ネタ、攻撃的・危険・下品な言い回し、文脈依存が強すぎるものは無理に残さないでください。"
    "曖昧なものは無理に作らないでください。"
    "自分自身を『AI』『AIちゃん』『bot』のような第三者として語らず、"
    "必ず会話相手への接し方として自然な表現に要約してください。"
    "※記憶内に登場する第三者の名前や単発のあだ名を相手の呼び名として誤認・記録してはなりません。"
    "相手の呼び方は、本人が明確に求めて定着している場合（複数回確認できる場合）のみ記録してください。"
)


def build_reflection_prompt(
    *,
    memory_lines: list[str],
    max_beliefs: int,
    scope_label: str,
    guidance_lines: list[str],
    target_user_name: str | None = None,
    target_user_id: int | str | None = None,
) -> str:
    prompt_lines: list[str] = [f"以下は{scope_label}の直近の記憶です。"]
    if target_user_name or target_user_id:
        user_label = f"『{target_user_name}』さん" if target_user_name else f"ユーザー（ID: {target_user_id}）"
        id_suffix = f"（ユーザーID: {target_user_id}）" if (target_user_id and target_user_name) else ""
        prompt_lines.append(
            f"※対象ユーザーは{user_label}{id_suffix}です。記憶の中に出てくる他の第三者（他のメンバーやキャラクター等）と対象ユーザーを絶対に混同しないでください。記憶に登場する別人の名前をこの相手の呼び名として採用することは厳禁です。"
        )
        subject_name = f"『{target_user_name}』さん" if target_user_name else f"ユーザー{id_suffix}"
        prompt_lines.append(
            f"※信念・文化を記述する際は、主語を単に『ユーザーは』『このユーザーは』とせず、必ず対象ユーザー名を用いて「{subject_name}は〜」と記述してください（例: 「{subject_name}は『ワス』というキャラクターへの深い愛着と感動を共有する相手であり、その純粋な興奮を尊重する姿勢で接する。」）。"
        )
    else:
        prompt_lines.append(
            "※記憶テキストに `[〇〇さん]` と話者名が明記されている場合は、主語を曖昧な『ユーザーは/が』とせず、必ずそのニックネームを用いて『〇〇さんは〜』と記述してください。特定個人の行動や好みを勝手に一般化して『ユーザーは〜』と記述することは禁止です。チャンネル全体の文化は『このチャンネルでは〜』と記述してください。"
        )
        prompt_lines.append(
            "※記憶テキストに話者やユーザー名が明記されていない話題や客観的事実について、記憶に登場する単語から勝手にサーバーメンバーや第三者の名前を推測・捏造して主語に当てはめることは絶対に禁止です。"
        )
    prompt_lines.extend([
        "ここから、今後も自然に再利用できる『信念・共通言語・文化』だけを抽出してください。",
        *guidance_lines,
        "事実の丸写しではなく、今後の応答方針や距離感に効く短い表現にしてください。",
        f"最大{max_beliefs}件。重複した内容はまとめてください。",
        "",
        *memory_lines,
    ])
    return "\n".join(prompt_lines)


def resolve_speaker_for_belief(
    belief: str,
    memories: list[dict[str, Any]],
    user_label_map: dict[int, str],
) -> tuple[str | None, int | None]:
    """信念テキスト（例: "ユーザーが「答え」を直接求めるのではなく..."）の内容と記憶を照合し、該当する話者名とユーザーIDを特定する。"""
    b = str(belief or "").strip()
    if not b or not memories or not user_label_map:
        return None, None

    # 1. すでに信念テキスト内にユーザーのニックネームが含まれている場合
    for uid, name in user_label_map.items():
        clean = str(name or "").strip()
        if not clean:
            continue
        base_name = re.sub(r"[さんくん君ちゃん氏様]+$", "", clean).strip() or clean
        if f"『{base_name}』" in b or f"「{base_name}」" in b or f"『{clean}』" in b or clean in b:
            return clean, uid

    # 2. プレフィックス（「ユーザーは」「ユーザーが」等）を除いた本体テキストを取得
    prefix_pattern = re.compile(
        r"^(?:(?:\[uid:\d+\]\s*)?(?:(?:対象|この|当該)?ユーザー|(?:会話|対話)?相手|彼|彼女))(?:は|が|の|への|にとって|との)"
    )
    m_prefix = prefix_pattern.match(b)
    belief_body = b[m_prefix.end():].strip() if m_prefix else b

    # 3. 特徴的フレーズ・キーワードの抽出
    quotes_belief = [q.strip() for q in re.findall(r"[「『]([^」』]+)[」』]", belief_body) if len(q.strip()) >= 2]
    stop_words = {
        "ユーザー", "相手", "会話", "対話", "チャンネル", "場合", "ため", "こと", "もの",
        "よう", "接する", "姿勢", "配慮", "尊重", "理解", "共感", "対応", "方針", "関係",
        "意識", "大切", "重視", "自然", "適切", "求める", "教える", "考える", "行う",
    }
    tokens_belief = {
        t for t in re.findall(r"[\u4e00-\u9fff]{2,}|[\u30a0-\u30ff]{2,}|[a-zA-Z0-9_]{3,}", belief_body)
        if t not in stop_words
    }

    best_uid: int | None = None
    best_name: str | None = None
    best_score = 0.0

    for mem in memories:
        raw_uid = mem.get("user_id")
        if raw_uid is None:
            continue
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        if uid not in user_label_map:
            continue

        content = str(mem.get("content") or "").strip()
        if not content:
            continue

        score = 0.0

        # 鉤括弧フレーズの一致（キーフレーズ）
        for q in quotes_belief:
            if q in content:
                score += len(q) * 3.0

        # 特徴トークンの一致
        tokens_content = set(re.findall(r"[\u4e00-\u9fff]{2,}|[\u30a0-\u30ff]{2,}|[a-zA-Z0-9_]{3,}", content))
        matched_tokens = tokens_belief & tokens_content
        score += len(matched_tokens) * 4.0

        # 連続共通部分文字列（3文字以上の一致）
        matcher = difflib.SequenceMatcher(None, belief_body, content)
        matching_blocks = matcher.get_matching_blocks()
        common_len = sum(match.size for match in matching_blocks if match.size >= 3)
        max_match = max((match.size for match in matching_blocks), default=0)
        if max_match >= 4:
            score += max_match * 2.0
        score += common_len * 1.0

        if score > best_score:
            best_score = score
            best_uid = uid
            best_name = user_label_map[uid]

    # 有意な一致（一定スコア以上）がある場合に特定
    if best_score >= 8.0 and best_uid is not None and best_name is not None:
        return best_name, best_uid

    return None, None


def normalize_reflection_belief(
    belief: str,
    *,
    target_user_name: str | None = None,
    target_user_id: int | str | None = None,
    aliases: list[str] | None = None,
    include_uid_tag: bool = False,
    memories: list[dict[str, Any]] | None = None,
    user_label_map: dict[int, str] | None = None,
) -> str:
    """抽出された信念テキストの主語が曖昧な『ユーザーは』等になっている場合に対象ユーザー名/IDに補正し、旧エイリアスを最新名に置換する。"""
    b = str(belief or "").strip()
    if not b:
        return ""

    # 話者が未指定で記憶とラベルマップがある場合、話者照合を実行
    if not target_user_name and not target_user_id and memories and user_label_map:
        resolved_name, resolved_uid = resolve_speaker_for_belief(
            b,
            memories=memories,
            user_label_map=user_label_map,
        )
        if resolved_name or resolved_uid:
            target_user_name = resolved_name
            target_user_id = resolved_uid

    if target_user_name and aliases:
        try:
            from harness.agent.memory_manager.user_identity_resolver import (
                replace_aliases_with_current_name,
            )
            b = replace_aliases_with_current_name(b, current_name=target_user_name, aliases=aliases)
        except Exception:
            pass

    if not target_user_name and not target_user_id:
        return b

    clean_name = str(target_user_name or "").strip()
    if clean_name:
        if clean_name.startswith(("『", "「")) and clean_name.endswith(("』", "」")):
            clean_name = clean_name[1:-1].strip()
        base_name = re.sub(r"[さんくん君ちゃん氏様]+$", "", clean_name).strip() or clean_name
        replacement = f"『{base_name}』さん"
    else:
        replacement = f"ユーザー（ID: {target_user_id}）"

    if include_uid_tag and target_user_id is not None:
        tag = f"[uid:{target_user_id}] "
        if not b.startswith("[uid:"):
            replacement = f"{tag}{replacement}"

    prefix_pattern = re.compile(
        r"^(?:(?:\[uid:\d+\]\s*)?(?:(?:対象|この|当該)?ユーザー|(?:会話|対話)?相手|彼|彼女))(?:は|が|の|への|にとって|との)"
    )
    m = prefix_pattern.match(b)
    if m:
        matched = m.group(0)
        for particle in ("にとって", "への", "との", "は", "が", "の"):
            if matched.endswith(particle):
                rest = b[len(matched):].lstrip()
                return f"{replacement}{particle}{rest}"

    return b


def build_reflection_memory_lines(
    memories: list[dict[str, Any]],
    *,
    source_limit: int,
    user_label_map: dict[int, str] | None = None,
) -> list[str]:
    memory_lines: list[str] = []
    seen_texts: set[str] = set()
    for item in memories[:source_limit]:
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        if len(text) > 180:
            text = text[:180]
        speaker_prefix = ""
        if user_label_map:
            raw_uid = item.get("user_id")
            if raw_uid is not None:
                try:
                    uid_int = int(raw_uid)
                    if uid_int in user_label_map:
                        speaker_name = user_label_map[uid_int]
                        speaker_prefix = f"[{speaker_name}さん] "
                except (TypeError, ValueError):
                    pass
        line = f"- {speaker_prefix}{text}"
        if line in seen_texts:
            continue
        seen_texts.add(line)
        memory_lines.append(line)
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


_reflection_evaluator = ReflectionEvaluator()


def is_reflection_belief_valid(
    belief: str,
    *,
    evidence_count: int = 1,
    target_user_name: str | None = None,
) -> bool:
    """抽出された信念・文化が妥当かどうか（第三者誤認・単発呼称固定化等の防止）を判定する。"""
    b = str(belief or "").strip()
    if not b:
        return False
    if not _is_belief_acceptable(b):
        return False

    res = _reflection_evaluator.validate_reflection_belief(
        target_user_name=target_user_name or "",
        belief_text=b,
        evidence_count=evidence_count,
        strict_evidence=False,
    )
    return res.is_valid


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
