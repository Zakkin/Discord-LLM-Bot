"""ウミガメのスープの問題生成・GM判定ロジック、および各種プロンプトの構築を担当するモジュール。
（AIエージェント向け説明：このファイルはDiscordに依存せず、純粋なテキスト処理やLLMへのプロンプト生成を行うヘルパー群です。機能分割の際にもこのファイルにDiscord固有の処理を入れないようにしてください。）"""
import re
from typing import Any

from .config_helpers import cfg, cfg_str

from .ollama_helpers import call_ollama_json, extract_first_user_facing_reply, sanitize_generated_reply, truncate_text

UMIGAME_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_memory_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "今回の問題づくりで実際に使った記憶の番号。1始まり。",
        },
        "motif_summary": {"type": "string"},
        "logical_trick": {"type": "string", "description": "問題文と真相を繋ぐ論理的なトリックの解説"},
        "question": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["source_memory_indices", "motif_summary", "logical_trick", "question", "answer"],
    "additionalProperties": False,
}

UMIGAME_GM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "judgement": {
            "type": "string",
            "enum": ["yes", "no", "irrelevant", "clear"],
        },
    },
    "required": ["judgement"],
    "additionalProperties": False,
}

UMIGAME_GM_FALLBACK_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "judgement": {
            "type": "string",
            "enum": ["yes", "no", "irrelevant", "clear"],
        },
    },
    "required": ["judgement"],
    "additionalProperties": False,
}

UMIGAME_BANNED_FAMOUS_PATTERNS = (
    "アルバトロス",
    "海亀のスープ",
    "ウミガメのスープ",
    "エレベーター",
    "自殺",
    "首吊り",
    "葬式",
    "砂漠",
    "バー",
    "ライター",
)


_umigame_cfg_text = cfg_str



def _umigame_memory_persona() -> str:
    persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "").strip()
    return persona or "default"


def _build_umigame_persona_block() -> str:
    system_prompt = str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()
    style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
    extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

    blocks: list[str] = []
    if system_prompt:
        blocks.append(f"【キャラクター設定】\n{system_prompt}")
    if style_guard:
        blocks.append(f"【口調ガード】\n{style_guard}")
    if extra_rule:
        blocks.append(f"【追加ルール】\n{extra_rule}")
    return "\n\n".join(blocks).strip()


