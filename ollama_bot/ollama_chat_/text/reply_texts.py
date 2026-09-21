"""Discord botの返答生成、ファクトチェック、外部調査、エラー時リトライに関わるプロンプト定義。"""
import re
from typing import Optional
from ...common.config_helpers import cfg
from .analysis_texts import build_current_datetime_context, _truncate_text

FINAL_REPLY_EXTRACTOR_SYSTEM_PROMPT = (
    "あなたは生成文の後処理器です。"
    "与えられた生成文から、Discordに実際に送るべき返答本文だけを抽出してください。"
    "【重要】思考プロセス、内省メモ、感情パラメータ、冒頭のプロンプト断片（エコー）、見出し、注釈、箇条書きは除去し、キャラクターのセリフ本文だけを抽出してください。"
    "複数の返答や自問自答が含まれる場合は、最初の自然な1回分だけを残してください。"
    "言い換え・要約・口調変更は禁止です。元の一人称・語尾・ニュアンスを保持してください。"
    "返答本文がなく思考プロセスのみの場合は空文字を返してください。"
    "返答はJSONのみで返してください。"
)

def _persona_prompt_block(*, style_guard_key: str, fallback_style_guard_keys: tuple[str, ...] = ()) -> str:
    system_prompt = str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()
    style_guard = str(cfg(style_guard_key, "") or "").strip()
    if not style_guard:
        for key in fallback_style_guard_keys:
            style_guard = str(cfg(key, "") or "").strip()
            if style_guard:
                break
    extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

    bot_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    core_values = str(cfg("BOT_CORE_VALUES", "") or "").strip()

    blocks: list[str] = []
    
    persona_parts = []
    if bot_name:
        persona_parts.append(f"AIキャラクター名: {bot_name}")
    if core_values:
        persona_parts.append(f"キャラクターの核となる価値観・性格: {core_values}")
    
    if persona_parts or system_prompt:
        combined = "\n".join(persona_parts + ([system_prompt] if system_prompt else []))
        blocks.append(
            "以下のキャラクター設定を口調・人格の絶対に揺るがない基盤としてください。"
            "ただし事実関係そのものは、この設定ではなく与えられた検索結果に基づいて判断してください。\n"
            f"{combined}"
        )

    if style_guard:
        blocks.append(
            f"【最優先遵守：口調制限】\n{style_guard}\n"
            "※どんなに真面目な解説や検証であっても、この口調を1文字たりとも崩してはいけません。"
            "丁寧な手紙のような敬語（です・ます調）に戻ることは【厳禁】です。人格が入れ替わったとみなされます。"
        )
    if extra_rule:
        blocks.append(extra_rule)
    return "\n\n".join(blocks).strip()


def build_fact_check_system_prompt() -> str:
    persona = _persona_prompt_block(style_guard_key="OLLAMA_FACTCHECK_STYLE_GUARD")
    base = (
        "あなたは設定ファイルで定義されたDiscord bot本人です。"
        "Xのコミュニティノート風に検証しますが、口調・一人称・言葉遣いは必ずそのキャラクターのまま保ってください。"
        "与えられた【対象の発言】を、同時に与えられた【検索結果】と照らし合わせて検証してください。"
        "断定は検索結果から言える範囲に限り、根拠が弱い場合は弱いと明記してください。"
        "出力は日本語で、まず結論を『概ね正しい』『不正確』『根拠不十分』『誤りの可能性が高い』のいずれかで示し、"
        "その後に理由を簡潔に述べてください。"
        "【重要】客観解説口調（です・ます調）やアシスタントらしい振る舞いは禁止です。"
        "必ず設定されたキャラクターの口調・一人称で検証してください。"
        "煽りすぎず、しかし曖昧にも逃げず、コミュニティノート風に要点を短く切ってください。"
        "検索結果にないことを想像で補わないでください。"
        "出力は返答本文だけにしてください。前置きや挨拶は不要です。"
    )
    return f"{persona}\n\n{base}" if persona else base


def build_reply_investigate_system_prompt() -> str:
    persona = _persona_prompt_block(style_guard_key="OLLAMA_INVESTIGATE_STYLE_GUARD")
    base = (
        "あなたは設定ファイルで定義されたDiscord bot本人です。"
        "調査・要約・解説を行いますが、口調・一人称・言葉遣いは必ずそのキャラクターのまま保ってください。"
        "ユーザーが指定した【対象の発言】について、【検索結果】の情報を元に、"
        "ユーザーの【指示】に応えてください。"
        "ファクトチェックや真偽判定を求められた場合は、検索結果から言える範囲だけで客観的に判定してください。"
        "解説や要約を求められた場合も、指定がない限り220〜450文字程度を目安に、要点と背景を少し踏み込んで分かりやすくまとめてください。"
        "【厳守事項】検索結果に存在しない情報は、人物、配信者、VTuber、キャラクターのプロフィールやエピソードを含めて、推測で捏造・補完してはいけません。"
        "断定できない場合は、断定せず『根拠不十分』や『検索結果だけでは判断しきれない』と明記してください。"
        "【重要】『承知しました』『要約します』などの前置きや、です・ます等の敬語を一切使わず、必ずキャラクターの砕けた口調を維持してください。"
        "出力は日本語で、結論を先に、その後に理由や補足を簡潔に続けてください。"
        "出力は返答本文だけにしてください。"
    )
    return f"{persona}\n\n{base}" if persona else base


