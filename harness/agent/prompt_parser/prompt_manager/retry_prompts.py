"""ハーネス 1-1. プロンプトマネージャー (フェーズ1) - リトライ用プロンプト生成

リトライ時のプロンプト生成および思考抑止サフィックス付与を担当するモジュール。
50KB Rule遵守のため責務分離された独立モジュール。
"""
from __future__ import annotations

import logging
import re

from .budget import is_assistant_echo_memory_line, is_chat_log_or_system_line

log = logging.getLogger("harness.agent.prompt_manager.retry_prompts")

CHAT_REPLY_OUTPUT_SUFFIX = (
    "思考プロセスや思考タグ（think）、前置き、解説は出力せず、"
    "あなたのキャラクター（Assistant）としての自然な日本語の返答本文だけを1〜2文で直接出力してください。"
)


def append_chat_reply_output_suffix(prompt: str) -> str:
    """プロンプト末尾に思考抑止指示を確実に付与する。"""
    normalized = str(prompt or "").strip()
    if not normalized:
        return CHAT_REPLY_OUTPUT_SUFFIX
    if CHAT_REPLY_OUTPUT_SUFFIX in normalized:
        return normalized
    return f"{normalized}\n\n{CHAT_REPLY_OUTPUT_SUFFIX}"


def _clean_memory_section_for_retry(prompt: str, last_ai_text: str = "") -> str:
    """リトライ時に推論モデルがコピペするのを防ぐため、記憶セクション内の会話ログ行や直前発言引き写し行を除去する。"""
    pattern = re.compile(
        r"(\n*この相手の過去の記憶や参考情報:[\s\S]*?)(?=\n\n(?:【|対象メッセージ|以下のメッセージ|会話履歴|直前のAI発言|\Z)|\Z)"
    )
    match = pattern.search(prompt)
    if not match:
        return prompt

    section_full = match.group(1)
    lines = section_full.splitlines()
    cleaned_lines: list[str] = []
    has_valid_memory_items = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # セクションヘッダーおよび注意文は保持
        if stripped.startswith("この相手の過去の記憶や参考情報:") or stripped.startswith("※"):
            cleaned_lines.append(line)
            continue

        item_text = stripped[1:].strip() if stripped.startswith("-") else stripped
        # 1. 会話ログ形式（Assistant:, System:, User:, Bot: 等）の除外
        if is_chat_log_or_system_line(item_text):
            continue

        # 2. 直前AI発言との一致または類似行の除外
        if is_assistant_echo_memory_line(item_text, last_assistant_text=last_ai_text):
            continue

        cleaned_lines.append(line)
        if stripped.startswith("-"):
            has_valid_memory_items = True

    # 有効な記憶行が1つも残らなかった場合、セクション全体を除去
    if not has_valid_memory_items:
        prefix = prompt[:match.start()].rstrip()
        suffix = prompt[match.end():].lstrip()
        if prefix and suffix:
            return f"{prefix}\n\n{suffix}"
        return prefix or suffix

    new_section = "\n".join(cleaned_lines)
    return prompt[:match.start()] + "\n\n" + new_section.strip() + prompt[match.end():]


