"""思考タグ（think/thought）の除去および未完思考ブロックからのセリフ救出処理。"""
from __future__ import annotations

import difflib
import re

from .cleaner import _normalize_compare_text
from .control import (
    _looks_like_control_line,
    _looks_like_conversational_reply_excerpt,
    _looks_like_fragmentary_meta_summary,
    _looks_like_meta_reply_preface,
    _looks_like_structured_analysis_block,
    _looks_like_style_tag_outline_before_reply,
    _strip_leading_control_prefixes,
)
from .patterns import looks_like_prompt_leak, looks_like_reasoning_leak
from .validation import (
    looks_like_abnormal_assistant_reply,
    looks_like_truncated_reply,
    looks_like_unusable_assistant_reply,
)


def _clean_salvage_candidate(candidate: str) -> str:
    """思考・検証メモやクォートの外側ゴミを除去し、セリフ本体を綺麗に抽出する。"""
    cand = str(candidate or "").strip()
    if not cand:
        return ""

    # 1. 思考・内省・検証キーワードで後続部分を切断
    cand = re.split(
        r"\s*\(?(?:Note|Wait|Actually|Check|Let's|Self-Correction|Check constraint|Final check|Matches(?:\s+exactly)?|Match|Output matches|Proceeds|Verified|Passed)\b",
        cand,
        flags=re.IGNORECASE,
    )[0].strip()

    # 2. 文頭からクォートで囲まれており、閉じクォートの後が空または英語メモ・記号のみである場合、クォート内部を抽出
    # 例1: 「セリフ本文」 -> セリフ本文
    # 例2: 「セリフ本文」 (Matches exactly) -> セリフ本文
    # 例3: "セリフ本文" -> セリフ本文
    quote_open_close = [
        ("「", "」"),
        ("『", "』"),
        ('"', '"'),
        ("“", "”"),
    ]
    for q_open, q_close in quote_open_close:
        cand_stripped = cand.strip()
        if cand_stripped.startswith(q_open):
            close_idx = cand_stripped.rfind(q_close)
            if close_idx != -1 and close_idx > len(q_open):
                after_close = cand_stripped[close_idx + len(q_close):].strip()
                if not after_close or re.fullmatch(r"\(?[A-Za-z\s_-]{2,40}\)?\.?|['\"]+", after_close):
                    inside = cand_stripped[len(q_open):close_idx].strip()
                    if len(inside) >= 4:
                        cand = inside
                        break

    # 3. 末尾に残った英語メモ（(Matches exactly), (Verified), (Done) 等）の除去
    cand = re.sub(
        r"\s*\(?(?:Matches(?:\s+exactly)?|Verified|Passed|Proceeds|Output matches)\)?\.?$",
        "",
        cand,
        flags=re.IGNORECASE,
    ).strip()
    cand = re.sub(r"\s*\([A-Za-z\s_-]{2,40}\)\.?$", "", cand).strip()
    cand = re.sub(r"['\"]+$", "", cand).strip()

    # 4. 先頭のメタ記号・プレフィックス除去
    cand = _strip_leading_control_prefixes(cand).strip()
    return cand


