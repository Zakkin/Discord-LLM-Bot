"""AIエージェント・ハーネス 外部調査プロンプト構築モジュール (research_prompts.py)

フェーズ1. コンテキストの構築（知覚・記憶の引き出し）において、
Web検索やブラウジング等で取得した外部調査結果（Verified Facts）を
推論用プロンプトへ統合・フォーマットする。
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from lib.config_utils import cfg, cfg_bool, cfg_int
from ollama_bot.common.ollama_helpers import truncate_text

_QUESTION_OR_EXPLANATION_PATTERN = re.compile(
    r"[?？]|"
    r"(?:教え|おしえ)(?:て|ろ|てくれ|てもら)|"
    r"(?:どういう|どんな|何|なに|誰|だれ|どこ|いつ|なぜ|なんで|どうして)(?:こと|もの|やつ|ですか|なの|なん|？|\?|$)|"
    r"(?:どう思|どうおも)|"
    r"(?:解説|説明|詳細|kwsk|違いを|オススメ|おすすめ)|"
    r"(?:知って|しって)(?:る|ますか|るか)|"
    r"(?:ある？|ありますか|あるかな)"
)

_CASUAL_EXPERIENCE_VERBS = (
    "飲んだ", "食べた", "食った", "買った", "行った", "行ってきた",
    "見てきた", "観てきた", "買ってきた", "食べてきた", "食ってきた", "飲んできた",
    "見た", "観た", "遊んだ", "やった", "使った", "届いた", "試した",
    "手に入れた", "当たった", "読んだ", "聴いた", "きいた", "頼んだ", "注文した", "作った", "泊まった",
    "飲んでみた", "食べてみた", "買ってみた", "行ってみた", "使ってみた", "試してみた",
)

_CASUAL_EXPERIENCE_ADJECTIVES = (
    "美味しかった", "おいしかった", "不味かった", "まずかった",
    "すごかった", "凄かった", "楽しかった", "面白かった", "おもしろかった",
    "よかった", "良かった", "微妙だった", "高かった", "安かった",
    "最高だった", "綺麗だった", "きれいだった",
)

_CASUAL_SHARE_ENDINGS = ("ぞ", "ぜ", "ね", "よ", "わ", "な")


def is_casual_share_message(text: str) -> bool:
    """ユーザー発言が日常の体験報告・感想・雑談であるかを判定する。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    # 疑問符が含まれる場合は質問の可能性が高い
    if "?" in raw or "？" in raw:
        return False
    # 明示的な質問・解説要求表現がある場合は除外
    if _QUESTION_OR_EXPLANATION_PATTERN.search(raw):
        return False
    # 体験動詞の過去形が含まれる
    if any(verb in raw for verb in _CASUAL_EXPERIENCE_VERBS):
        return True
    # 感想・状態表現が含まれる
    if any(adj in raw for adj in _CASUAL_EXPERIENCE_ADJECTIVES):
        return True
    # 「だった」「でした」が含まれる
    if "だった" in raw or "でした" in raw:
        return True
    # 末尾が「〜ぞ」「〜ね」「〜よ」「〜ぜ」「〜わ」「〜な」等の報告・共有調
    clean_tail = re.sub(r"[〜～!！♪\s]+$", "", raw)
    if any(clean_tail.endswith(ending) for ending in _CASUAL_SHARE_ENDINGS):
        return True
    return False


_CONSULTATION_MESSAGE_PATTERN = re.compile(
    r"(?:してる人(?:いない|おらん|いる)|やってる人(?:いない|おらん|いる)|"
    r"使ってる人(?:いない|おらん|いる)|聞きたい(?:けど|んだが|んだけど|んですが)|"
    r"どうすりゃいい|どうすればいい|どうしたらいい|途方に暮れ|"
    r"困って(?:る|いる|ます)|悩んで(?:る|いる)|"
    r"(?:いい|良い|代わりの|代替の)?(?:方法|やり方|手段|ソフト|ツール|対策|解決策)(?:ない|ある|教えて)|"
    r"みんなどう(?:してる|やってる))",
    re.IGNORECASE,
)


def is_consultation_or_problem_solving_message(text: str) -> bool:
    """ユーザー発言が困りごと・意見募集・解決策や代替手段の相談であるかを判定する。"""
    raw = str(text or "").strip()
    if not raw or len(raw) < 8:
        return False
    return bool(_CONSULTATION_MESSAGE_PATTERN.search(raw))