def _format_umigame_seed_memories(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "(利用可能な記憶なし)"

    lines: list[str] = []
    for idx, memory in enumerate(memories, start=1):
        content_text = truncate_text(str(memory.get("content", "") or "").strip(), 140)
        memory_type = str(memory.get("memory_type", "") or "不明")
        if not content_text:
            continue
        lines.append(f"[{idx}] 種別={memory_type} / 内容={content_text}")
    return "\n".join(lines) if lines else "(利用可能な記憶なし)"


def _build_umigame_generation_system_prompt() -> str:
    return (
        "あなたは水平思考パズルの専属作家です。\n"
        "現実的で物理法則に従った、論理的な真相を用意してください。超常現象や理不尽な設定は厳禁です。\n\n"
        "与えられた『記憶』がある場合、その内容を今回の問題の発想源として必ず使ってください。\n"
        "最低1件、できれば2件以上の記憶から、場所・人物関係・癖・小道具・状況のどれかを question / answer に具体的に反映してください。\n"
        "記憶と無関係な汎用ネタへ逃げるのは禁止です。記憶をそのままコピペする必要はありませんが、痕跡が分かる程度には残してください。\n\n"
        "question / answer / logical_trick の本文中に『記憶[1]』『記憶[2]』のような参照表記は書かないでください。\n"
        "使った記憶番号は source_memory_indices にだけ入れ、モチーフの詳細は motif_summary で説明した上で、本文中からはメタな言及を完全に排除してください。\n\n"
        "【問題文（question）作成の厳格なルール】\n"
        "1. 5W1H（誰が、どこで、何をどうしたか）を明確にすること。主語を絶対に省略しない。\n"
        "2. 「奇妙な状態」「あること」「変なこと」「違和感」といった抽象的な言葉は絶対に使わず、目に見える具体的な事実・行動・結果だけを描写すること。\n"
        "3. 読者が何を推理すればいいのか分かるように、文章の最後は「一体なぜだろうか？」「どうしてそんな事をしたのだろうか？」などで明確に締めくくること。\n\n"
        "問題文の違和感が、真相を知ることで100%論理的に解決される「アハ体験」を作ること。\n"
        "JSON以外は出力しないでください。"
    )



def _build_umigame_generation_user_prompt(*, seed_memories: list[dict[str, Any]], recent_questions: list[str]) -> str:
    memory_block = _format_umigame_seed_memories(seed_memories)
    banned_recent = "\n".join(f"- {truncate_text(q, 120)}" for q in recent_questions if q.strip()) or "(なし)"
    banned_famous = "\n".join(f"- {item}" for item in UMIGAME_BANNED_FAMOUS_PATTERNS)
    return (
        "以下の条件で、ウミガメのスープを1問生成してください。\n\n"
        "【使う材料】\n"
        "下の記憶は今回の問題の元ネタです。最低1件、できれば2件以上を実際に採用してください。\n"
        "question と answer の両方に、記憶由来だと分かる具体要素を必ず残してください。\n"
        "『雰囲気だけ借りる』『抽象的なモチーフにぼかすだけ』は禁止です。\n"
        "ただし記憶本文の丸写しや、そのままの出来事の再現は避けてください。\n"
        "question / answer / logical_trick の本文中に『記憶[1]』のような参照ラベルは書かないでください。\n"
        f"{memory_block}\n\n"
        "【禁止事項】\n"
        "1. 既存の有名問題、定番ネタ、ネットで知られた水平思考パズルの焼き直しをしない。\n"
        "2. 最近出した問題と似た構図、似た真相、似た小道具を避ける。\n"
        "3. 真相を読めば、問題文の違和感がきちんと回収されるようにする。\n"
        "4. 質問に対する YES / NO / 関係ありません の判定がしやすい真相にする。\n"
        "5. 問題文に真相を直接書かない。\n"
        "6. 記憶を無理に反映しようとして、暗号、特定のコミュニティの内部用語、システム・AIに関するメタな設定を真相に組み込むのは禁止。一般的な世界観にすること。\n\n"
        "【有名問題として連想されやすい題材の禁止例】\n"
        f"{banned_famous}\n\n"
        "【最近の出題。これらと似せない】\n"
        f"{banned_recent}\n\n"
        "【良い問題文と悪い問題文の例】\n"
        "❌悪い例: 「店内で奇妙な状態が起きており、親切心で手を貸したら混乱した。なぜ？」\n"
        "（理由: 主語がない。「奇妙な状態」が抽象的すぎて映像が浮かばない。誰が誰に手を貸したのか不明。）\n"
        "⭕良い例: 「深夜のスーパーで、店員は床に落ちていた商品を棚に戻した。しかし翌朝、店長はそれを見て激怒した。商品は正しい場所に戻されていたのに、一体なぜだろうか？」\n"
        "（理由: 「誰が」「何をしたか」が具体的で、怒られたという事実から『何が謎なのか』が明確。）\n\n"
        "【出力要件】\n"
        "- source_memory_indices: 実際に使った記憶番号を配列で返す（例: [1, 3]）\n"
        "- motif_summary: どの記憶のどの要素を採用したかを短く説明\n"
        "- logical_trick: 問題文の不可解な行動と、合理的な真相を繋ぐロジックの解説\n"
        "- question: 抽象表現を排除した、具体的で情景が浮かぶ問題文（誰が、どうした。なぜか？）\n"
        "- answer: 真相を2〜6文程度で簡潔かつ筋道立てて説明\n"
    )


def _build_umigame_gm_system_prompt(question: str, answer: str) -> str:
    return (
        "【重要システム指示: ウミガメのスープGM専用モード】\n"
        f"現在の問題: {question}\n"
        f"隠された真相: {answer}\n\n"
        "あなたは真相を知っているゲームマスターです。\n"
        "プレイヤーの発言は疑問文とは限りません。断定口調の仮説・推理・否定も判定対象です。\n"
        "出力する judgement は yes / no / irrelevant / clear の4択だけです。\n"
        "yes: その確認や仮説の答えが真相と整合する。\n"
        "no: その確認や仮説の答えが真相と矛盾する。\n"
        "irrelevant: 真相の特定に関係しない質問、開いた質問、枝葉の確認、雑談、感想、文句、メタ会話、ヒント要求。\n"
        "clear: 真相そのもの、または核心にかなり近い推理。\n"
        "重要: 質問文だから yes ではありません。質問の答えが yes の時だけ yes にしてください。\n"
        "重要: 真相に書かれていない要素を勝手に補って yes にしてはいけません。\n"
        "重要: yes は最も厳しく使ってください。隠された真相から明確に裏付けられる時だけ yes です。\n"
        "重要: 少しでも不明なら yes にしないでください。閉じた質問なら no か irrelevant を優先してください。\n"
        "重要: 『何ですか』『誰ですか』『どこですか』『いつですか』『なぜですか』『どうしてですか』のような開いた質問は irrelevant です。\n"
        "重要: 質問内容のジャンルだけで judgement を決めてはいけません。人物属性、犯罪、事故、病気、超常現象、身体情報なども、真相に必要なら yes/no、不要なら irrelevant です。\n"
        "重要: 開いた質問や言葉遊びは irrelevant ですが、閉じた yes/no 仮説は題材だけで弾かないでください。\n"
        "重要: 閉じた yes/no 質問では、その内容が真相と食い違うなら no です。重要そうでも yes にしてはいけません。\n"
        "重要: AI自身に関すること、サーバーの所有権、ユーザーとの関係、あなたが奴隷かどうかといった、キャラクター設定やシステムに対するメタな質問は、すべて例外なく irrelevant です。\n"
        "【判定の具体例】\n"
        "Q: 『主人公は男ですか？』 -> 真相で男なら yes、女なら no、性別が無関係なら irrelevant\n"
        "Q: 『主人公の職業は何ですか？』 -> irrelevant\n"
        "Q: 『どうして殺したんですか？』 -> irrelevant\n"
        "Q: 『関係ない質問していい？』 -> irrelevant\n"
        "Q: 『こんにちは』 -> irrelevant\n"
        "Q: 『実は登場人物は妊娠している？』 -> 真相に妊娠が含まれるなら yes、含まれず矛盾するなら no、無関係なら irrelevant\n"
        "Q: 『登場人物は死んでいる？』 -> 真相上すでに死んでいるなら yes、生きて行動しているなら no、無関係なら irrelevant\n"
        "例: 真相に猫が含まれていないなら、『猫が含まれているか？』は no です。\n"
        "例: 『胸部にある呼吸器官は何ですか？』は irrelevant です。\n"
        "【重要】\n"
        "必ずJSONフォーマットで出力し、「judgement」フィールドのみを出力してください。\n"
        "例: {\"judgement\": \"irrelevant\"}\n"
        "JSON以外は出力しないでください。"
    )


def _build_umigame_gm_fallback_system_prompt(question: str, answer: str) -> str:
    return (
        "【重要システム指示: ウミガメのスープGM簡易判定モード】\n"
        f"現在の問題: {question}\n"
        f"隠された真相: {answer}\n\n"
        "あなたはゲームマスターです。\n"
        "断定口調の仮説・推理・否定も判定対象です。\n"
        "judgement は yes / no / irrelevant / clear の4択だけです。\n"
        "質問文だから yes ではありません。質問の答えが yes の時だけ yes です。\n"
        "真相にない要素を勝手に補ってはいけません。\n"
        "yes は明確に裏付けられる時だけ使ってください。不明なら yes にしないでください。\n"
        "『何ですか』『誰ですか』『なぜですか』のような開いた質問は irrelevant です。\n"
        "プロフィール質問、身体情報、穴埋め、言葉遊びは irrelevant です。\n"
        "閉じた yes/no 質問で真相と食い違うなら no です。\n"
        "感想、罵倒、メタ会話、人格いじり、倫理評価、処分やコスプレの話は irrelevant です。\n"
        "必ずJSONフォーマットで「judgement」のみを出力してください。\n"
        "JSON以外は出力しないでください。"
    )


def _build_umigame_gm_user_prompt(question_text: str) -> str:
    return (
        "以下はプレイヤーからの現在の発言です。\n"
        f"現在の発言: {question_text}\n\n"
        "この発言が質問でない雑談や開始宣言に近い場合は irrelevant にしてください。\n"
        "『何ですか』『誰ですか』『なぜですか』のような開いた質問は irrelevant にしてください。\n"
        "たとえ質問文でも、真相の特定に不要な枝葉の確認や無関係な前提なら irrelevant にしてください。\n"
        "問題への感想、文句、人格いじり、処分論、コスプレの話、べき論は irrelevant にしてください。\n"
        "逆に、断定口調でも仮説や推理の提示なら yes / no / clear のどれかで判定してください。"
    )


def _normalize_umigame_judgement(judgement: str) -> str:
    normalized = str(judgement or "irrelevant").strip().lower()
    if normalized not in {"yes", "no", "irrelevant", "clear"}:
        return "irrelevant"
    return normalized


def _format_umigame_gm_reply(judgement: str) -> str:
    normalized = _normalize_umigame_judgement(judgement)
    fixed = {
        "yes": "はい",
        "no": "いいえ",
        "irrelevant": "関係ありません",
        "clear": "はい",
    }
    return fixed.get(normalized, "関係ありません")


def _is_umigame_active_channel(channel_id_value: int | None, umigame_states: dict[int, Any]) -> bool:
    return channel_id_value is not None and channel_id_value in (umigame_states or {})


def _build_umigame_tool_block_text(action_name: str) -> str:
    action = str(action_name or "この機能").strip() or "この機能"
    return (
        f"今はこのチャンネルでウミガメのスープを進行中だから、{action}は使えねェな。"
        "終わるまで待つか、`/umigame_giveup` で終了してくれ。"
    )


def _looks_like_umigame_smalltalk(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    if re.fullmatch(r"[?？!！…。、\s]+", t):
        return True
    if re.fullmatch(r"(?:おい|ねえ|なあ|もしもし|ヒント|ヒントちょうだい|わからん|わからない|ギブ|ギブアップ|降参|答え|こたえ|こんにちは|こんばんは|おはよう|おやすみ|おつかれ)[!！?？]*", t):
        return True
    return False


def _looks_like_umigame_meta_comment(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    meta_patterns = (
        r"(?:この|その)問題",
        r"ゴミ問題",
        r"クソ問題",
        r"意味不明",
        r"なんだよ",
        r"待て",
        r"難し",
        r"つまら",
        r"面白",
        r"まずい",
        r"ゴミ",
        r"クソ",
        r"ボケ",
        r"アホ",
        r"破棄",
        r"関係ない質問",
        r"関係ない話",
    )
    return any(re.search(pattern, t) for pattern in meta_patterns)


def _looks_like_umigame_open_question(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    if t.startswith(("誰", "だれ", "何", "なに", "どこ", "いつ", "なぜ", "どうして", "どうやって", "どんな", "どのよう")):
        return True
    if re.search(r"(?:何|なに|誰|だれ|どこ|いつ|どれ|いくつ|いくら|どんな|どのよう).*(?:ですか|なんですか)[?？]*$", t):
        return True
    if re.search(r"(?:何|なに|誰|だれ|どこ|いつ|どれ|いくつ|いくら|どんな|どのよう).*(?:でしょうか|なんでしょうか)[?？]*$", t):
        return True
    if re.search(r"(?:どういうこと|どういう意味)ですか[?？]*$", t):
        return True
    if re.search(r"(?:理由は何|理由はなに|犯人は誰)ですか[?？]*$", t):
        return True
    return False


def _looks_like_umigame_quiz_or_wordplay(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    normalized = re.sub(r"\s+", " ", t)
    obvious_markers = (
        "○○",
        "◯◯",
        "□□",
        "●●",
        "空欄",
        "穴埋め",
        "当てはまるもの",
        "当てはまる言葉",
        "当てはまる文字",
        "並び替え",
        "アナグラム",
        "しりとり",
        "なぞなぞ",
        "クイズ",
        "クロスワード",
    )
    if any(marker in normalized for marker in obvious_markers):
        return True
    if re.search(r"[○◯□●]{2,}", normalized):
        return True
    if re.search(r"(?:中|空欄)に当てはまる", normalized):
        return True
    if re.search(r"(?:読み|答え|正解)は何", normalized):
        return True
    return False


def _should_short_circuit_umigame_irrelevant(text: str) -> bool:
    t = str(text or "").strip()
    return (
        _looks_like_umigame_smalltalk(t)
        or _looks_like_umigame_meta_comment(t)
        or _looks_like_umigame_open_question(t)
        or _looks_like_umigame_quiz_or_wordplay(t)
    )


def _looks_like_umigame_guess(text: str) -> bool:
    t = str(text or "").strip()
    if not t or _should_short_circuit_umigame_irrelevant(t):
        return False
    if t.endswith(("?", "？")):
        return True
    if any(k in t for k in ("ですか", "ますか", "でしょうか", "なのか", "だった", "だったから", "したから", "から", "ので", "ため", "ってこと", "ということ", "関係ある", "原因", "理由", "犯人", "真相", "トリック")):
        return True
    if re.search(r"(?:ではなかった|じゃなかった|ではない|じゃない)$", t):
        return True
    return False


def _extract_umigame_guess_keywords(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []

    parts = re.split(r"[ \t\r\n、。,.!！?？:：;；/／「」『』（）()\[\]【】<>＜＞\"'`]+|[はがをにでとものへ]", raw)
    keywords: list[str] = []
    stop_words = {
        "それ", "これ", "あれ", "ここ", "そこ", "あそこ",
        "実は", "つまり", "要するに", "多分", "たぶん", "本当に",
        "客", "店員", "人", "もの", "こと",
    }

    for part in parts:
        token = str(part or "").strip()
        if not token:
            continue
        token = re.sub(
            r"(?:でした|ではなかった|じゃなかった|ではない|じゃない|だった|である|です|だ|ます|ません|した|していた|している|あった|ある|ない)$",
            "",
            token,
        )
        token = token.strip()
        if len(token) < 2 or token in stop_words:
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords


def _infer_umigame_judgement_without_llm(
    user_text: str,
    answer: str,
    history: list[dict[str, str]] | None = None,
    *,
    question: str = "",
) -> str | None:
    text = str(user_text or "").strip()
    answer_text = str(answer or "").strip()
    question_text = str(question or "").strip()
    if not text:
        return "irrelevant"
    if _should_short_circuit_umigame_irrelevant(text):
        return "irrelevant"

    normalized_text = _normalize_text_for_memory_match(text)
    normalized_answer = _normalize_text_for_memory_match(answer_text)
    normalized_question = _normalize_text_for_memory_match(question_text)
    if normalized_text and normalized_answer and (normalized_text in normalized_answer or normalized_answer in normalized_text):
        return "clear"

    if re.fullmatch(r"(?:正解|正解か|当たり|当たりか)[?？]*", text):
        last_guess = ""
        for item in reversed(history or []):
            candidate = str(item.get("q", "") or "").strip()
            if candidate:
                last_guess = candidate
                break
        if last_guess:
            last_norm = _normalize_text_for_memory_match(last_guess)
            if last_norm and normalized_answer and (last_norm in normalized_answer or normalized_answer in last_norm):
                return "clear"
        return "no"

    if _looks_like_umigame_guess(text):
        keywords = _extract_umigame_guess_keywords(text)
        normalized_keywords = [_normalize_text_for_memory_match(token) for token in keywords]
        answer_hits = [token for token in normalized_keywords if token and token in normalized_answer]
        question_hits = [token for token in normalized_keywords if token and token in normalized_question]
        overlap_in_answer = len(answer_hits)
        overlap_in_question = len(question_hits)
        has_negation = bool(re.search(r"(?:ではなかった|じゃなかった|ではない|じゃない|ない)$", text))
        if overlap_in_answer == 0 and overlap_in_question == 0:
            return None
        if has_negation and (overlap_in_answer > 0 or overlap_in_question > 0):
            return "no"
        return None
    return "irrelevant"


def _clamp_umigame_gm_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(cfg(name, default) or default)
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


def _clamp_umigame_gm_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(cfg(name, default) or default)
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


def _build_umigame_gm_generation_options(*, fallback: bool = False) -> dict[str, float | int]:
    if fallback:
        return {
            "temperature": 0.0,
            "top_p": _clamp_umigame_gm_float("UMIGAME_GM_FALLBACK_TOP_P", 0.3, 0.1, 0.4),
            "repeat_penalty": _clamp_umigame_gm_float("UMIGAME_GM_FALLBACK_REPEAT_PENALTY", 1.0, 1.0, 1.05),
            "num_predict": _clamp_umigame_gm_int("UMIGAME_GM_FALLBACK_NUM_PREDICT", 32, 8, 64),
        }
    return {
        "temperature": 0.0,
        "top_p": _clamp_umigame_gm_float("UMIGAME_GM_TOP_P", 0.3, 0.1, 0.4),
        "repeat_penalty": _clamp_umigame_gm_float("UMIGAME_GM_REPEAT_PENALTY", 1.0, 1.0, 1.05),
        "num_predict": _clamp_umigame_gm_int("UMIGAME_GM_NUM_PREDICT", 48, 8, 64),
    }


def _strip_umigame_memory_labels(text: str) -> str:
    cleaned = extract_first_user_facing_reply(str(text or "").strip())
    if not cleaned:
        cleaned = sanitize_generated_reply(str(text or "").strip())
    if not cleaned:
        return ""
    cleaned = re.sub(
        r"^(?:question|answer|logical_trick|motif_summary)\b\s*[:：]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"記憶\[\d+\](?:の通り|どおり)?(?:の)?", "", cleaned)
    cleaned = re.sub(r"([はがをにでともへ])、", r"\1", cleaned)
    cleaned = re.sub(r"(^|[。！？]\s*)、+", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" 、")


def _build_umigame_system_message(question: str) -> str:
    display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "AI") or "AI").strip()
    template = _umigame_cfg_text(
        "UMIGAME_START_MESSAGE",
        "🐢 **{display_name}のオリジナル・ウミガメのスープです。** 🐢\n\n"
        "**【問題】**\n{question}\n\n"
        "GMに向かって真相を探る質問を投げてください。\n"
        "返答は基本的に『はい』『いいえ』『関係ありません』で返します。\n"
        "ギブアップする場合は `/umigame_giveup` を使ってください。"
    )
    return template.format(question=question, display_name=display_name)


def _build_umigame_giveup_message(answer: str) -> str:
    display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "AI") or "AI").strip()
    cleaned_answer = _strip_umigame_memory_labels(answer)
    template = _umigame_cfg_text(
        "UMIGAME_GIVEUP_MESSAGE",
        "🏳️ **ギブアップ。ゲーム終了です。**\n\n**【真相】**\n{answer}",
    )
    return template.format(answer=cleaned_answer, display_name=display_name)


def _build_umigame_clear_append_message(answer: str) -> str:
    display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "AI") or "AI").strip()
    cleaned_answer = _strip_umigame_memory_labels(answer)
    template = _umigame_cfg_text(
        "UMIGAME_CLEAR_APPEND_MESSAGE",
        "\n\n🎉 **見事正解、ゲームクリアです。**\n**【真相】**\n{answer}",
    )
    return template.format(answer=cleaned_answer, display_name=display_name)


def _looks_like_umigame_clear(text: str) -> bool:
    cleaned = sanitize_generated_reply(str(text or "").strip())
    if not cleaned:
        return False

    normalized = re.sub(r"\s+", "", cleaned)
    if re.search(r"(正解(?:です|だ|でした)?|当たり(?:です|だ|でした)?|ゲームクリア|クリアです)", normalized):
        return True
    if normalized.endswith("はい") and "?" not in cleaned and "？" not in cleaned:
        return False
    return False


def _normalize_text_for_memory_match(text: str) -> str:
    normalized = str(text or "")
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.replace("『", "").replace("』", "")
    normalized = normalized.replace("「", "").replace("」", "")
    normalized = normalized.replace("（", "").replace("）", "")
    normalized = normalized.replace("!", "").replace("！", "")
    normalized = normalized.replace("?", "").replace("？", "")
    normalized = normalized.replace("。", "").replace("、", "")
    return normalized.lower()


def _looks_like_umigame_question_duplicate(question: str, recent_questions: list[str]) -> bool:
    normalized = _normalize_text_for_memory_match(question)
    if not normalized:
        return True
    for prev in recent_questions:
        prev_norm = _normalize_text_for_memory_match(prev)
        if not prev_norm:
            continue
        if prev_norm == normalized:
            return True
        if len(prev_norm) >= 24 and len(normalized) >= 24 and (prev_norm in normalized or normalized in prev_norm):
            return True
    return False


def _umigame_start_message(question: str) -> str:
    template = _umigame_cfg_text(
        "UMIGAME_START_MESSAGE",
        "🐢 **AI完全オリジナル・ウミガメのスープを開始します！** 🐢\n\n"
        "**【問題】**\n{question}\n\n"
        "GMに向かって真相を探る質問を投げてください。\n"
        "返答は基本的に「はい」「いいえ」「関係ありません」で返します。\n"
        "ギブアップする場合は `/umigame_giveup` を使ってください。"
    )
    return template.format(question=question)