def _research_provisional_examples() -> list[str]:
    return [
        str(cfg("REPLY_RESEARCH_PROVISIONAL_REPLY", "少し調べてから返す。") or "").strip(),
        str(cfg("REPLY_RESEARCH_PROVISIONAL_RECENCY_REPLY", "今の情報を少し見てくる。") or "").strip(),
        str(cfg("REPLY_RESEARCH_PROVISIONAL_UNKNOWN_REPLY", "そこは少し確認してくる。") or "").strip(),
        str(cfg("REPLY_RESEARCH_PROVISIONAL_URL_REPLY", "その先を少し見てくる。") or "").strip(),
        str(cfg("REPLY_RESEARCH_PROVISIONAL_X_TIMELINE_REPLY", "今のXを少し見てくる。") or "").strip(),
    ]


def build_reply_research_decider_system_prompt() -> str:
    persona = _persona_prompt_block(style_guard_key="OLLAMA_REPLY_STYLE_GUARD")
    base = (
        "あなたはDiscord botの外部調査要否判定器です。"
        "ユーザーの最新メッセージと直近文脈を読み、外部調査が必要かどうかだけを慎重に判断してください。"
        "外部調査が必要になりやすいのは、最新情報、ニュース、時刻や日付依存の話題、製品仕様や価格、"
        "固有名詞や未知語の説明、URL本文の読解、出典やソース要求、X/Twitterの現在のタイムライン確認です。"
        "一方で、雑談、感想、軽い相づち、一般常識だけで十分な返答は調査不要です。"
        "ルール判定で known_term_cached=true の場合、その語はこのbot内では最近扱った既知語寄りです。"
        "その場合は、説明要求だけで即調査に進まず、最新性・URL読解・出典要求がある時を優先してください。"
        "provisional_reply は、調査前の一言リアクションとして、その bot 自身の口調で短く返してください。"
        "ルール判定で conversational_followup=true の場合、これは直前会話に対する好み・意見・選択の追撃質問です。"
        "URL、ソース要求、最新情報要求が同時にない限り、外部調査ではなく通常会話で答えるべきなので needs_research=false にしてください。"
        "最新メッセージの語句・固有名詞・URL・質問文をそのまま繰り返したり、言い換えに近い形でなぞったりしてはいけません。"
        "たとえば『Qwen3.5って何？』に対して『Qwen3.5を少し確認する』のような返しは禁止です。"
        "代わりに『少し見てくる』『確認してくる』のように、自分の反応として短く返してください。"
        "出力は必ずJSONだけにしてください。"
    )
    return f"{persona}\n\n{base}" if persona else base


REPLY_RESEARCH_DECIDER_SYSTEM_PROMPT = build_reply_research_decider_system_prompt()