_EXPLICIT_DEFINITION_OR_SEARCH_REQUEST_RE = re.compile(
    r"(?:とは|って何|ってなに|って誰|ってだれ|ってどこ|について|詳細|意味|調べ|検索|まとめ|ニュース|速報)",
    re.IGNORECASE,
)


def is_contextual_reaction_or_tsukkomi_message(
    text: str,
    *,
    intent_info: dict[str, Any] | None = None,
    context_lines: list[str] | None = None,
    topic_break: bool = False,
) -> bool:
    """ユーザー発言が直前の会話に対する短文リアクション・ツッコミ・比喩（造語）であるかを判定する。"""
    if topic_break:
        return False
    raw = str(text or "").strip()
    if not raw:
        return False
    clean_raw = re.sub(r"^（[^（）\n]{1,120}への返信）[\s\n]*", "", raw).strip() or raw
    if "?" in clean_raw or "？" in clean_raw:
        return False
    if re.search(r"https?://|www\.", clean_raw):
        return False
    if _QUESTION_OR_EXPLANATION_PATTERN.search(clean_raw):
        return False
    if _EXPLICIT_DEFINITION_OR_SEARCH_REQUEST_RE.search(clean_raw):
        return False
    if is_consultation_or_problem_solving_message(clean_raw):
        return False

    payload = dict(intent_info or {})
    delib_candidate = payload.get("deliberation_info")
    delib: dict[str, Any] = dict(delib_candidate) if isinstance(delib_candidate, dict) else {}
    intent_str = str(payload.get("intent") or delib.get("intent") or "").strip()
    strategy_str = str(payload.get("reply_strategy") or delib.get("reply_strategy") or "").strip()
    avoid_str = str(payload.get("avoid") or delib.get("avoid") or "").strip()
    topic_origin_str = str(payload.get("topic_origin") or delib.get("topic_origin") or "").strip()
    target_is_ai = payload.get("target_is_ai")

    if len(clean_raw) <= 35 and (
        intent_str in {"雑談", "ツッコミ", "ボケ", "相槌", "感想", "からかい"}
        or strategy_str in {"見守り", "外野ツッコミ", "等身大ツッコミ", "共感", "受容"}
        or "文字通りの意味" in avoid_str
        or (target_is_ai is False and bool(topic_origin_str))
    ):
        return True

    # 句読点や疑問符のない短い日本語体言止め・一言リアクション（例: 「泥棒会議」）
    has_jp = bool(re.search(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", clean_raw))
    if has_jp and 0 < len(clean_raw) <= 15 and not is_casual_share_message(clean_raw):
        if context_lines or topic_origin_str or len(clean_raw) <= 12:
            return True

    return False


def build_research_context_text(
    context_lines: list[str] | None = None,
    effective_user_text: str = "",
    original_user_text: str = "",
) -> str:
    """直近文脈とユーザー発言から調査用コンテキストテキストを構築する。"""
    parts: list[str] = []
    if context_lines:
        parts.append("\n".join(context_lines[-6:]))
    if effective_user_text and effective_user_text != original_user_text:
        parts.append(effective_user_text)
    return "\n".join(part for part in parts if part).strip()


def build_research_reply_base_prompt(
    *,
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    topic_break: bool = False,
    last_assistant_text: str = "",
    intent_info: dict[str, Any] | None = None,
    media_prompt_parts: list[str] | None = None,
    is_casual_share: bool | None = None,
    context_lines: list[str] | None = None,
    is_contextual_reaction: bool | None = None,
) -> str:
    """外部調査結果を取り込むための基本プロンプトを構築する。"""
    payload = dict(intent_info or {})
    delib_candidate = payload.get("deliberation_info")
    delib: dict[str, Any] = dict(delib_candidate) if isinstance(delib_candidate, dict) else {}
    topic_origin_val = str(payload.get("topic_origin") or delib.get("topic_origin") or "").strip()
    reply_strategy_val = str(payload.get("reply_strategy") or delib.get("reply_strategy") or "").strip()
    avoid_val = str(payload.get("avoid") or delib.get("avoid") or "").strip()

    lead = "以下のメッセージは、直前の流れとは別話題です。" if topic_break else "以下のメッセージに、外部調査結果を踏まえて直接答えてください。"
    ref_match = re.match(r"^（([^（）\n]{1,120})への返信）[\s\n]*", user_text_for_prompt)
    parts = [lead]
    clean_target_text = user_text_for_prompt
    if ref_match:
        ref_info = ref_match.group(1).strip()
        clean_user_text = user_text_for_prompt[ref_match.end():].strip() or user_text_for_prompt
        clean_target_text = clean_user_text
        if ref_info.startswith("あなたの「"):
            parts.append(f"【あなた自身の直前発言（相手はこれに返信しています）】: {ref_info}")
            parts.append(f"対象メッセージ: {clean_user_text}")
            parts.append("※対象メッセージは、上記のあなた自身の発言（質問・戸惑い・ツッコミ）に対する相手からの答え・返事です。あなたの発言とセットで意味を読み取り、会話のキャッチボールとして返してください。")
        else:
            parts.append(f"【返信元の相手発言】: {ref_info}")
            parts.append(f"対象メッセージ: {clean_user_text}")
            parts.append("※返信元の相手発言の話題に引きずられず、対象メッセージ（最新のユーザー発言）に直接答えてください。")
    else:
        parts.append(f"対象メッセージ: {user_text_for_prompt}")

    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    has_url = bool(re.search(r"https?://|www\.", user_text_for_prompt))
    is_url_followup = not topic_break and has_url and (
        bool(re.search(r"(?:これか|これのこと|これですか|これ|それ|あれ)", user_text_for_prompt))
        or bool(re.search(r"[?？]", user_text_for_prompt))
    )
    if is_casual_share is None:
        is_casual_share = is_casual_share_message(clean_target_text) and not is_url_followup
    is_consultation = is_consultation_or_problem_solving_message(clean_target_text)
    if is_contextual_reaction is None:
        is_contextual_reaction = (
            not topic_break
            and not is_url_followup
            and not is_consultation
            and not is_casual_share
            and is_contextual_reaction_or_tsukkomi_message(
                clean_target_text,
                intent_info=payload,
                context_lines=context_lines,
                topic_break=topic_break,
            )
        )

    if not topic_break and context_lines:
        recent_ctx = [str(line).strip() for line in context_lines[-6:] if str(line).strip()]
        if recent_ctx:
            parts.extend([
                "",
                "【直前の会話文脈】",
                *recent_ctx,
            ])

    if not topic_break and (topic_origin_val or reply_strategy_val or avoid_val):
        memo_lines = ["", "[返答前の内省メモ]"]
        if topic_origin_val:
            memo_lines.append(f"- 話題の起点・背景: {topic_origin_val}")
        if reply_strategy_val:
            memo_lines.append(f"- 返答方針: {reply_strategy_val}")
        if avoid_val:
            memo_lines.append(f"- 避けること: {avoid_val}")
        parts.extend(memo_lines)

    parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{target_name}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウントID/ユーザー名ではなく必ず現在の対話相手のサーバーニックネーム（『{target_name}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
        "",
        *(["会話の続きに無理やり合わせなくてよいです。"] if topic_break else []),
        *(
            [
                "※直前の会話文脈を受けたURLの確認・共有です。直前の会話で話題になっていたこと（汚れそう、持ち歩けない等の感想・懸念）を踏まえ、URL調査結果で見つかった特徴を受容・肯定して等身大の共感やツッコミで返してください。",
                "直前の会話文脈とURL調査結果を結びつけ、会話の流れを自然に継続してください。",
            ]
            if is_url_followup
            else (
                [
                    "※相手からの日常の体験報告・感想・雑談です。相手の発言（体験や感想）に対する等身大のリアクション（共感・相槌・ツッコミ）を主軸にし、外部調査結果は前提知識として理解するのみに留めてください（相手への豆知識や解説の展開は厳禁）。外部調査結果の引用丸写しや一方的な長文講釈は禁止です。",
                    "会話履歴、長期記憶、直前のAI発言に別の話題があっても、その話題へ戻らないでください。",
                ]
                if is_casual_share
                else (
                    [
                        "※相手は困りごとや疑問について意見・解決策・代替手段を求めています。検索結果の丸写しや長文の解説講義にはせず、相手が求めている解決につながる実用的な情報・方法をキャラクターの言葉で1〜3文で簡潔に教えてあげてください（『私も困ったことがある』等の自己投影だけで終わらせることは厳禁）。",
                        "会話履歴、長期記憶、直前のAI発言に別の話題があっても、その話題へ戻らないでください。",
                    ]
                    if is_consultation
                    else (
                        [
                            "※直前の会話文脈を踏まえたリアクション・ツッコミ・短文発言です（辞書的な意味や解説を求める質問ではありません）。",
                            "外部調査結果は『実在する固有ネタや作品名かどうか』の裏取り（前提知識）としてのみ参照してください。",
                            "もし外部調査結果に対象の言葉そのものの明確な定義がなく、検索クエリの共起語に釣られた無関係なニュースや別作品（例: チョッパーのイベント等）しか並んでいない場合は、その検索結果はノイズとして完全に破棄し、直前の会話の流れ（例: 『絵柄泥棒』という発言やLoRA学習の相談に対する『泥棒会議』というツッコミ・比喩・造語）として解釈してください。",
                            "『調べてみたんですけど〜定義までは確認できなかった』『特定の作品のネタですかね？』といった辞書引き・検索失敗レポートは【絶対厳禁】です。相手のリアクション（ツッコミやネーミングセンス）に対する等身大の返信・ツッコミ返しとして1〜2文で自然に返してください。",
                        ]
                        if is_contextual_reaction
                        else [
                            "会話履歴、長期記憶、直前のAI発言に別の話題があっても、その話題へ戻らないでください。",
                        ]
                    )
                )
            )
        ),
        "今回の返答は、対象メッセージと外部調査結果を最優先してください。",
        "対象メッセージや外部調査結果に出てこない固有名詞・人物・作品名・ミーム・例えを勝手に持ち込まないでください。",
        "外部調査結果に含まれない断定は禁止です。",
    ])

    if last_assistant_text and any(ch in str(last_assistant_text or "") for ch in ("?", "？")):
        parts.extend([
            "",
            "直前のAI発言が質問だった場合だけ、その質問への返答として自然につながるように答えてください。",
            "ただし話題の中心は必ず対象メッセージと外部調査結果に置いてください。",
        ])

    if bool(payload.get("target_is_ai")) and str(payload.get("intent", "")).lower() in {"request", "依頼", "要求", "command", "指示"}:
        parts.extend([
            "",
            "最新のユーザー発言はAIへの依頼または要求である可能性があります。",
            "依頼された内容に対して、頭ごなしな拒絶や放棄を避け、キャラクターの性格や口調を保ったまま自然に返答してください（セリフ指定や言葉遊び・気軽なリクエストの場合はある程度要望を叶えてあげてください）。",
        ])
    if bool(payload.get("should_clarify")):
        parts.extend([
            "",
            "意味が曖昧なら、決めつけずに短く確認してください。",
        ])
    if media_prompt_parts:
        parts.extend(media_prompt_parts)

    return "\n".join(parts).strip()


