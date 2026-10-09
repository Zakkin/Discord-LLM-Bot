"""
relationship_prompts.py

ユーザーとの過去の対話行動履歴や関係性パラメータ（親密度・信頼度・好意・反感）に基づき、
LLM向けの関係性プロンプトや対話ログをフォーマットするプロンプトビルダーを提供します。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from lib.config_utils import cfg

log = logging.getLogger("harness.agent.prompt_parser.prompt_manager.relationship_prompts")


def _clamp_relationship_val(value: float, max_val: float = 10.0) -> float:
    """関係値パラメータを指定範囲内に制限する。"""
    from lib.config_utils import cfg
    limit = max(float(cfg("RELATIONSHIP_ABS_MAX", max_val) or max_val), 0.5)
    try:
        numeric = float(value)
    except Exception:
        numeric = 0.0
    return max(-limit, min(limit, numeric))


def format_interaction_logs_for_prompt(logs: list[dict[str, Any]], max_items: int = 8) -> str:
    """プロンプト向けに対話履歴とBotの受け止めメモをフォーマットする。"""
    if not logs:
        return ""

    lines: list[str] = []
    # 新しい順に最大 max_items 件取得し、時系列（古い順）に並べ替える
    target_logs = list(reversed(logs[:max_items]))

    for item in target_logs:
        user_text = str(item.get("user_text", "") or "").strip().replace("\n", " ")
        if not user_text:
            continue
        if len(user_text) > 80:
            user_text = user_text[:77] + "..."

        appraisal = str(item.get("bot_appraisal", "") or "").strip().replace("\n", " ")
        # appraisal から機械的な感情タグ（例: （感情: joy））を除去
        appraisal = re.sub(r"[（(]感情:\s*[a-zA-Z_]+[）)]", "", appraisal).strip()
        if len(appraisal) > 80:
            appraisal = appraisal[:77] + "..."

        entry = f"- 相手の発言: 「{user_text}」"
        if appraisal:
            # プレースホルダーの簡易検知
            lower_ap = appraisal.lower()
            if not any(ph in lower_ap for ph in ("thought", "所感", "一言コメント", "placeholder")):
                entry += f"\n  その時感じたこと: {appraisal}"
        lines.append(entry)

    return "\n".join(lines)


def build_relationship_thought_prompt(
    rel: Any,
    logs: list[dict[str, Any]],
    *,
    bot_name: str = "AI",
    user_name: str = "相手",
) -> str:
    """一言コメント生成用のLLMプロンプトを構築する。"""
    affinity = _clamp_relationship_val(getattr(rel, "affinity", 0.0))
    trust = _clamp_relationship_val(getattr(rel, "trust", 0.0))
    affection = _clamp_relationship_val(getattr(rel, "affection", 0.0) or 0.0)
    hatred = _clamp_relationship_val(getattr(rel, "hatred", 0.0) or 0.0)
    logs_text = format_interaction_logs_for_prompt(logs)

    # パラメータから感じている距離感の自然言語サマリー
    distance_notes: list[str] = []
    if hatred >= 3.0:
        distance_notes.append("強い反感や警戒を感じている")
    elif trust <= -2.0 or affinity <= -2.0:
        distance_notes.append("まだ何を考えているか掴めず、少し身構えたり警戒している")
    elif affection >= 3.0 or (affinity >= 3.0 and trust >= 2.0):
        distance_notes.append("とても親しみを感じており、心を開いて頼りにしている")
    elif affinity >= 1.0 or trust >= 1.0:
        distance_notes.append("好意的で話しやすく、もっと仲良くなりたいと思っている")
    else:
        distance_notes.append("まだ知り合って日が浅く、少し緊張しながら接している")

    distance_summary = "、".join(distance_notes)

    from lib.config_utils import cfg
    first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
    char_instruction = str(cfg("RELATIONSHIP_THOUGHT_SYSTEM_INSTRUCTION", "") or "").strip()
    if not char_instruction:
        char_instruction = (
            f"一人称は『{first_person or '私'}』です。相手と一生懸命お話ししようとするキャラクターです。\n"
            "口調の特徴: 話し言葉で、自然な範囲の会話表現を使います。"
        )
    first_person_desc = f"（{first_person}）" if first_person else ""

    raw_examples = cfg("RELATIONSHIP_THOUGHT_GOOD_EXAMPLES", ())
    if isinstance(raw_examples, (list, tuple)):
        good_examples = [str(x) for x in raw_examples if str(x).strip()]
    elif isinstance(raw_examples, str) and raw_examples.strip():
        good_examples = [x.strip() for x in raw_examples.split("|||") if x.strip()]
    else:
        good_examples = [
            '{"thought": "いつも優しく声をかけてくれて、すごく安心するんですよ…！"}',
            '{"thought": "ちょっとからかわれてばかりで、まだ何を考えてるのか読めないんですよ…！"}',
            '{"thought": "まだあまりお話しできてないから、少し緊張しちゃうんですよ…！"}',
            '{"thought": "憎まれ口ばかりなのに、たまに気遣ってくれるから調子が狂うんですよ…！"}',
        ]

    prompt_parts = [
        "【キャラクター設定】",
        f"あなたはDiscordで会話する「{bot_name}」です。",
        char_instruction,
        "",
        f"【相手「{user_name}」との現在の関係と心情】",
        f"- 親密度: {affinity:+.2f} / 信頼度: {trust:+.2f} / 好意: {affection:+.2f} / 反感: {hatred:+.2f}",
        f"- 心の距離感: {distance_summary}",
    ]

    if logs_text:
        prompt_parts.extend([
            "",
            f"【相手「{user_name}」との最近のやり取り】",
            logs_text,
        ])
    else:
        prompt_parts.extend([
            "",
            "【状況】",
            f"相手「{user_name}」とはまだ対話の記録がほとんどありません。",
        ])

    prompt_parts.extend([
        "",
        "【指示】",
        "上記の関係性や普段のやり取りを踏まえて、",
        f"「{bot_name}」自身の一人称{first_person_desc}と口調で、相手「{user_name}」に対する今の率直な本音を、相手に直接語りかけるように1文（20〜50文字）で出力してください。",
        "",
        "【良い出力例】",
    ])
    prompt_parts.extend(good_examples)
    prompt_parts.extend([
        "",
        "【絶対厳守ルール】",
        "1. 客観的な状況説明や解説文（『〜を考慮し』『〜前向きな一言を出力する』等）は絶対に出力しないでください。",
        "2. 上記の対話記録やメモの文章をそのままコピー・丸写ししてはいけません。",
        "3. 必ずキャラクター自身の話し言葉（セリフ）として直接記述してください。",
        f"4. 誰かの名前を聞かれたこと等の単一の出来事に対する一時的困惑（『急に名前を聞かれて』等）ではなく、相手「{user_name}」本人の普段のキャラクターや接し方・態度に対する本音の印象を出力してください。",
    ])

    return "\n".join(prompt_parts)