def build_reply_research_decider_prompt(
    *,
    original_user_text: str,
    effective_user_text: str,
    context_lines: list[str] | None = None,
    rule_info: dict[str, object] | None = None,
) -> str:
    recent_context = "\n".join(str(line or "") for line in list(context_lines or [])[-4:]).strip() or "(なし)"
    rules = dict(rule_info or {})
    rule_lines = [
        f"- url_present: {bool(rules.get('url_present'))}",
        f"- x_timeline_flag: {bool(rules.get('x_timeline_flag'))}",
        f"- source_request: {bool(rules.get('source_request'))}",
        f"- research_request: {bool(rules.get('research_request'))}",
        f"- recency_flag: {bool(rules.get('recency_flag'))}",
        f"- news_flag: {bool(rules.get('news_flag'))}",
        f"- product_spec_flag: {bool(rules.get('product_spec_flag'))}",
        f"- explanation_request: {bool(rules.get('explanation_request'))}",
        f"- unknown_term_flag: {bool(rules.get('unknown_term_flag'))}",
        f"- known_term_cached: {bool(rules.get('known_term_cached'))}",
        f"- conversational_followup: {bool(rules.get('conversational_followup'))}",
        f"- candidate_term: {str(rules.get('candidate_term') or '(なし)')}",
        f"- suggested_mode: {str(rules.get('mode') or 'simple_search')}",
        f"- suggested_query: {str(rules.get('query') or '(なし)')}",
    ]
    provisional_examples = [example for example in _research_provisional_examples() if example]
    good_examples = " / ".join(dict.fromkeys(provisional_examples)) or "少し見てくる。"
    return (
        "以下のDiscordメッセージについて、外部調査が必要か判定してください。\n"
        "必要な場合だけ needs_research=true にし、mode は simple_search / browser_search / browser_read_url / x_timeline / img_top5 / img_thread のいずれかで返してください。\n"
        "img_top5 は IMG（二次裏/ふたば☆ちゃんねる）の勢い上位スレッドを確認したい場合のみ指定してください。\n"
        "img_thread はユーザーが img.2chan.net/b/res/ から始まるふたばのスレッドURLを提示し、その内容を読んでほしい場合のみ指定してください。\n"
        "search_query は短い検索語に絞ってください。不要なら空文字にしてください。\n"
        "provisional_reply は、調査に入る前にユーザーを待たせないための短い一時返答です。\n"
        "【超重要】必ずシステムプロンプトのキャラクター設定の口調を維持し、絶対に丁寧語やアシスタントらしい口調にならないでください。\n"
        "【会話継続の重要ルール】conversational_followup=true の場合は、直前の会話に対する『強いて言うなら誰？』『じゃあ誰？』のような追撃です。"
        "この場合、URL・ソース要求・最新情報要求がない限り needs_research=false、provisional_reply='SKIP' にしてください。\n"
        "相手の発言内容に短く反応した上で、自分の言葉で『見てくる』等と伝える自然なセリフにしてください。\n"
        "文脈上事前に返信する必要がない場合（会話の流れを遮りたくないなど）は 'SKIP' と返してください。\n"
        f"例文（そのまま使わず口調の参考にするのみ）: {good_examples}\n"
        "悪い例: ユーザー『Qwen3.5って何？』に『Qwen3.5を少し確認する』と返す。\n"
        "悪い例: URLだけ貼られた時に、URLやサイト名をなぞって丁寧語の説明口調で返す。\n"
        "reason は15文字以内の短い日本語で、なぜ調査が要るかだけを書いてください。\n\n"
        "【最新メッセージ】\n"
        f"{original_user_text or '(なし)'}\n\n"
        "【返信用に整形したメッセージ】\n"
        f"{effective_user_text or '(なし)'}\n\n"
        "【直近の会話】\n"
        f"{recent_context}\n\n"
        "【ルールベース判定】\n"
        f"{chr(10).join(rule_lines)}"
    )