def build_research_break_base_prompt_text(
    *,
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
) -> str:
    """話題転換（BREAK）時の調査用ベースプロンプトテキストを構築する。"""
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    return "\n".join([
        "以下のメッセージは、直前の流れとは別話題です。",
        f"対象メッセージ: {user_text_for_prompt}",
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{target_name}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウントID/ユーザー名ではなく必ず現在の対話相手のサーバーニックネーム（『{target_name}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
        "",
        "会話の続きに無理やり合わせなくてよいです。",
        "単独で意味が通る質問・依頼なら、その内容に直接答えてください。",
        "直前のAI発言、最近の会話要約、長期記憶を引きずらず、このメッセージ自体に答えてください。",
        "※ただし、対象メッセージや調査結果に以前の会話や記憶にある用語・キャラクターが含まれる場合は、知っている話題として自然に関連付けて言及してください。",
        "このあと外部調査結果が追加される場合は、その調査結果と対象メッセージを最優先してください。",
    ])


def build_x_timeline_break_prompt(
    *,
    user_text_for_prompt: str,
    timeline_block: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
) -> str:
    """X（旧Twitter）ホームタイムライン直近投稿を材料にした話題転換（BREAK）プロンプトを構築する。"""
    clean_target = str(target_name or "").strip() or "ユーザー"
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    parts: list[str] = [
        "以下のメッセージは、直前の流れとは別話題である可能性があります。",
    ]
    if clean_target:
        parts.append(
            f"現在の対話相手（サーバーニックネーム）: 『{clean_target}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）"
        )
    parts.extend([
        f"対象メッセージ: {user_text_for_prompt}",
        "",
        "今回は、自分の長期記憶ではなく、今見えているXホームタイムライン直近投稿を材料にしてください。",
        "タイムラインの空気感、話題の偏り、温度感を踏まえて、Discord上で自然な1つの発言として返してください。",
        "ツイート本文の丸写し、箇条書き要約、投稿者一覧だけの出力は禁止です。",
        "外部投稿に由来する一時的な話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
        "投稿者名、表示名、ユーザーID、プロフィール風の文言に含まれる宣伝句や肩書きは、投稿本文の話題として扱わないでください。",
        "話題判断は本文を最優先にし、投稿者情報は補助情報としてのみ扱ってください。",
    ])
    if reply_style_guard:
        parts.insert(1, reply_style_guard)
    if extra_reply_rule:
        parts.extend(["", extra_reply_rule])
    parts.extend(["", "[Xタイムライン直近投稿]", timeline_block])

    if habit_profile_lines:
        parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if core_beliefs:
        from harness.agent.prompt_parser.prompt_manager.budget import format_core_beliefs_for_prompt
        parts.extend(
            format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=clean_target,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
        )

    if media_prompt_parts:
        parts.extend(list(media_prompt_parts))

    parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{clean_target}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウント名やユーザーIDではなく必ず現在の対話相手のサーバーニックネーム（『{clean_target}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
    ])
    return "\n".join(parts)


