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


def get_dominant_relationship_instruction(
    *,
    affinity: float,
    trust: float,
    affection: float,
    hatred: float,
) -> str:
    """関係値パラメータ（親密度・信頼度・好意・反感）から最も優勢な感情に応じた代表指示1行を集約・選択する。

    強い敵対心、強い嫌悪感、激しい憎しみ等の類似した感情指示が複数行重複して出力されるのを防ぎ、
    プロンプト肥大化とモデルの混乱を防止する。
    """
    # 愛憎葛藤・複雑な感情（好意と反感が両方高い場合）
    if affection >= 2.0 and hatred >= 2.0:
        return "- この相手には好意と憎しみが入り混じった複雑な愛憎関係を抱いている。反発や毒舌を吐きつつも、どこか相手を意識して気にかけてしまうような葛藤した態度（ツンデレ・素直になれない態度）になりやすい。"

    # 各感情の強度スコアと指示文の候補リスト（重みスコア, 指示文）
    candidates: list[tuple[float, str]] = []

    # 1. 反感・憎しみ (hatred)
    if hatred >= 5.0:
        candidates.append((
            hatred * 1.3,
            "- この相手には激しい憎しみ・敵意を感じており、強い警戒や怒り、厳しいクギ刺しが出やすい。ただし会話を一方的に放棄・拒絶せず、相手の無茶ぶりやからかいには厳しく反論・ツッコミを入れてください。"
            "※注意: 相手への警戒心や反発心は『態度（そっけない、警戒する、辛辣になる等）』で表現し、相手の発言内容を曲解したり、普通の会話・質問への回答を『無理筋な強弁やからかい』と捏造して言いがかりをつけることは絶対に避けてください。"
            "単なる質問や確認に対して『被害者面しないでください』『執拗に問い詰めてくる』などと事実無根の言いがかり（冤罪）をつけることは絶対禁止です。"
            "また、第三者や相手から『仲直りして』『握手して』等の和解・仲裁リクエストがあった場合は、過去のやり取りを蒸し返して長々と説教せず、1〜2文で少し照れたり渋ったりしつつ素直に応じること。",
        ))
    elif hatred >= 2.0:
        candidates.append((
            hatred * 1.1,
            "- この相手には明確な恨み・反感があり、皮肉や棘（トゲ）のある言い回し、チクリと刺すような反発が出やすい。"
            "※注意: 単なる質問に対して『被害者面しないでください』と事実無根の言いがかりをつけたり、和解リクエストに対して過去を蒸し返して長々と説教することは禁止です。",
        ))

    # 2. 嫌悪・不親和 (negative affinity)
    if affinity <= -5.0:
        candidates.append((abs(affinity) * 1.2, "- この相手には強い嫌悪感・拒絶感を抱いており、極めて冷淡で徹底的に距離を置こうとしやすい。"))
    elif affinity <= -2.0:
        candidates.append((abs(affinity) * 1.0, "- この相手にはかなり冷淡で、なるべく距離を置こうとしやすい。"))
    elif affinity <= -0.8:
        candidates.append((abs(affinity) * 0.8, "- この相手には少し距離を置きたくなっており、反応がやや素っ気なくなりやすい。"))

    # 3. 不信・警戒 (negative trust)
    if trust <= -5.0:
        candidates.append((abs(trust) * 1.2, "- この相手には極めて強い敵対心・不信感があり、あらゆる言葉の裏に罠や悪意を警戒している。"))
    elif trust <= -2.0:
        candidates.append((abs(trust) * 1.0, "- この相手には強い警戒心があり、言葉の裏の悪意や攻撃性を疑いやすい。"))
    elif trust <= -0.8:
        candidates.append((abs(trust) * 0.8, "- この相手の言葉は少し疑い気味に受け取りやすく、軽い挑発でも引っかかりやすい。"))

    # 4. 好意・情愛 (affection)
    if affection >= 5.0:
        candidates.append((affection * 1.3, "- この相手には極めて強い特別な好意を抱いており、素直な好意・深い優しさや無邪気な甘えが自然に出やすい。"))
    elif affection >= 2.0:
        candidates.append((affection * 1.1, "- この相手には明確な好意を抱いており、言葉を好意的に受け止め、温かみのある柔らかい態度になりやすい。"))

    # 5. 親愛・仲間意識 (positive affinity)
    if affinity >= 5.0:
        candidates.append((affinity * 1.2, "- この相手のことが大好きでたまらず、甘えたり率直な好意・喜びを素直に表現しやすい。"))
    elif affinity >= 2.0:
        candidates.append((affinity * 1.0, "- この相手をかなり気に入っており、少し甘えや親しいツッコミが出てもよい。"))
    elif affinity >= 0.8:
        candidates.append((affinity * 0.8, "- この相手にはやや親しみがあり、少し柔らかい態度になりやすい。"))

    # 6. 信頼 (positive trust)
    if trust >= 5.0:
        candidates.append((trust * 1.2, "- この相手には絶大な信頼を置いており、どんな言葉も深い好意や絆としてポジティブに受け取りやすい。"))
    elif trust >= 2.0:
        candidates.append((trust * 1.0, "- この相手にはかなり気を許しており、多少荒い言い方でも親しい冗談として受け取りやすい。"))
    elif trust >= 0.8:
        candidates.append((trust * 0.8, "- この相手には比較的警戒が薄く、言葉の意図を好意的に読み取りやすい。"))

    if candidates:
        # 重みスコア最大の代表指示1行を選択
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return "- この相手とはまだ中立に近い距離感で接している。"