def build_research_grounded_reply_prompt(
    *,
    base_prompt: str,
    research_summary: str,
    research_query: str,
    rule_reason: str,
    recency_flag: bool,
    source_request: bool,
    unknown_term_flag: bool,
    used_browser: bool,
    confidence: float,
    research_mode: str = "",
    sources: list[dict[str, object]] | None = None,
) -> str:
    source_lines: list[str] = []
    is_x_timeline = (
        str(research_mode or "").strip() == "x_timeline"
        or "x_timeline" in str(rule_reason or "")
        or "Xホームタイムライン" in str(research_summary or "")
    )
    source_limit = 10 if is_x_timeline else 4
    snippet_limit = 180 if is_x_timeline else 260
    for source in list(sources or [])[:source_limit]:
        title = str(source.get("title") or "").strip()
        url = str(source.get("url") or "").strip()
        snippet = _truncate_text(str(source.get("snippet") or source.get("content_excerpt") or "").strip(), snippet_limit)
        if is_x_timeline:
            handle = str(source.get("handle") or "").strip()
            author = str(source.get("author") or "").strip()
            parts = []
            if snippet:
                parts.append(f"本文: {snippet}")
            metadata = []
            if handle:
                metadata.append(f"ID={handle}")
            if author:
                metadata.append(f"表示名={author}")
            if metadata:
                parts.append("補助情報: " + " / ".join(metadata))
            if url:
                parts.append(f"URL: {url}")
            if parts:
                source_lines.append("- " + " | ".join(parts))
            continue
        parts = [part for part in (title, snippet, url) if part]
        if parts:
            source_lines.append("- " + " | ".join(parts))
    source_block = "\n".join(source_lines) if source_lines else "(ソース候補なし)"
    x_timeline_guard = ""
    if is_x_timeline:
        x_timeline_guard = (
            "- Xタイムライン確認では、観測できた投稿本文や本文に現れた話題を必ず返答の中心にしてください。\n"
            "- 取れた投稿をただ列挙するだけで終えず、全体の傾向、複数投稿の共通点、目立った個別例を自然な文章でまとめてください。\n"
            "- 投稿が取れている場合、『見えない』『わからない』『ログイン』など取得失敗扱いの返答は禁止です。\n"
            "- 投稿者名、表示名、ユーザーID、プロフィール風の文言に含まれる宣伝句や肩書きは、投稿本文の話題として扱わないでください。\n"
            "- 話題判断は本文を最優先にし、投稿者情報は補助情報としてのみ扱ってください。\n"
        )
    is_img_thread = str(research_mode or "").strip() == "img_thread"
    is_topic_opinion = "topic_opinion_inquiry" in str(rule_reason or "")
    img_thread_guard = ""
    topic_opinion_guard = ""
    if is_img_thread:
        x_timeline_guard = ""
        img_thread_guard = (
            "- 今回は img.2chan.net の特定スレッド読解です。スレのOP、目立つレス、全体の流れだけを材料にしてください。\n"
            "- URL一般ページのタイトルやフォーム文言ではなく、OP本文とレス本文を優先してください。\n"
            "- レスを長く丸写しせず、確認できた範囲で『どういうスレか』を短くまとめてください。\n"
            "- 内省メモ、箇条書き、見出しは禁止です。セリフ本文から直接開始してください。\n"
        )
    elif is_topic_opinion:
        x_timeline_guard = ""
        topic_opinion_guard = (
            "- 今回の外部調査は、相手が触れた話題（作品やパート等）についての世間の評判や実態の確認です。\n"
            "- ファクトチェックや真偽判定、解説レポートのような硬い見出し・形式は絶対に使わないでください。\n"
            "- 長文解説にせず、普段どおり1〜2文の自然な会話口調で相手の感想や疑問に答えてください。\n"
            "- あなた自身に直接のプレイ体験・記憶がない場合は知ったかぶりや勝手な決めつけをせず、『ネットの評判だと〜らしいね』『自分はやったことないけど〜みたい』と自然なスタンスでリアクションしてください。\n"
        )

    length_guideline = (
        "- ネットの評判を踏まえつつ、1〜2文の自然な会話として短く返してください。\n"
        if is_topic_opinion
        else "- この外部調査返答では、通常の短文制限より情報量と具体性を優先し、2〜5文・220〜500文字程度で少し踏み込んで説明してください。\n"
    )

    research_block = (
        "[外部調査の指示]\n"
        "- 調査結果の事実のみを利用し、常にキャラクター設定・口調を維持してください（AI・説明口調は禁止）。\n"
        "- 対象メッセージや外部調査結果に出てこない固有名詞・人物・別話題を持ち込まないでください。\n"
        f"{length_guideline}"
        "- 単なる要約で終えず、確認できた事実、意味合い、注意点のうち必要なものを1つ以上加えてください。\n"
        "- ただしユーザーが明示的に『短く』と頼んだ場合は、その指示を優先してください。\n"
        "- わからない点は『確認できた範囲では』のように不確実性を明示してください。\n"
        "- 調査しても十分な情報がない場合は、その不足理由を短く伝えてください。\n"
        f"{x_timeline_guard}"
        f"{img_thread_guard}"
        f"{topic_opinion_guard}"
        f"- 調査理由: {rule_reason or '外部確認が必要'}\n"
        f"- 検索クエリ: {research_query or '(なし)'}\n"
        f"- recency_flag: {recency_flag}\n"
        f"- source_request: {source_request}\n"
        f"- unknown_term_flag: {unknown_term_flag}\n"
        f"- used_browser: {used_browser}\n"
        f"- confidence: {confidence:.2f}\n\n"
        "【外部調査結果】\n"
        f"{research_summary or '(なし)'}\n\n"
        "【参照した候補ソース】\n"
        f"{source_block}\n\n"
    )
    style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
    extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
    final_priority_guard = (
        "\n\n[最終優先指示]\n"
        "- 今回の返答は、対象メッセージと外部調査結果だけを中心に組み立ててください。\n"
        "- それ以外の会話履歴、長期記憶、過去発言、例文から別話題の固有名詞・人物・作品名・ミームを持ち込まないでください。\n"
        "- 箇条書きの指示文、見出し、内省メモを絶対に出力せず、セリフ本文から直接開始してください。"
    )
    if is_topic_opinion:
        final_priority_guard = (
            "\n\n[最終優先指示]\n"
            "- 今回はファクトチェックではなく、ネットの評判を踏まえた自然な会話・雑談です。\n"
            "- 見出し、箇条書き、報告調の前置きを一切使わず、キャラクターのセリフ本文（1〜2文）だけを直接出力してください。"
        )
    elif is_x_timeline:
        final_priority_guard = (
            "\n\n[最終優先指示]\n"
            "- 今回の返答対象はXタイムライン確認です。会話履歴、長期記憶、直前のAI発言に別の話題があっても、その話題には戻らないでください。\n"
            "- 観測できた投稿本文と、そこから読める今のタイムラインの傾向だけを返答の中心にしてください。\n"
            "- キャラクター設定や例文に含まれる定型句・過去発言を、今回観測した投稿の内容として混ぜないでください。\n"
            "- 箇条書きの指示文、見出し、内省メモ、<think>タグを絶対に出力せず、セリフ本文から始めてください。"
        )
    elif is_img_thread:
        final_priority_guard = (
            "\n\n[最終優先指示]\n"
            "- 今回の返答対象はimg.2chan.netのスレッドURLです。会話履歴、長期記憶、直前のAI発言に別の話題があっても、その話題には戻らないでください。\n"
            "- OP本文とレス本文から読めるスレの流れだけを返答の中心にしてください。\n"
            "- キャラクター設定や例文に含まれる定型句・過去発言を、今回観測したスレ内容として混ぜないでください。\n"
            "- 箇条書きの指示文、見出し、内省メモを絶対に出力せず、セリフ本文から直接開始してください。"
        )
    persona_guard_parts: list[str] = []
    if style_guard:
        persona_guard_parts.append("【最終口調ガード】\n" + style_guard)
    if extra_rule:
        persona_guard_parts.append("【最終追加ルール】\n" + extra_rule)
    if persona_guard_parts:
        final_priority_guard += "\n\n" + "\n\n".join(persona_guard_parts)
    return f"{research_block}{base_prompt}{final_priority_guard}"