def build_img_break_prompt(
    *,
    user_text_for_prompt: str,
    img_block: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
) -> str:
    """img.2chan.net（ふたば）の勢い上位スレッドを材料にした話題転換（BREAK）プロンプトを構築する。"""
    clean_target = str(target_name or "").strip() or "ユーザー"
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    parts: list[str] = [
        "以下のメッセージは、直前の流れとは別話題である可能性があります。",
    ]
    if clean_target:
        parts.append(
            f"現在の対話相手（サーバーニックネーム）: 『{clean_target}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）"
        )
    parts.extend([
        f"対象メッセージ: {user_text_for_prompt}",
        "",
        "今回は、自分の長期記憶ではなく、img.2chan.net(ふたば☆ちゃんねる)の現在の勢い上位5スレッドを材料にしてください。",
        "以下のスレッドから一番面白そうなものを1つ選んで、それを材料にDiscord上で自然な1つの発言をしてください。",
        "提示されたスレッドの中から「あなたが一番面白い・興味を惹かれたスレッド」を1つ選び、その内容を踏まえてDiscord上で自然な1つの発言として返してください。",
        "カタログの丸写し、URLの羅列、箇条書き要約は禁止です。",
        "外部の掲示板から拾ってきた話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
    ])
    if reply_style_guard:
        parts.insert(1, reply_style_guard)
    if extra_reply_rule:
        parts.extend(["", extra_reply_rule])
    parts.extend(["", "[img現在の勢い上位5スレッド]", img_block])

    if habit_profile_lines:
        parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if core_beliefs:
        from harness.agent.prompt_parser.prompt_manager.budget import format_core_beliefs_for_prompt
        parts.extend(
            format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=clean_target,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
        )

    if media_prompt_parts:
        parts.extend(list(media_prompt_parts))

    parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{clean_target}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウント名やユーザーIDではなく必ず現在の対話相手のサーバーニックネーム（『{clean_target}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
    ])
    return "\n".join(parts)


