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
from ...common.ollama_reply_safety import _strip_think_blocks
from ..ollama_chat_types import UserRelationship
from .relationships_logic import _clamp_relationship_value, _fallback_user_thought

log = logging.getLogger("ollama_bot.ollama_chat.emotion.thought")


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
        if appraisal and not looks_like_placeholder_thought(appraisal):
            entry += f"\n  その時感じたこと: {appraisal}"
        lines.append(entry)

    return "\n".join(lines)


def build_relationship_thought_prompt(
    rel: UserRelationship,
    logs: list[dict[str, Any]],
    *,
    bot_name: str = "AI",
    user_name: str = "相手",
) -> str:
    """一言コメント生成用のLLMプロンプトを構築する。"""
    affinity = _clamp_relationship_value(rel.affinity)
    trust = _clamp_relationship_value(rel.trust)
    affection = _clamp_relationship_value(getattr(rel, "affection", 0.0) or 0.0)
    hatred = _clamp_relationship_value(getattr(rel, "hatred", 0.0) or 0.0)
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
        f"【キャラクター設定】",
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
            f"【状況】",
            f"相手「{user_name}」とはまだ対話の記録がほとんどありません。",
        ])

    prompt_parts.extend([
        "",
        "【指示】",
        f"上記の関係性や普段のやり取りを踏まえて、",
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


_PLACEHOLDER_SUBSTRINGS: tuple[str, ...] = (
    "ここに",
    "thought",
    "所感",
    "一言コメント",
    "本音の一言",
    "格納",
    "文字列",
    "テキストを入力",
    "出力する内容",
    "出力内容",
    "プレースホルダー",
    "placeholder",
    "記述して",
    "入力して",
    "記載して",
    "記入して",
    "テンプレート",
    "キー名",
    "文字列として出力",
    "値を文字列として",
    "文字列として",
    "値を出力",
    "を踏まえ",
    "を考慮し",
    "を反映し",
    "を出力する",
    "一言を出力",
    "前向きな一言",
    "距離感を縮めようとする",
    "性格・口調",
    "関係性パラメータ",
    "パラメータ",
    "受け止めメモ",
    "私の価値観",
    "ツイートへの反応",
    "出来事の評価",
    "心の声",
    "（感情:",
    "(感情:",
    "名前を聞かれ",
    "名前を出され",
    "名前を言われ",
    "急に名前",
    "突然名前",
    "誰のことか",
    "意図が掴めない",
    "意図がつかめない",
    "意図がわからない",
)


def _has_japanese_char(text: str) -> bool:
    """日本語文字（ひらがな、カタカナ、漢字）が含まれているかを判定する。"""
    return bool(re.search(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", text))


def looks_like_placeholder_thought(text: str | None) -> bool:
    """生成された所感がプロンプトの例文・プレースホルダー・メタ指示文であるかを判定する。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    if len(cleaned) < 5:
        return True
    if not _has_japanese_char(cleaned):
        return True

    lower = cleaned.lower()
    for sub in _PLACEHOLDER_SUBSTRINGS:
        if sub in lower:
            return True

    # 「ここに〜」「〜を格納」「〜を出力」などの正規表現
    if re.search(r"ここに", cleaned):
        return True
    if re.search(r"(格納|出力|記述|入力|記載|記入)した?(文字列|テキスト|内容|もの)?", cleaned):
        return True
    if re.search(r"(?:踏まえ|考慮し|反映し|意識し).*(?:出力|記述|生成|作成)", cleaned):
        return True
    if re.search(r"(?:出力|記述|生成|作成)する[。！\s]*\Z", cleaned):
        return True
    if re.search(r"[（(]感情:\s*[a-zA-Z_]+[）)]", cleaned):
        return True
    if re.search(r"(?:名前|誰か).*?(?:聞かれ|出され|言われ|呼ばれ).*?(?:戸惑|わから|掴め|つかめ|困惑|意図)", cleaned):
        return True

    return False


def looks_like_copied_log_appraisal(text: str, logs: list[dict[str, Any]]) -> bool:
    """生成されたテキストが過去の対話ログの appraisal（受け止めメモ）の丸写しであるかを判定する。"""
    if not text or not logs:
        return False
    cleaned = text.strip()
    for item in logs:
        appraisal = str(item.get("bot_appraisal", "") or "").strip()
        if not appraisal:
            continue
        appraisal_clean = re.sub(r"[（(]感情:\s*[a-zA-Z_]+[）)]", "", appraisal).strip()
        if not appraisal_clean:
            continue
        # 完全一致または部分一致
        if cleaned == appraisal_clean or appraisal_clean in cleaned or cleaned in appraisal_clean:
            return True
        # 15文字以上の共通部分がある場合
        if len(cleaned) >= 15 and len(appraisal_clean) >= 15:
            if cleaned[:15] in appraisal_clean or appraisal_clean[:15] in cleaned:
                return True
    return False


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