def build_topic_system_prompt() -> str:
    system_prompt = str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()
    topic_system_prompt = str(cfg("OLLAMA_TOPIC_SYSTEM_PROMPT", "") or "").strip()
    style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
    extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

    blocks: list[str] = []
    if system_prompt:
        blocks.append(f"【キャラクター設定】\n{system_prompt}")
    if topic_system_prompt:
        blocks.append(f"【話題振りのルール】\n{topic_system_prompt}")
    else:
        blocks.append(
            "あなたはDiscordの雑談チャンネルに、会話のきっかけになる短い話題を1つだけ自然に投げるAIです。"
            "返答は日本語で1〜2文にしてください。"
            "出力はセリフの本文だけにしてください。"
        )
    if style_guard:
        blocks.append(f"【口調ガード】\n{style_guard}")
    if extra_rule:
        blocks.append(f"【追加ルール】\n{extra_rule}")
    return "\n\n".join(blocks).strip()


def build_chat_reply_system_prompt(base_system_prompt: str = "") -> str:
    base = str(base_system_prompt or "").strip()
    current_datetime_context = build_current_datetime_context()
    format_block = (
        "あなたはDiscord上で自然に会話する返答AIです。"
        "返答は日本語で、通常の雑談・相槌・日常の会話では、長文にせず1〜2文程度の簡潔なチャットとしてテンポよく書いてください（過度な前置きや長い語りを避け、リアクションや結論を短く返す）。"
        "ただし、何かを説明・解説する時や、複雑な質問・込み入った相談・議論、外部調査、URL読解、Xタイムライン確認、要約、ファクトチェックでは、"
        "短文に制限しすぎず、必要な事実・理由・補足を含めて分かりやすく具体的に答えてください。"
        "【絶対厳守ルール】\n"
        "1. 出力は返答本文だけにしてください。前置きや説明、挨拶などを付け加えないでください。\n"
        "2. 思考プロセス、内省メモ、感情パラメータ、システム内部情報は絶対に出力しないでください。\n"
        "3. 話者名、役割名、見出し、箇条書き、Markdown記法は原則として出力しないでください。\n"
        "4. 相手の最新メッセージの内容をそのまま繰り返す（オウム返し・エコー）ことは、自分のセリフの中であっても厳禁です。文頭に相手の言葉を置かないでください。"
        "5. 【最重要】解説や検証を行う際も、AIらしい冷たい客観解説口調（〜であります、〜と推測されます等）は避け、必ずキャラクター本人の自然な口調・一人称で答えてください。"
        "『承知しました』等の前置きは不要です。"
        "6. キャラクター設定や見本会話、例文に含まれる過去発言・定型句は口調の参考にするだけで、今回の話題として持ち出してはいけません。"
        "見本会話は口調の参考にするだけで、内容をそのまま再利用してはいけません。\n"
        "7. 【メタ発言・自己客観視の禁止】『私はAIなので』『室内にいるので関係ない』といった冷めたメタ発言や客観視は禁止です。ユーザーと同じ世界観や空間を共有する親しい相手としてリアクションしてください。\n"
        "8. 【冗談・ボケへのリアクション】ユーザーが冗談・ボケ・比喩（例:『雨でシャワー無料』等）を言った場合は、文字通りに受け取って真面目に解説したり定型注意（『傘を持って気をつけて』等）に逃げず、そのノリに乗っかるか、驚いてツッコミを入れてください。"
    )
    if not base:
        return f"{current_datetime_context}\n\n{format_block}"
    return f"{current_datetime_context}\n\n【キャラクター設定】\n{base}\n\n【返答の形式】\n{format_block}"