_DISALLOWED_QUERY_PREFIXES = ("q=", "query=", "p=")


def normalize_citation_url(url: str) -> str:
    """引用比較・重複排除用にURLを正規化する（フラグメント除去、末尾スラッシュ整理、小文字化）。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        hostname = (parsed.hostname or "").lower()
        if not scheme or not hostname:
            return raw

        port = parsed.port
        netloc = hostname
        if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
            netloc = f"{hostname}:{port}"

        path = parsed.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")

        return urlunsplit((scheme, netloc, path, parsed.query, ""))
    except Exception:
        return raw


def is_disallowed_citation_url(url: str) -> bool:
    """検索エンジンの検索結果一覧ページや内部/ローカルアドレスを除外判定する。"""
    raw = str(url or "").strip()
    if not raw:
        return True
    try:
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return True

        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return True

        # ローカルアドレス・内部ネットワーク判定
        if hostname in ("localhost", "local", "loopback") or hostname.endswith((".local", ".internal", ".lan")):
            return True
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                return True
        except ValueError:
            pass

        path = (parsed.path or "").lower()
        query = (parsed.query or "").lower()

        # Google
        if "google." in hostname:
            if path.startswith("/search") or path.startswith("/url") or any(query.startswith(p) or f"&{p}" in query for p in _DISALLOWED_QUERY_PREFIXES):
                return True
        # DuckDuckGo
        if "duckduckgo.com" in hostname:
            if path.startswith("/search") or path.startswith("/html") or any(query.startswith(p) or f"&{p}" in query for p in _DISALLOWED_QUERY_PREFIXES):
                return True
        # Bing
        if "bing.com" in hostname:
            if path.startswith("/search") or any(query.startswith(p) or f"&{p}" in query for p in _DISALLOWED_QUERY_PREFIXES):
                return True
        # Yahoo
        if "search.yahoo." in hostname or ("yahoo." in hostname and path.startswith("/search")):
            return True
        # Baidu
        if "baidu.com" in hostname and (path.startswith("/s") or path.startswith("/search")):
            return True
        # Yandex
        if "yandex." in hostname and path.startswith("/search"):
            return True
        # 一般的な検索結果一覧パス
        if path in ("/search", "/search/") and any(query.startswith(p) or f"&{p}" in query for p in _DISALLOWED_QUERY_PREFIXES):
            return True

        return False
    except Exception:
        return True


def sanitize_citation_title(title: str, fallback_domain: str, max_chars: int = 30) -> str:
    """タイトルをMarkdownリンクを壊さないようサニタイズし、必要なら短縮する。"""
    cleaned = str(title or "").strip()
    # 改行を空白に置換
    cleaned = re.sub(r"[\r\n]+", " ", cleaned)
    # 角括弧を全角に変換してMarkdownの [label](url) を保護
    cleaned = cleaned.replace("[", "［").replace("]", "］")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        cleaned = fallback_domain or "参照リンク"

    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "..."
    return cleaned


def is_failure_reply_text(text: str) -> bool:
    """AIの返答がエラーまたは回答不能定型文であるかを判定する。"""
    clean = str(text or "").strip()
    if not clean:
        return True
    failure_patterns = (
        "今のはうまく返せなかった",
        "今のはうまく言葉になっていません",
        "十分な情報が取れなかった",
        "検索結果が見つかりませんでした",
        "URLの読み取りに失敗しました",
        "検索がタイムアウトしました",
        "timeout exhausted",
        "エラーが発生しました",
    )
    return any(p in clean for p in failure_patterns)


def research_citation_sources(
    research_result: dict[str, Any] | None,
    *,
    max_citations: int | None = None,
) -> list[dict[str, Any]]:
    """外部調査結果から有効な引用ソースリストをクレンジング・重複排除して抽出する。"""
    if not isinstance(research_result, dict):
        return []

    limit = max_citations if max_citations is not None else max(min(cfg_int("REPLY_RESEARCH_MAX_CITATIONS", 3), 4), 1)
    seen_urls: set[str] = set()
    citations: list[dict[str, Any]] = []

    raw_sources = research_result.get("sources")
    if not raw_sources:
        return []

    for source in list(raw_sources):
        # dict または object をサポート
        if isinstance(source, dict):
            url = str(source.get("url") or "").strip()
            raw_title = str(source.get("title") or "").strip()
            used_in_answer = source.get("used_in_answer")
            source_type = str(source.get("source_type") or "").strip()
        else:
            url = str(getattr(source, "url", "") or "").strip()
            raw_title = str(getattr(source, "title", "") or "").strip()
            used_in_answer = getattr(source, "used_in_answer", None)
            source_type = str(getattr(source, "source_type", "") or "").strip()

        # used_in_answer が明示的に False の場合は除外
        if used_in_answer is False:
            continue

        if not url or is_disallowed_citation_url(url):
            continue

        normalized = normalize_citation_url(url)
        if not normalized or normalized in seen_urls:
            continue
        seen_urls.add(normalized)

        try:
            fallback_domain = urlsplit(url).netloc or "参照リンク"
        except Exception:
            fallback_domain = "参照リンク"

        clean_title = sanitize_citation_title(raw_title, fallback_domain=fallback_domain)

        citation_entry: dict[str, Any] = {
            "url": url,
            "normalized_url": normalized,
            "title": clean_title,
            "raw_title": raw_title,
            "source_type": source_type,
            "used_in_answer": used_in_answer if used_in_answer is not None else True,
        }
        # 元の source が dict ならその他のキーも保持（後方互換性）
        if isinstance(source, dict):
            for k, v in source.items():
                if k not in citation_entry:
                    citation_entry[k] = v

        citations.append(citation_entry)
        if len(citations) >= limit:
            break

    return citations


def build_research_citation_suffix(
    research_result: dict[str, Any] | None,
    *,
    enabled: bool | None = None,
    max_citations: int | None = None,
    existing_text: str = "",
) -> str:
    """引用サフィックスを構築する（Discordプレビュー抑制フォーマット）。

    enabled が None の場合は環境変数/コンフィグ REPLY_RESEARCH_CITATIONS_ENABLED（デフォルト: False）を参照する。
    """
    if enabled is not None:
        is_enabled = bool(enabled)
    else:
        env_val = os.environ.get("REPLY_RESEARCH_CITATIONS_ENABLED")
        if env_val is not None:
            is_enabled = env_val.strip().lower() in ("1", "true", "yes", "on")
        else:
            is_enabled = cfg_bool("REPLY_RESEARCH_CITATIONS_ENABLED", False)
    if not is_enabled:
        return ""

    citations = research_citation_sources(research_result, max_citations=max_citations)
    if not citations:
        return ""

    existing_clean = str(existing_text or "").strip()
    remaining: list[dict[str, Any]] = []
    for c in citations:
        target_url = str(c.get("url") or "").strip()
        target_norm = str(c.get("normalized_url") or "").strip()
        # すでに本文に URL が含まれている場合は除外
        if existing_clean:
            if target_url and target_url in existing_clean:
                continue
            if target_norm and target_norm in existing_clean:
                continue
        remaining.append(c)

    if not remaining:
        return ""

    prefix = str(cfg("REPLY_RESEARCH_CITATION_PREFIX", "出典: ") or "出典: ")
    links = [f"[{c['title']}](<{c['url']}>)" for c in remaining]
    return f"{prefix}{' / '.join(links)}"


def append_research_citations(
    reply: str,
    research_result: dict[str, Any] | None,
    *,
    enabled: bool | None = None,
    max_citations: int | None = None,
) -> str:
    """返答文に引用サフィックスを安全に付与する（二重付与防止・エラー除外）。"""
    base_reply = str(reply or "").strip()
    if not base_reply:
        return base_reply

    if is_failure_reply_text(base_reply):
        return base_reply

    prefix = str(cfg("REPLY_RESEARCH_CITATION_PREFIX", "出典: ") or "出典: ").strip()
    if prefix and prefix in base_reply and ("http://" in base_reply or "https://" in base_reply):
        # 既に引用フッターが付与されている場合は二重付与しない
        return base_reply

    suffix = build_research_citation_suffix(
        research_result,
        enabled=enabled,
        max_citations=max_citations,
        existing_text=base_reply,
    )
    if not suffix:
        return base_reply

    if suffix in base_reply:
        return base_reply

    max_chars = 1900
    joined = f"{base_reply}\n\n{suffix}"
    if len(joined) <= max_chars:
        return joined

    reserved = len(suffix) + 2
    trimmed = truncate_text(base_reply, max(max_chars - reserved, 80))
    return f"{trimmed}\n\n{suffix}"


__all__ = [
    "is_casual_share_message",
    "is_consultation_or_problem_solving_message",
    "is_contextual_reaction_or_tsukkomi_message",
    "build_research_context_text",
    "build_research_reply_base_prompt",
    "build_research_break_base_prompt_text",
    "build_x_timeline_break_prompt",
    "build_img_break_prompt",
    "normalize_citation_url",
    "is_disallowed_citation_url",
    "sanitize_citation_title",
    "is_failure_reply_text",
    "research_citation_sources",
    "build_research_citation_suffix",
    "append_research_citations",
]