def salvage_reply_from_think_block(raw_text: str) -> str:
    """思考タグが閉じないまま切断されたテキスト等から、思考ブロック内の完成したセリフ下書きを救出する。"""
    t = str(raw_text or "").strip()
    if not t:
        return ""

    # 1. 明示的な出力マーカー（[Output], Output:, Final:, Draft:, [Response] 等）からの抽出
    marker_patterns = [
        r"(?:\[(?:Output|Output Generation|Final Output|Response)\](?:\s*\(.*?\))?\s*(?:->|[:：])?|(?:^|\n)\s*Output\s*[:：]|Final\s*[:：]|Draft\s*[:：])\s*([^\n]+(?:\n[^\n]+)?)",
        r"(?:出力セリフ|最終出力|返答案|返答セリフ|本文)\s*[:：]\s*([^\n]+(?:\n[^\n]+)?)",
    ]
    for pattern in marker_patterns:
        matches = list(re.finditer(pattern, t, flags=re.IGNORECASE))
        if matches:
            candidate = _clean_salvage_candidate(matches[-1].group(1))
            if (
                candidate
                and len(candidate) >= 4
                and re.search(r"[ぁ-んァ-ヶ一-龠]", candidate)
                and not looks_like_prompt_leak(candidate)
                and not looks_like_reasoning_leak(candidate)
                and not looks_like_truncated_reply(candidate)
                and not looks_like_unusable_assistant_reply(candidate)
                and not looks_like_abnormal_assistant_reply(candidate)
            ):
                return candidate

    # 2. 思考テキストを行ごとに後ろから探索し、日本語の完成したセリフ行を救出
    lines = [line.strip() for line in t.splitlines() if line.strip()]
    for line in reversed(lines):
        # 記号・英数字のみの行は除外
        if re.search(r"^[A-Za-z\s0-9_\-.,'\"!?():;*#/]+$", line):
            continue
        # 英語の推論/分析/構造化行（箇条書きを含む）を除外
        if re.search(
            r"^(?:[-*+•]|\d+[\.)])\s*(?:\*\*)?[A-Za-z\s0-9_\-.,'\"!?():;*#/]+(?:\*\*)?[:：]",
            line,
            flags=re.IGNORECASE,
        ):
            continue
        if re.search(
            r"^(?:[-*+•]|\d+[\.)])\s*(?:\*\*)?(?:Analyze|Deconstruct|Persona|Constraints?|Task|Core\s+Task|Maintain|Tone|Format|Rule|Rules|Key Rules|Prompt|Avoid|Avoids|Use|Uses|Language|Direct|Output|Greeting|Context|Personality|Suffix|Suffixes|Draft|Polish|Step|Refinement|Recent\s+Tweets)\b",
            line,
            flags=re.IGNORECASE,
        ):
            continue
        if re.search(
            r"^(?:Here's|Let's|Wait|Actually|I'll|The prompt|Check|Constraints?|Drafting|Final|Done|Proceeds|Ready|Self-Correction|Refinement|Output matches|Note[:：]|Checks[:：]|Final check|Analyze|Deconstruct|Persona|Maintain|Task|Core\s+Task|Rule|Key Rules|Greeting|Language|Direct output|Tone|Suffixes?|Avoids?|Uses?|Recent\s+Tweets)\b",
            line,
            flags=re.IGNORECASE,
        ):
            continue
        # モデルがプロンプト制約をチェックした行（例: "..." -> Done. や "..." -> I didn't.）はセリフではないのでスキップ
        if re.search(r"->\s*(?:Done|Check|OK|Passed|Match|Matches|True|False|I didn't|Not applicable|Checked|No|Yes|Correct|Valid)", line, flags=re.IGNORECASE):
            continue
        # 行内の英字比率が高い場合（英単語が複数ある・英字が日本語より多い推論行）はスキップ
        ascii_chars = len(re.findall(r"[A-Za-z]", line))
        jp_chars = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", line))
        if ascii_chars >= 8:
            if jp_chars >= 20 and jp_chars >= ascii_chars * 2:
                pass
            elif ascii_chars >= jp_chars or len(re.findall(r"[A-Za-z]{3,}", line)) >= 2:
                continue

        if _looks_like_control_line(line) or looks_like_prompt_leak(line) or looks_like_reasoning_leak(line):
            continue

        candidate = _clean_salvage_candidate(line)
        if not candidate:
            continue

        # 抽出テキスト内にダブルクォートの残骸が含まれている場合は不正抽出として破棄
        if any(q in candidate for q in ('"', '”', '“')):
            continue
        # 鉤括弧の数が不一致（未閉じ）の場合は破棄
        if candidate.count('「') != candidate.count('」') or candidate.count('『') != candidate.count('』'):
            continue
        # 抽出テキスト内に英語のキーワード・指示構文が含まれる場合は破棄
        if re.search(r"\b(?:avoids?|uses?|personality|slightly|nerdy|enthusiastic|constraints?|suffix|dialogue|sentence|rules?|task|recent\s+tweets)\b", candidate, flags=re.IGNORECASE):
            continue

        # プロンプトの命令調文末（〜出せ、〜しろ、〜しないでください等）が含まれる場合はプロンプト指示文なので破棄
        if re.search(r"(?:出せ|出せよ|しろ|すること|禁止|扱うこと|踏まえて|材料にして|選んで|作成してください|しないでください|しないで|しないでね|避けてください|答えてください|返してください|出力してください|使ってください|保ってください|認めてください|捨ててください|広げてください|断定・同調しないでください)[。！!…\s]*$", candidate):
            continue

        if (
            len(candidate) >= 8
            and re.search(r"[ぁ-んァ-ヶ一-龠]", candidate)
            and not looks_like_prompt_leak(candidate)
            and not looks_like_reasoning_leak(candidate)
            and not looks_like_truncated_reply(candidate)
            and not looks_like_unusable_assistant_reply(candidate)
            and not looks_like_abnormal_assistant_reply(candidate)
        ):
            return candidate

    return ""