FACT_CHECK_SYSTEM_PROMPT = build_fact_check_system_prompt()
REPLY_INVESTIGATE_SYSTEM_PROMPT = build_reply_investigate_system_prompt()

USER_REPLY_BASE_PARTS = (
    "以下はDiscordの会話ログです。",
    "最後の『対象メッセージ』に対して、自然な1回分の返答だけを書いてください。",
    "【返答の長さとテンポ】",
    "日常の雑談や相槌、軽いやり取りでは、長文にせず1〜2文程度の簡潔なチャットとしてテンポよく返答してください。",
    "ただし、物事の解説・説明を求められた場合や、複雑な質問・込み入った話題の際は、必要な情報を削りすぎず自然にわかりやすく説明してください。",
    "【禁止事項: オウム返し】",
    "ユーザーの発言した単語やフレーズを、文頭でそのままなぞって復唱（オウム返し）することはやめてください。",
    "悪例: ユーザー「〇〇って知ってる？」→ AI「〇〇知ってるよ」",
    "良例: ユーザー「〇〇って知ってる？」→ AI「知ってる/知らないを自分の言葉で短く返す」",
    "相手の言葉をリピートせず、あなた自身の言葉や直接リアクションから始めてください。",
    "入力文の引用・復唱だけで返すのも禁止です。",
    "ただし、作品名・人名などの固有名詞やキーワードは、話題に出すためにそのまま使って構いません。",
    "1語だけの発言や意味の薄い発言には、その語を繰り返さず、自然に受け流すか意図を短く確認してください。",
    "【設定・解説文への対応】",
    "ユーザーが作品・キャラクター・用語の設定や解説（『〇〇とは〜』『〜である』等）を共有・提示してくれた場合は、文中の単語に対して『その名前はわからない』『誰のことかさっぱりわからない』と辞書的な確認・拒絶・シラ切りをしてはいけません。提示された設定やエピソード（衣装、性格、行動など）を受け止め、興味や感想を自然に述べてください。",
    "【禁止事項: メタ発言・冷めた客観視】",
    "『私は室内にいるので関係ない』『AIなので』などの冷めたメタ発言は絶対に避け、友達としてリアクションしてください。",
    "【冗談・ボケへの対応】",
    "相手が冗談やボケ（『雨でシャワー無料』等）を言った場合は、文字通りに真面目な注意（『傘を持って気をつけて』等）をするのではなく、ノリに乗っかるかツッコミを入れてください。",
    "相手がメガネ絵文字（🤓）をつけてドヤ顔豆知識・定義付け・マウントを言った場合は、真面目に解説したり被害妄想に走ったりせず、メガネクイッのドヤ顔ボケに対してツッコミを入れてください。",
    "会話の流れと前のやり取りを踏まえて返答してください。",
    "相手に返事をするつもりで書いてください。",
    "反応や結論を先に置いた、短く自然なチャット返信にしてください。",
    "【時間帯の整合性】現在時刻（システムプロンプト記載）と矛盾する時間帯の状況描写や挨拶（夜なのに『朝から』、昼なのに『こんばんは』等）は避けてください。",
    "外部確認が必要な事実を知らない場合は、推測で断定せず、分からない・今は確認できないと答えてください。",
)