def _slim_base_prompt_for_retry(base_prompt: str) -> str:
    """リトライ時に推論モデルの思考迷走・トークン浪費を防ぐため、内省メモ・関係性パラメータ・会話習慣・重複ルールを除去して徹底スリム化する。"""
    normalized_base = (base_prompt or "").strip()
    if not normalized_base:
        return ""
    normalized_base = re.sub(r"\n*\[返答前の内省メモ\][\s\S]*?\[/返答前の内省メモ\]", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*\[熟考メモ\][\s\S]*?\[/熟考メモ\]", "", normalized_base).strip()
    stop_pat = r"(?=\n\n(?:【|対象メッセージ|以下のメッセージ|会話履歴|\Z)|\Z)"
    normalized_base = re.sub(r"\n*\[今回の返答スタイル\][\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*\[現在の感情状態と相手との関係性\][\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【この相手との関係性】[\s\S]*?(?=\n\n|\Z)", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【現在のあなたの内的状態】[\s\S]*?(?=\n\n|\Z)", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【今回の返答方針】[\s\S]*?(?=\n\n|\Z)", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【あなた自身の価値観・信念・会話文化】[\s\S]*?(?=\n\n|\Z)", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*相手ごとの会話習慣プロファイル:[\s\S]*?(?=\n\n|\Z)", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*追加ルール:[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【過去の話題の引き継ぎ禁止】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【不確実性の表現】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【会話中で言及されたサーバーメンバー】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【返信・会話関係】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【複数人会話の文脈指示】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    # 【会話の状況（ユーザー同士のやり取り）】および【会話の状況（チャンネル全体の雑談への参加）】はブロック全体を消去せず、メタ注意文のみを除去する
    normalized_base = re.sub(r"\n*※注意:[^\n]+メタ説明や客観解説は絶対に口に出さないでください[^\n]*", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*※自分に向けられた発言ではないため[^\n]*", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*※状況:[^\n]+自発的に会話の輪に入って[^\n]*", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*※禁止:[^\n]+被害妄想・困惑の態度は絶対に取らないでください[^\n]*", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*※指示:[^\n]+チャンネルの雑談に参加する外野[^\n]*", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*※相手の発言に対して『急に何言ってるの』[^\n]*", "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【禁止事項: [^】]+】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【冗談・ボケへの対応】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【返答の長さとテンポ】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*【時間帯の整合性】[\s\S]*?" + stop_pat, "", normalized_base).strip()
    # 直前のAI発言テキストの抽出（記憶セクション内の類似・引き写し照合用）
    last_ai_match = re.search(r"直前のAI発言:\s*([^\n]+(?:\n[^\n]+)*?)(?=\n\n|\Z)", normalized_base)
    last_ai_text = last_ai_match.group(1).strip() if last_ai_match else ""

    # この相手の過去の記憶や参考情報: ブロック内に紛れ込んだ Assistant: 行や直前発言の引き写し行を確実に除去（多重防御）
    normalized_base = _clean_memory_section_for_retry(normalized_base, last_ai_text=last_ai_text)

    # リトライ時に推論モデルが直前AI発言をそのまま引用・丸コピペ出力する事故を防ぐため、直前のAI発言ブロックを除去する
    normalized_base = re.sub(r"\n*直前のAI発言:[\s\S]*?" + stop_pat, "", normalized_base).strip()
    normalized_base = re.sub(r"\n*※直前のAI発言は質問を含んでいます[\s\S]*?" + stop_pat, "", normalized_base).strip()
    if CHAT_REPLY_OUTPUT_SUFFIX in normalized_base:
        normalized_base = normalized_base.replace(f"\n\n{CHAT_REPLY_OUTPUT_SUFFIX}", "").replace(CHAT_REPLY_OUTPUT_SUFFIX, "").strip()
    # 多重防御: normalized_base から対象メッセージが脱落していた場合、元の base_prompt から復元する
    if "対象メッセージ" not in normalized_base:
        target_match = re.search(r"【対象メッセージ】[\s\S]*?(?=\n\n|\Z)", base_prompt)
        if target_match:
            normalized_base = f"{normalized_base}\n\n{target_match.group(0).strip()}".strip()
        else:
            target_match2 = re.search(r"対象メッセージ:[^\n]+", base_prompt)
            if target_match2:
                normalized_base = f"{normalized_base}\n\n{target_match2.group(0).strip()}".strip()
    log.debug("[prompt-manager] _slim_base_prompt_for_retry: len %d -> %d", len(base_prompt or ""), len(normalized_base))
    return normalized_base


def build_similar_retry_prompt(
    base_prompt: str,
    rejected_reply: str = "",
    last_assistant_text: str = "",
) -> str:
    """直前発言類似・重複検知時のリトライプロンプトを生成する。"""
    log.info("[prompt-manager] build_similar_retry_prompt generated")
    normalized_base = _slim_base_prompt_for_retry(base_prompt)

    clean_rejected = (rejected_reply or "").strip()
    clean_last_ai = (last_assistant_text or "").strip()

    forbidden_parts: list[str] = []
    if clean_rejected:
        forbidden_parts.append(f"直前の出力文『{clean_rejected}』")
    if clean_last_ai:
        forbidden_parts.append(f"直前のAI発言『{clean_last_ai}』")

    if forbidden_parts:
        forbidden_desc = "および".join(forbidden_parts)
        rule_body = (
            "【厳禁事項: 直前発言・重複表現の完全禁止】\n"
            f"{forbidden_desc}と同じセリフ、同じ単語の組み合わせ、同じオチは絶対に使わないでください。\n"
            "これらの表現は完全に破棄し、全く別の切り口（相手のボケに対するツッコミ、驚き、呆れ、怯えなど）と新しい言葉遣いで1〜2文の返答をゼロから生成してください。"
        )
    else:
        rule_body = (
            "【厳禁事項: 直前発言・重複表現の完全禁止】\n"
            "直前に出力した返答と同じ文面、同じオチ、同じ言い回しを繰り返すことは厳禁です。\n"
            "以前の発言や定型句を引き写さず、最新のユーザー発言（対象メッセージ）に対して新しい視点や感想、自然なリアクションで返答してください。\n"
            "短くてもよいので、最新の相手の言葉に対するあなた自身の直接的なコメントを1〜2文で書いてください。"
        )

    prompt = (
        normalized_base
        + "\n\n再生成ルール:\n"
        + rule_body
    )
    return append_chat_reply_output_suffix(prompt)


def build_role_flip_retry_prompt(base_prompt: str) -> str:
    """主客反転・相手への命令返答検知時のリトライプロンプトを生成する。"""
    log.info("[prompt-manager] build_role_flip_retry_prompt generated")
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    prompt = (
        normalized_base
        + "\n\n重要修正:\n"
        + "今回の最新発言は、ユーザーがAIに向けた依頼または要求です。\n"
        + "AI自身がやるか断るかの返答に修正してください。\n"
        + "『お前がやれ』『お前が踊れ』のように、ユーザーへ主語を反転させてはいけません。"
    )
    return append_chat_reply_output_suffix(prompt)


def build_time_contradiction_retry_prompt(base_prompt: str) -> str:
    """時間帯矛盾（夜に朝挨拶など）検知時のリトライプロンプトを生成する。"""
    log.info("[prompt-manager] build_time_contradiction_retry_prompt generated")
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    prompt = (
        normalized_base
        + "\n\n重要修正:\n"
        + "直前の出力は、現在のサーバ時刻（夜/昼など）と矛盾する時間帯の状況描写や挨拶（例: 夜なのに『朝から』『おはよう』等）が含まれていたため却下されました。\n"
        + "現在時刻と矛盾する時間帯を勝手に捏造せず、現在の時刻・時間帯（システムプロンプト記載）に完全に合わせたセリフを出力してください。"
    )
    return append_chat_reply_output_suffix(prompt)


def build_parrot_retry_prompt(base_prompt: str, original_user_text: str) -> str:
    """オウム返し検知時のリトライプロンプトを生成する。"""
    log.info("[prompt-manager] build_parrot_retry_prompt generated")
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    if not normalized_base:
        normalized_base = f"対象メッセージ: {original_user_text}"

    prompt = (
        normalized_base
        + "\n\n追加ルール:\n"
        + "直前の返答はオウム返し寄りでした。内容は保ちつつ、ゼロから表現を言い直してください。\n"
        + "【禁止事項: オウム返し】\n"
        + "ユーザーの発言したフレーズを、文頭でそのままなぞって復唱（オウム返し）することはやめてください。\n"
        + "悪例: ユーザー「〇〇って知ってる？」→ AI「〇〇知ってるよ」\n"
        + "良例: ユーザー「〇〇って知ってる？」→ AI「知ってる/知らないを自分の言葉で短く返す」\n"
        + "相手の言葉を文頭でリピートせず、あなた自身の言葉や直接リアクションから始めてください。\n"
        + "※話題の対象となっている名詞（固有名詞、食べ物、作品名など）まで無理に別の言葉に言い換える必要はありません。自然に会話を続けてください。\n"
        + "入力文の丸写し・復唱だけで返すのも禁止です。\n"
        + "直前のAI発言を繰り返すのも禁止です。"
    )
    return append_chat_reply_output_suffix(prompt)


def build_output_only_retry_prompt(base_prompt: str) -> str:
    """思考漏れ・制御行漏れ検知時のリトライプロンプトを生成する。"""
    log.info("[prompt-manager] build_output_only_retry_prompt generated")
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    prompt = (
        normalized_base
        + "\n\n最重要:\n"
        + "ここから先は、返答本文だけを1〜2文で出力してください。"
        + "返答は必ず日本語で書いてください。英語の説明文に切り替えてはいけません。"
        + "指示文、説明、見出し、会話ログ、対象メッセージ、追加ルール、箇条書き、引用、注記は一切出力してはいけません。"
        + "出力の先頭を『以下は』『対象メッセージ』『追加ルール』『会話履歴』『直前のAI発言』で始めてはいけません。"
        + "自分の返答本文だけを書いて、その時点で終了してください。"
    )
    return append_chat_reply_output_suffix(prompt)


def build_character_deviation_retry_prompt(base_prompt: str) -> str:
    """キャラクターの口調崩壊・暴言が検知された際のリトライプロンプトを生成する。"""
    from lib.config_utils import cfg
    log.info("[prompt-manager] build_character_deviation_retry_prompt generated")
    instruction = str(cfg("OLLAMA_CHARACTER_DEVIATION_RETRY_INSTRUCTION", "") or "").strip()
    if not instruction:
        instruction = (
            "直前の出力は、キャラクター設定から逸脱した表現や攻撃的な語尾が含まれていたため却下されました。\n"
            "【厳守事項】\n"
            "1. 相手を直接罵倒したり過激な暴言を使ってはいけません。\n"
            "2. あなたの設定された名前・一人称・口調を厳格に維持してください。\n"
            "3. キャラクター設定に応じた適切な態度と語尾で自然に返答してください。"
        )
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    prompt = (
        normalized_base
        + "\n\n最重要修正（キャラクター口調・人格の厳守）:\n"
        + instruction
    )
    return append_chat_reply_output_suffix(prompt)


def build_final_reply_extractor_prompt(raw_text: str) -> str:
    """LLM出力からセリフ本文を最終抽出するプロンプトを生成する。"""
    log.info("[prompt-manager] build_final_reply_extractor_prompt generated: len=%d", len(raw_text or ""))
    return (
        "次の生成文から、Discordに実際に送るべき返答本文だけを取り出してください。\n"
        "生成文そのものを評価したり説明したりしてはいけません。\n"
        "制御文、思考、注釈、見出し、会話ログ、話者ラベル、複数ターンの続きは除去してください。\n"
        "返答候補が複数ある場合は、ユーザーへの最初の自然な1回分だけ残してください。\n"
        "返答本文が見つからない場合は空文字にしてください。\n\n"
        f"生成文:\n{raw_text}"
    )


def build_abnormal_rejection_retry_prompt(base_prompt: str) -> str:
    """荒唐無稽なボケに対する非常識扱い・ヒステリックな対話拒絶が検知された際のリトライプロンプトを生成する。"""
    log.info("[prompt-manager] build_abnormal_rejection_retry_prompt generated")
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    prompt = (
        normalized_base
        + "\n\n最重要修正（ボケへの非常識扱い・対話拒絶の厳禁）:\n"
        + "直前の出力は、相手のボケ（空中で食べる等）に対して相手を『非常識』と見下して非難したり、『言わないでください、怖いですから』とヒステリックに対話を拒絶・逃走していたため却下されました。\n"
        + "【厳守事項】\n"
        + "1. 相手を『非常識』扱いして攻撃・非難したり、ヒステリックに対話を拒絶・逃走してはいけません。\n"
        + "2. 相手の突拍子もないボケには、常識的な観点から等身大でツッコミ（例: 『落としちゃいますよ！』『危ないですから座って食べてください！』等）を入れてください。\n"
        + "3. あなたの設定されたキャラクターの自然な一人称と口調で、1〜2文でツッコミを返してください。"
    )
    return append_chat_reply_output_suffix(prompt)


def build_self_contradiction_retry_prompt(base_prompt: str, last_assistant_text: str = "") -> str:
    """自己矛盾・前言撤回検知時のリトライプロンプトを生成する。"""
    log.info("[prompt-manager] build_self_contradiction_retry_prompt generated")
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    stance_note = ""
    if last_assistant_text:
        stance_note = f"『{last_assistant_text.strip()}』"
    else:
        stance_note = "『過去の自分の発言』"
    prompt = (
        normalized_base
        + "\n\n最重要修正（一貫したスタンスの維持・自己矛盾の厳禁）:\n"
        + "直前の出力は、直前の自分自身の立場や発言と矛盾する前言撤回（直前で『好き』『食べた』『知っている』と言っていたのに『食べてみたい』『美味しいの？』『嫌いですよ』と未体験・不知・嫌悪を装うなど）が含まれていたため却下されました。\n"
        + f"直前の自分の立場{stance_note}と矛盾せず、一貫したスタンスで答えてください。\n"
        + "未体験や不知を装わず、嫌悪や否定への前言撤回も避け、共感や体験を踏まえた自然な返答を1〜2文で直接出力してください。"
    )
    return append_chat_reply_output_suffix(prompt)