def _strip_think_blocks(text: str) -> str:
    """思考タグ（<think>...</think>, <thought>...</thought>）を除去し、必要に応じてセリフ救出を行う。"""
    t = str(text or "")
    if not t:
        return ""

    # 1. 終了タグ </think> または </thought> が存在する場合:
    close_matches = list(re.finditer(r"</(?:think|thought)\b[^>]*>", t, flags=re.IGNORECASE))
    if close_matches:
        last_close = close_matches[-1]
        after_close = t[last_close.end():].strip()
        # 終了タグの後にさらに未完の <think> が開かれている場合はそれ以降を除去
        after_close = re.sub(r"<(?:think|thought)\b[^>]*>.*\Z", "", after_close, flags=re.IGNORECASE | re.DOTALL).strip()
        
        # 終了タグ以降にテキストが存在する場合:
        # 思考モデルの仕様上、終了タグより前にあるテキストはすべて思考プロセス・内省メモ・プロンプト反芻であるため、
        # 終了タグより前のテキストはすべて切り捨て、終了タグ以降の本文のみを採用します。
        if after_close:
            return after_close

        # 終了タグ以降が空の場合（モデルが本文を出力した後に末尾で思考タグを閉じた場合など）:
        first_open = re.search(r"<(?:think|thought)\b[^>]*>", t, flags=re.IGNORECASE)
        if first_open:
            before = t[:first_open.start()].strip()
            if before and not _looks_like_control_line(before) and not looks_like_prompt_leak(before):
                return before
        before_close = t[:last_close.start()].strip()
        if before_close and not _looks_like_control_line(before_close) and not looks_like_prompt_leak(before_close):
            return before_close
        return salvage_reply_from_think_block(t)

    # 2. 終了タグはないが、開始タグ <think> または <thought> が含まれている場合:
    # 開始タグより前にテキストがあればそれを残し、開始タグ以降の未完思考ブロックからはサルベージを試みる
    first_open = re.search(r"<(?:think|thought)\b[^>]*>", t, flags=re.IGNORECASE)
    if first_open:
        before = t[:first_open.start()].strip()
        if before and not _looks_like_control_line(before) and not looks_like_prompt_leak(before):
            return before
        return salvage_reply_from_think_block(t)

    # 3. think/thought タグが一切ない場合:
    # 通常の生成テキストとしてそのまま返却します。
    return t


def _looks_like_duplicate_think_echo(before: str, after: str) -> bool:
    before_norm = _normalize_compare_text(before)
    after_norm = _normalize_compare_text(after)
    if not before_norm or not after_norm:
        return False
    if before_norm == after_norm:
        return True
    shorter, longer = sorted((before_norm, after_norm), key=len)
    if len(shorter) >= 8 and shorter in longer and len(longer) <= int(len(shorter) * 1.35):
        return True
    if min(len(before_norm), len(after_norm)) < 8:
        return False
    return difflib.SequenceMatcher(None, before_norm, after_norm).ratio() >= 0.92


def _looks_like_pre_think_leak(before: str, after: str) -> bool:
    before_text = str(before or "").strip()
    after_text = str(after or "").strip()
    if not before_text or not after_text:
        return False

    before_lines = [line.strip() for line in before_text.splitlines() if line.strip()]
    if _looks_like_style_tag_outline_before_reply(before_text, after_text):
        return True
    if _looks_like_meta_reply_preface(before_text) and _looks_like_conversational_reply_excerpt(after_text):
        return True
    if _looks_like_structured_analysis_block(before_text):
        return True
    if looks_like_prompt_leak(before_text) or any(_looks_like_control_line(line) for line in before_lines):
        return True

    if re.search(r"(丁寧語|敬語|口調|文体|箇条書き|Markdown|内省|思考|推論|ルール|指示|制約|遵守|厳守|絶対)", before_text, flags=re.IGNORECASE):
        return True

    # 文章が途中で切れているような開始（「して〜」「ので〜」など）や、
    # 助詞や記号のみの短い行が先行している場合は、プロンプトの残骸の可能性が高い
    stripped_before = before_text.strip()
    if len(stripped_before) > 0 and len(stripped_before) < 100:
        if re.match(r"^(?:こと|ください|ます|です|ません|ましょう|ださい|せる|える|れる|られる|ない)[。！？!?…]*$|^[してなのでからとにをへが]|^[、。！？!?…]+", stripped_before):
            return True
        # 文末記号がなく、かつ1行のみで、後に続く think ブロックとの関連性が薄い場合
        if "\n" not in stripped_before and not re.search(r"[。！？!?…]$", stripped_before):
            return True

    if _looks_like_fragmentary_meta_summary(before_text) and _looks_like_conversational_reply_excerpt(after_text):
        return True

    if len(before_lines) <= 2 and len(before_text) <= 60 and re.search(r"[ぁ-んァ-ヶ一-龠]", after_text):
        if not re.search(r"[。！？!?…]$", before_text):
            return True
        # 短い単独行で、後ろに十分な会話本文がある場合、メタ宣言や受諾（「このルールは絶対です。」「了解しました。」等）ならリークとみなす
        if re.search(r"(?:ルール|指示|絶対|厳守|遵守|了解|承知|方針|プロンプト|制約)", before_text):
            return True

    return False