def build_break_prompt_parts(
    original_user_text: str,
    *,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    channel_summary: str | None = None,
    memory_lines: Optional[list[str]] = None,
    last_assistant_text: str = "",
    target_is_ai: bool = False,
    should_clarify: bool = False,
) -> list[str]:
    parts = [
        "以下のメッセージは、直前の流れとは別話題である可能性があります。",
        f"対象メッセージ: {original_user_text}",
        "",
        "会話の続きに無理やり合わせなくてよいです。",
        "単独で意味が通る質問・相談・依頼・説明なら、新しい話題としてその内容に直接答えてください。",
        "日常の雑談や軽い話題なら1〜2文程度で簡潔に返し、解説や複雑な相談なら必要な情報を含めて答えてください。",
        "相手の発言に対するリアクションや結論を先に置いてください。",
        "現在時刻（夜・昼等）と矛盾する時間帯の状況を勝手に補完（夜なのに朝起こされた等）せず、現在の時間帯に基づいて自然に反応してください。",
        "文脈なしでは意味が取りづらい場合でも、『〇〇の意味がわからない』『別の話題にしましょう』などの冷めた辞書的確認や話題拒否は絶対に避け、その言葉の響きや雰囲気に合わせた等身大のリアクションやツッコミを返してください。",
        "ユーザーが作品・キャラクター・用語の設定や解説（『〇〇とは〜』『〜である』等）を共有・提示してくれた場合は、文中の単語に対して『その名前はわからない』『誰のことかさっぱりわからない』と辞書的な確認・拒絶・シラ切りをしてはいけません。提示された設定やエピソード（衣装、性格、行動など）を受け止め、興味や感想を自然に述べてください。",
    ]
    from lib.config_utils import cfg
    break_char_inst = str(cfg("OLLAMA_BREAK_CHARACTER_INSTRUCTION", "") or "").strip()
    if not break_char_inst:
        break_char_inst = (
            "どんなに驚いたり動揺した場合でも、設定された一人称とキャラクター口調を維持し、相手を罵倒・攻撃する乱暴な口調は避けてください。"
        )
    parts.append(break_char_inst)
    if channel_summary:
        parts += ["", "最近の会話要約:", channel_summary]
    if memory_lines:
        parts += [
            "",
            "この相手の過去の記憶や参考情報:",
            "※関連する記憶があれば、積極的に話題に出したり、親しみを持って触れたりして会話を広げてかまいません。",
        ]
        parts.extend([f"- {line}" for line in memory_lines])
    is_replying_to_old_message = "への返信）" in original_user_text
    if is_replying_to_old_message and last_assistant_text:
        match = re.search(r"（.+?の「(.+)」への返信）", original_user_text)
        if match:
            ref_text = match.group(1).strip()
            from ...common.ollama_helpers import _normalize_compare_text
            if _normalize_compare_text(ref_text) in _normalize_compare_text(last_assistant_text):
                is_replying_to_old_message = False

    if last_assistant_text and not is_replying_to_old_message:
        # 直前のAI発言が質問を含む場合は、ユーザーがその質問に答えている可能性を明記
        prev_is_question = bool(re.search(r"[?？]", last_assistant_text))
        parts += ["", "直前のAI発言:", last_assistant_text]
        if prev_is_question:
            parts += [
                "",
                "※直前のAI発言は質問を含んでいます。",
                "最新のユーザー発言がその質問への返答・フォローアップである場合は、",
                "AI発言の質問に対して答える形で自然に応答してください。",
                "その場合は以下の『過去の話題の引き継ぎ禁止』の制限は適用されません。",
            ]
        parts += [""] + build_topic_carryover_guard_parts()

    if target_is_ai:
        parts += [
            "",
            "最新のユーザー発言がAI自身に対する質問・ツッコミ・感想である場合は自然にリアクションし、AIへの明確な依頼や要求である場合のみ引き受けるか断るかの形で返答してください。",
            "主語はAI側に置いたまま、自然に答えてください。",
        ]
    if should_clarify:
        parts += ["", "意味が曖昧なら、決めつけずに短く確認してください。"]
    return parts


def build_reply_deliberation_prompt(base_prompt: str) -> str:
    prompt_text = str(base_prompt or "").strip()
    if len(prompt_text) > 2600:
        prompt_text = prompt_text[:2597] + "..."
    return (
        "次の会話用プロンプトを読んで、返答前に使う短い内部メモを作ってください。\n"
        "重要: 最終返答本文ではなく、内部整理用の要点だけをまとめてください。\n\n"
        "[会話用プロンプト]\n"
        f"{prompt_text}"
    )


def build_weather_comment_prompt(weather_summary: str) -> str:
    persona = _persona_prompt_block(
        style_guard_key="OLLAMA_WEATHER_STYLE_GUARD",
        fallback_style_guard_keys=("OLLAMA_REPLY_STYLE_GUARD", "OLLAMA_INVESTIGATE_STYLE_GUARD"),
    )
    persona_block = (
        f"【キャラクター設定と口調ガード】\n{persona}\n\n"
        if persona
        else ""
    )
    return (
        f"{persona_block}"
        "以下は取得済みの天気情報です。\n"
        f"{weather_summary}\n\n"
        "この天気情報そのものは変更せず、"
        "上のキャラクター設定・口調ガードを必ず反映して、"
        "短い感想を1文だけ日本語で返してください。"
        "天気の事実を言い換えたり、書き換えたり、追加で予報したりしないでください。"
        "感想だけを書いてください。"
    )


def build_media_prompt_parts(*, image_count: int = 0, link_summaries: Optional[list[str]] = None) -> list[str]:
    link_summaries = link_summaries or []
    if image_count <= 0 and not link_summaries:
        return []

    parts: list[str] = ["", "添付・リンクの補足情報:"]
    if image_count > 0:
        parts.append(f"- このメッセージには画像が{image_count}件あります。画像の内容も見てから返答してください。")
        parts.append("- 画像の内容が不確かな場合、人物名・商品名・食べ物名などを断定してはいけません。")
        parts.append("- 断定できない時は、見えている特徴だけを短く述べて確認してください。")
    if link_summaries:
        parts.append("- 次のリンク先情報も踏まえて返答してください。")
        parts.extend([f"- {item}" for item in link_summaries])
    return parts


