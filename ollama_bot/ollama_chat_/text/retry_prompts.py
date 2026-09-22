"""
[AI Agent Summary]
リトライ時のプロンプト生成および思考抑止サフィックス付与を担当するモジュール。
50KB Rule遵守のため reply_texts.py から責務分離された独立モジュール。
"""
from __future__ import annotations

import re


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
    return normalized_base


def build_similar_retry_prompt(base_prompt: str) -> str:
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    prompt = (
        normalized_base
        + "\n\n再生成ルール:\n"
        + "直前に出力した返答と同じ文面、同じオチ、同じ言い回しを繰り返すことは厳禁です。\n"
        + "以前の発言や定型句を引き写さず、最新のユーザー発言（対象メッセージ）に対して新しい視点や感想、自然なリアクションで返答してください。\n"
        + "短くてもよいので、最新の相手の言葉に対するあなた自身の直接的なコメントを1〜2文で書いてください。"
    )
    return append_chat_reply_output_suffix(prompt)


def build_role_flip_retry_prompt(base_prompt: str) -> str:
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
    normalized_base = _slim_base_prompt_for_retry(base_prompt)
    prompt = (
        normalized_base
        + "\n\n重要修正:\n"
        + "直前の出力は、現在のサーバ時刻（夜/昼など）と矛盾する時間帯の状況描写や挨拶（例: 夜なのに『朝から』『おはよう』等）が含まれていたため却下されました。\n"
        + "現在時刻と矛盾する時間帯を勝手に捏造せず、現在の時刻・時間帯（システムプロンプト記載）に完全に合わせたセリフを出力してください。"
    )
    return append_chat_reply_output_suffix(prompt)


def build_parrot_retry_prompt(base_prompt: str, original_user_text: str) -> str:
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
    return (
        "次の生成文から、Discordに実際に送るべき返答本文だけを取り出してください。\n"
        "生成文そのものを評価したり説明したりしてはいけません。\n"
        "制御文、思考、注釈、見出し、会話ログ、話者ラベル、複数ターンの続きは除去してください。\n"
        "返答候補が複数ある場合は、ユーザーへの最初の自然な1回分だけ残してください。\n"
        "返答本文が見つからない場合は空文字にしてください。\n\n"
        f"生成文:\n{raw_text}"
    )