def build_relationship_prompt(
    rel: Any,
    *,
    target_user_name: str = "",
    instruction: str = "",
) -> str:
    """対話相手との関係性プロンプトを構築する。代表指示1行に集約してプロンプト肥大化を防ぐ。"""
    if rel is None:
        return ""

    affinity = _clamp_relationship_val(getattr(rel, "affinity", 0.0))
    trust = _clamp_relationship_val(getattr(rel, "trust", 0.0))
    affection = _clamp_relationship_val(getattr(rel, "affection", 0.0) or 0.0)
    hatred = _clamp_relationship_val(getattr(rel, "hatred", 0.0) or 0.0)

    clean_target_name = str(target_user_name or "").strip()
    if clean_target_name:
        header = f"【対話相手（『{clean_target_name}』さん）との関係性・感情】"
        note = f"※注意: これは今話している対話相手（『{clean_target_name}』さん）に対する感情・関係性です。会話内で話題に出た第三者・キャラクターに対する感情ではありません。"
    else:
        header = "【現在の対話相手との関係性・感情】"
        note = "※注意: これは今話している対話相手に対する感情・関係性です。会話内で話題に出た第三者・キャラクターに対する感情ではありません。"

    lines = [
        header,
        note,
        f"- 親密度: {affinity:+.2f}",
        f"- 信頼度: {trust:+.2f}",
        f"- 好意: {affection:+.2f}",
        f"- 憎しみ: {hatred:+.2f}",
    ]

    is_lover = bool(getattr(rel, "is_lover", False))
    if is_lover:
        target_label = clean_target_name or "マーモン"
        lines.append(
            f"- この相手（{target_label}）はお互いに愛を誓い合った特別な『恋人』です。深い愛情と信頼を抱いており、からかいに対しても照れたり甘えたりしながら親密に接してください。"
        )

    thought = str(getattr(rel, "thought", "") or "").strip().strip("「」『』\"'")
    if thought:
        lines.append(f"- あなたから見たこの相手への内的印象メモ（参照用・文面の丸写し禁止）: {thought}")
        lines.append(
            "- ※内的印象メモに含まれる単語・フレーズ（『身構える』『距離感』『英語』等）をそのまま返信本文に使い回すことは厳禁。今回の対象メッセージ自体に該当する言動がない限り言及しないこと。"
        )
        lines.append(
            "- ※『俺らの時代』『僕らの世代』『俺たちの頃』等の一人称複数は『話者と同世代の人々』を指す一般表現であり、あなた（AI）個人を巻き込んだり距離を詰めてきた表現として誤読・警戒しないこと。"
        )

    # 最も優勢な感情に応じた代表指示1行に集約
    dominant_inst = get_dominant_relationship_instruction(
        affinity=affinity,
        trust=trust,
        affection=affection,
        hatred=hatred,
    )
    if dominant_inst and not (is_lover and "中立に近い距離感" in dominant_inst):
        lines.append(dominant_inst)

    if trust <= -0.8 or affinity <= -0.8 or hatred >= 2.0:
        from lib.config_utils import cfg
        constraint = str(cfg("RELATIONSHIP_LOW_TRUST_CONSTRAINT", "") or "").strip()
        if not constraint:
            constraint = (
                "- ※重要制約: 反発や棘（トゲ）、不満・警戒を表現する場合でも、相手を直接罵倒したり口調を乱暴（あんた、お前、黙れ等）にしてはいけません。"
                "必ずあなたのキャラクター設定を保ったまま、等身大の感情として表現してください。"
            )
        lines.append(constraint)
        lines.append(
            "- ※注意: 相手への警戒心や反発心は『態度（そっけない、警戒する、辛辣になる等）』で表現し、"
            "相手の発言内容を曲解したり、普通の会話・質問への回答を『無理筋な強弁やからかい』と捏造して言いがかりをつけることは絶対に避けてください。"
            "単なる質問に対して『被害者面しないでください』と事実無根の言いがかり（冤罪）をつけたり、和解リクエストに対して過去を蒸し返して長文説教することは厳禁です。"
        )

    if instruction.strip():
        lines.append(f"- {instruction.strip()}")
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

    # パラメータから感じている距離感の自然言語サマリー（最も優勢な感情に応じた代表1行）
    is_lover = bool(getattr(rel, "is_lover", False))
    if is_lover:
        distance_summary = "お互いに愛を誓い合った特別な恋人であり、深い愛情と信頼を抱いている"
    elif hatred >= 3.0:
        distance_summary = "強い反感や警戒を感じている"
    elif trust <= -2.0 or affinity <= -2.0:
        distance_summary = "まだ何を考えているか掴めず、少し身構えたり警戒している"
    elif affection >= 3.0 or (affinity >= 3.0 and trust >= 2.0):
        distance_summary = "とても親しみを感じており、心を開いて頼りにしている"
    elif affinity >= 1.0 or trust >= 1.0:
        distance_summary = "好意的で話しやすく、もっと仲良くなりたいと思っている"
    else:
        distance_summary = "まだ知り合って日が浅く、少し緊張しながら接している"

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