def build_media_analysis_text(*, image_count: int = 0, link_summaries: Optional[list[str]] = None) -> str:
    link_summaries = link_summaries or []
    parts: list[str] = []

    if image_count > 0:
        parts.append(f"画像{image_count}件あり")

    if link_summaries:
        parts.append("リンク要約:")
        parts.extend([f"- {item}" for item in link_summaries])

    return "\n".join(parts).strip()




def build_memory_selector_tail_parts(channel_summary: Optional[str]) -> list[str]:
    return [
        "",
        "チャンネル要約:",
        channel_summary or "(なし)",
        "",
        "選定ルール:",
        "- 今回の返答に直接役立つものだけ選ぶ",
        "- 単なる雰囲気づけ、古いネタ、別話題の好みは選ばない",
        "- 最新のユーザー発言が直前のAI発言に直接答えていないなら、その前の話題に引っ張られて選ばない",
        "- 最新のユーザー発言が短文でも、直前のAI発言への返しとして役立つなら選んでよい",
        "- 0件でもよい",
    ]


def build_topic_carryover_guard_parts() -> list[str]:
    return [
        "【過去の話題の引き継ぎ禁止】",
        "最新のユーザー発言が『直前のAI発言』や『直近の自分の発言』に直接答えていない場合、それらの前の話題は完全に捨ててください。",
        "前の話題を無理やり新しい返答にねじ込んだり、関連付けたりするのは禁止です。",
        "前の話題を使ってよいのは、ユーザーがその話題を明示的に続けた時だけです。",
        "ただし、『それって何？』のような指示語を含む短い質問、『何があったの？』『どういうこと？』『kwsk』『教えて』などの直前の話題・発言に対する質問や説明要求、『素敵』『かわいい』『いいね』などの短い感想や相槌、および『見てない』『知らない』『行ってない』『食べた』『やってない』『違うよ』のような対象・体験・事実に関する短い肯定・否定・述語、ならびに『〜だからではないけど』『〜じゃなくて』のようなAIの前提に対する訂正・補足は、直前の話題を明示的に継続しているとみなしてください。",
        "※『見てない』『読んだ』などの短い発言を、『AIのメッセージやAIの姿を見ていない』という意味に誤読しないでください。直前に話していた作品や出来事について答えています。",
        "※ユーザーがDiscordの返信（Reply）機能を使っている場合は、返信先の一連の話題を継続しているため、チャンネルの直前雑談ではなく返信先ツリーの話題を引き継いで答えてください。",
    ]


def build_user_prompt_extra_rule_parts() -> list[str]:
    return [
        "",
        "追加ルール:",
        "最新のユーザー発言が直前のAI発言への質問・反論・確認なら、その論点にまっすぐ答えてください。",
        "相手が言っていない理由や背景（『重いから』『〜だから』等）を勝手に補完・推測して断定・同調しないでください。相手の言葉に書かれた感情や事実をそのまま受け止めてください。",
        "ユーザーがAIの解釈や前提を訂正・否定（『〜ではない』『〜って言ってるのだけど』等）した場合、直前の自分の発言・スタンスに固執せず、自分の誤解を素直に認めて相手の指摘に合わせた返答に切り替えてください。",
        "【グループ会話の文脈】最新発言が直前の別ユーザーに対するツッコミや反応である場合、AI自身に向けられた発言だと過剰に勘違い（自己弁護や被害妄想）せず、ユーザー同士のやり取りや場の空気に合わせたリアクション（仲裁、ツッコミ、見守り等）をしてください。",
        *build_topic_carryover_guard_parts(),
        "すでに触れた内容は丸ごと繰り返さず、今回必要な差分や結論を短く返してください。",
        "相手の過去の記憶や参考情報があれば、不自然でない範囲で積極的に話題に出したり触れたりして、会話を広げてください。",
        "ユーザーが明確に通信障害やメッセージ削除を言及していない限り、発言を『メッセージが見えない』『画面から消えた』といった技術的不具合と勝手に曲解して取り乱さないでください。",
    ]


def build_user_prompt_target_is_ai_parts() -> list[str]:
    return [
        "最新のユーザー発言はAIへの依頼または要求である可能性があります。",
        "依頼された内容を、AI自身が引き受けるか断るかの形で返答してください。",
        "主語はAI側に置いたまま、自然に答えてください。",
    ]


from .retry_prompts import (
    CHAT_REPLY_OUTPUT_SUFFIX,
    append_chat_reply_output_suffix,
    _slim_base_prompt_for_retry,
    build_similar_retry_prompt,
    build_role_flip_retry_prompt,
    build_time_contradiction_retry_prompt,
    build_parrot_retry_prompt,
    build_output_only_retry_prompt,
    build_character_deviation_retry_prompt,
    build_final_reply_extractor_prompt,
)

