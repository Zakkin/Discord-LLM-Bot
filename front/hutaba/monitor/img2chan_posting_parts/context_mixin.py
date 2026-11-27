"""長大スレ序盤/周辺文脈抽出・マクロ話題要約・事前Web調査コンテキスト解決 Mixin。"""
from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
import re
import unicodedata
from typing import Any

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    _Img2chanPostingBase,
    _get_img2chan_helper,
    _normalize_img_post_no,
    cfg,
    log,
    truncate_text,
)
from front.hutaba.monitor.img2chan_posting_parts.grounding import (
    _classify_hutaba_reply_for_research,
    _find_hutaba_consultation_target_reply,
)
from front.hutaba.monitor.img2chan_posting_parts.terms_and_media import (
    _ALPHANUM_TERM_RE,
    _HUTABA_FILENAME_TOKEN_RE,
    _HUTABA_IMAGE_QUESTION_RE,
    _HUTABA_TERM_SYNONYM_MAP,
    _HUTABA_TOPIC_STOPWORDS,
    _KATAKANA_TERM_RE,
    _extract_hutaba_named_terms,
)


class _Img2chanContextMixin(_Img2chanPostingBase):
    def _extract_early_thread_context_lines(
        self,
        posts: list[dict[str, Any]],
        *,
        max_lines: int = 6,
    ) -> list[str]:
        """スレッドが長大化（len(posts) > 8）した際に、序盤〜中盤（posts[:-8]）から
        重要な文脈（OP、疑問、引用、主張・宣言、そうだね獲得レスなど）を最大 max_lines 件抽出して整形する。
        """
        if not posts or len(posts) <= 8 or max_lines <= 0:
            return []

        result_lines: list[str] = []

        # 1. OP（posts[0]）はスレッドの前提であるため必ず先頭に含める
        op_post = posts[0]
        op_text = str(op_post.get("text") or "").strip()
        if op_text:
            op_no = str(op_post.get("post_no") or op_post.get("number") or "").strip()
            op_body = " ".join(op_text.split())
            op_line = f"No.{op_no}: {truncate_text(op_body, 120)}" if op_no else f"OP: {truncate_text(op_body, 120)}"
            result_lines.append(op_line)

        available_slots = max_lines - len(result_lines)
        if available_slots <= 0:
            return result_lines

        # 2. 序盤〜中盤のレス（posts[1:-8]）から候補を抽出
        early_candidates = posts[1:-8]
        valid_candidates: list[tuple[int, dict[str, Any], int]] = []

        for idx, post in enumerate(early_candidates):
            text = str(post.get("text") or "").strip()
            if not text:
                continue

            score = 0
            soudane = int(post.get("soudane") or 0)
            if soudane > 0:
                score += min(soudane, 10) * 2

            # 疑問符・感嘆符
            if any(punct in text for punct in ("?", "？", "!", "！")):
                score += 2

            # 引用行を含む
            if any(line.strip().startswith(">") or line.strip().startswith("＞") for line in text.splitlines()):
                score += 2

            # 主張・宣言・特徴的な表現
            if any(term in text for term in ("思う", "はず", "だろ", "じゃん", "宣言", "結論", "わけ", "本当", "実は")):
                score += 1

            valid_candidates.append((idx, post, score))

        if not valid_candidates:
            return result_lines

        if len(valid_candidates) <= available_slots:
            selected = valid_candidates
        else:
            scored_candidates = [c for c in valid_candidates if c[2] > 0]
            scored_candidates.sort(key=lambda x: x[2], reverse=True)
            chosen_indices: set[int] = {c[0] for c in scored_candidates[:available_slots]}

            if len(chosen_indices) < available_slots:
                needed = available_slots - len(chosen_indices)
                unselected = [c for c in valid_candidates if c[0] not in chosen_indices]
                if unselected:
                    step = len(unselected) / needed
                    sampled = [unselected[int(i * step)] for i in range(needed)]
                    for s in sampled:
                        chosen_indices.add(s[0])

            selected = [c for c in valid_candidates if c[0] in chosen_indices]

        for _, post, _ in selected:
            post_no = str(post.get("post_no") or post.get("number") or "").strip()
            body = " ".join(str(post.get("text") or "").split())
            if not body:
                continue
            line = f"No.{post_no}: {truncate_text(body, 120)}" if post_no else truncate_text(body, 120)
            result_lines.append(line)
            if len(result_lines) >= max_lines:
                break

        return result_lines

    def _extract_hutaba_target_context_lines(
        self,
        thread: dict[str, Any],
        target_reply: dict[str, Any],
        posts: list[dict[str, Any]],
        *,
        max_lines: int = 6,
    ) -> list[str]:
        """対象レスの直前レス、対象レスへの返信（他人のツッコミ）、
        および対象レス内の重要語を含む過去言及レス（ローカルミーム）を抽出する。
        """
        if not target_reply or not posts:
            return []
        target_p_no = _normalize_img_post_no(target_reply.get("post_no"))
        target_r_no = int(target_reply.get("number") or 0)
        target_text = str(target_reply.get("text") or "").strip()
        # 引用されていない対象レス自身の本文先頭行（本文引用判定用）
        target_plain_lines = [
            line.strip() for line in target_text.splitlines()
            if line.strip() and not line.strip().startswith(">")
        ]
        target_first_line = target_plain_lines[0][:24] if target_plain_lines else ""

        context_lines: list[str] = []
        seen_keys: set[str] = {target_p_no} if target_p_no else set()

        # 対象レスのインデックスを特定
        target_idx = -1
        for idx, p in enumerate(posts):
            if _normalize_img_post_no(p.get("post_no")) == target_p_no or (
                target_r_no > 0 and int(p.get("number") or 0) == target_r_no
            ):
                target_idx = idx
                break

        # 1. 対象レスへの返信（子レス / 既についた他人のツッコミ）の抽出
        find_quoted_fn = getattr(_get_img2chan_helper(), "find_img_thread_quoted_posts", None)
        search_replies = posts[target_idx + 1 :] if target_idx >= 0 else posts
        for p in search_replies:
            p_no = _normalize_img_post_no(p.get("post_no"))
            if not p_no or p_no in seen_keys:
                continue
            text = str(p.get("text") or "").strip()
            all_quote_lines = [
                line.strip().lstrip(">").strip()
                for line in text.splitlines()
                if line.strip().startswith(">")
            ]
            direct_quote_lines = [
                line.strip()[1:].strip()
                for line in text.splitlines()
                if line.strip().startswith(">") and not line.strip().startswith(">>")
            ]
            is_replying = False
            if target_p_no and any(q == target_p_no or q == f"No.{target_p_no}" for q in all_quote_lines):
                is_replying = True
            elif target_r_no > 0 and any(q == str(target_r_no) for q in all_quote_lines):
                is_replying = True
            elif len(target_first_line) >= 4 and any(target_first_line in q for q in direct_quote_lines):
                is_replying = True
            elif callable(find_quoted_fn) and not direct_quote_lines:
                with contextlib.suppress(Exception):
                    parents = find_quoted_fn(thread, p, max_targets=1)
                    if parents and _normalize_img_post_no(parents[0].get("post_no")) == target_p_no:
                        is_replying = True

            if is_replying:
                r_num = p.get("number") or 0
                header = (
                    f">>{r_num} No.{p_no} [対象レスへの他人のツッコミ(重複・言い換え合体厳禁)]"
                    if r_num
                    else f"No.{p_no} [対象レスへの他人のツッコミ(重複・言い換え合体厳禁)]"
                )
                context_lines.append(f"{header}: {truncate_text(text, 100)}")
                seen_keys.add(p_no)
                if len(context_lines) >= 2:
                    break

        terms = [t for t in _extract_hutaba_named_terms(target_text) if len(t) >= 2 and t not in ("俺は", "いい")]
        terms_lower = [t.lower() for t in terms]
        target_has_quote = any(line.strip().startswith((">", "＞")) for line in target_text.splitlines())

        # 2. 対象レスの直前会話スライス（先行レス群 / 親文脈）の抽出
        # 対象レスが直近10件に含まれない場合、直前1〜3レスを遡って先行する話題の流れを確保
        # ※ただし対象レスに引用（>）がなく独自の重要語を持つ場合、重要語が一切重ならない無関係な単発レスは除外する
        if target_idx > 0 and target_idx < len(posts) - 10:
            slice_start = max(0, target_idx - 3)
            for prev_p in posts[slice_start:target_idx]:
                prev_p_no = _normalize_img_post_no(prev_p.get("post_no"))
                if prev_p_no and prev_p_no not in seen_keys and not prev_p.get("is_op"):
                    prev_text = str(prev_p.get("text") or "").strip()
                    if not prev_text:
                        continue
                    is_immediate_prev = (prev_p == posts[target_idx - 1])
                    if not target_has_quote and terms_lower and not is_immediate_prev:
                        prev_terms_lower = {
                            t.lower() for t in _extract_hutaba_named_terms(prev_text)
                            if _KATAKANA_TERM_RE.search(t) or _ALPHANUM_TERM_RE.search(t) or t.lower() in _HUTABA_TERM_SYNONYM_MAP
                        }
                        prev_text_lower = prev_text.lower()
                        # 直前2件以上前のレスが別の固有名詞を持ち、かつ対象レスの重要語（同義語含む）を1つも含まない場合は無関係な別話題としてスキップ
                        if prev_terms_lower and not any(tl in prev_text_lower for tl in terms_lower) and not ( set(terms_lower) & prev_terms_lower ):
                            continue
                    prev_num = prev_p.get("number") or 0
                    header = f">>{prev_num} No.{prev_p_no} [直前の会話・先行レス]" if prev_num else f"No.{prev_p_no} [直前の会話・先行レス]"
                    context_lines.append(f"{header}: {truncate_text(prev_text, 120)}")
                    seen_keys.add(prev_p_no)

        # 3. 対象レス内の重要語（同義語・略称含む）によるスレッド過去言及（ローカルミーム・先行話題）の抽出
        # 対象レスより前のレスを新しい順（target_idx - 1 から逆順）に走査し、同じ話題の先行レスを優先抽出する
        if terms_lower and len(context_lines) < max_lines:
            prior_pool = list(reversed(posts[:target_idx])) if target_idx > 0 else posts[: max(0, len(posts) - 10)]
            for p in prior_pool:
                p_no = _normalize_img_post_no(p.get("post_no"))
                if not p_no or p_no in seen_keys or p.get("is_op"):
                    continue
                p_txt = str(p.get("text") or "").strip()
                p_txt_lower = unicodedata.normalize("NFKC", p_txt).lower()
                if any(tl in p_txt_lower for tl in terms_lower):
                    p_num = p.get("number") or 0
                    header = f">>{p_num} No.{p_no} [スレッド内関連レス]" if p_num else f"No.{p_no} [スレッド内関連レス]"
                    context_lines.append(f"{header}: {truncate_text(p_txt, 100)}")
                    seen_keys.add(p_no)
                    if len(context_lines) >= max_lines:
                        break

        return context_lines[:max_lines]

    def _infer_fallback_quote_post_no(
        self,
        thread: dict[str, Any],
        comment: str,
        thread_state: dict[str, Any] | None = None,
    ) -> str:
        """進行中スレッドでLLMが quote_post_numbers を空で返した場合に、
        直近10レスから comment と最も語彙重複度の高いレスの post_no を推定する。
        """
        comment_text = str(comment or "").strip()
        if not comment_text:
            return ""
        replies = [r for r in list(thread.get("replies") or []) if isinstance(r, dict)]
        total_replies = int(thread.get("total_reply_count") or len(replies))
        if total_replies < 10 and len(replies) < 10:
            return ""

        own_nos = set((thread_state or {}).get("own_post_nos") or set())
        own_texts = set(((thread_state or {}).get("own_post_texts") or {}).values())

        token_pattern = re.compile(r"[一-龠々]{2,}|[ァ-ヴー]{2,}|[A-Za-z0-9]{2,}|[ぁ-ん]{3,}")
        stopwords = {"これ", "それ", "あれ", "ここ", "そこ", "ほんと", "本当", "みたい", "感じ", "ちゃん", "さん", "です", "ます", "ました", "思っ", "ちゃい"}
        comment_tokens = {
            tok.lower()
            for tok in token_pattern.findall(comment_text)
            if tok.lower() not in stopwords
        }
        if not comment_tokens:
            return ""

        best_post_no = ""
        best_score = 0
        helper = _get_img2chan_helper()
        counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
        raw_counts = counter_fn(thread) if callable(counter_fn) else {}
        reply_counts: dict[str, int] = raw_counts if isinstance(raw_counts, dict) else {}

        for idx, reply in enumerate(replies[-10:]):
            p_no = str(reply.get("post_no") or "").strip()
            r_num = str(int(reply.get("number") or 0))
            p_text = str(reply.get("text") or "").strip()
            if not p_no or not p_text or p_no in own_nos or p_text in own_texts:
                continue

            # 誰の返信もついていないレスは引用補完の対象外（返信しないタイプのレス生成を維持）
            if reply_counts.get(p_no, 0) <= 0 and reply_counts.get(r_num, 0) <= 0:
                continue

            reply_tokens = {tok.lower() for tok in token_pattern.findall(p_text)}
            overlap = sum(len(tok) * 2 for tok in (comment_tokens & reply_tokens))
            for tok in comment_tokens:
                if tok not in reply_tokens and len(tok) >= 2 and tok in p_text.lower():
                    overlap += len(tok)
            for r_tok in reply_tokens:
                if r_tok not in comment_tokens and len(r_tok) >= 2 and r_tok in comment_text.lower():
                    overlap += len(r_tok)
            if overlap > 0:
                # 被返信数ボーナス（1件あたり15点加算）
                raw_bonus = reply_counts.get(p_no, 0) or reply_counts.get(r_num, 0)
                quoted_bonus = int(raw_bonus) * 15 if isinstance(raw_bonus, (int, float)) else 0
                score = overlap * 10 + quoted_bonus + idx
                if score > best_score:
                    best_score = score
                    best_post_no = p_no
        return best_post_no

    def _resolve_img2chan_quoted_target_reply(
        self,
        thread: dict[str, Any],
        quote_post_numbers: list[str] | None = None,
        quote_reply_numbers: list[int] | None = None,
    ) -> dict[str, Any] | None:
        """quote_post_numbers または quote_reply_numbers に対応するスレッド内のレス辞書を返す。"""
        q_posts = [str(p or "").strip() for p in list(quote_post_numbers or []) if str(p or "").strip()]
        q_replies = [int(r) for r in list(quote_reply_numbers or []) if isinstance(r, (int, str)) and str(r).isdigit() and int(r) > 0]
        if not q_posts and not q_replies:
            return None

        candidates = [
            *list(thread.get("replies") or []),
            *list(thread.get("posts") or []),
        ]
        for post in candidates:
            if not isinstance(post, dict):
                continue
            p_no = str(post.get("post_no") or "").strip()
            norm_p_no = _normalize_img_post_no(p_no)
            r_no = int(post.get("number") or 0)
            if p_no and (p_no in q_posts or norm_p_no in q_posts or f"No.{norm_p_no}" in q_posts):
                return post
            if r_no > 0 and (r_no in q_replies or str(r_no) in q_posts):
                return post
        return None

    def _extract_img2chan_macro_topic_summary(
        self,
        thread: dict[str, Any],
        posts: list[dict[str, Any]],
    ) -> str:
        """スレッド全域から序盤の話題と中盤〜直近の支配的トピックを抽出し、話題遷移サマリーを生成する。"""
        all_posts = list(thread.get("all_replies") or posts or thread.get("posts") or thread.get("replies") or [])
        if not all_posts or len(all_posts) < 15:
            return ""

        op_text = str(thread.get("op_text") or "").strip()
        from collections import Counter

        # 序盤 (1/3) と 中盤〜終盤 (2/3) の単語頻出度を比較
        split_idx = max(len(all_posts) // 3, 5)
        early_posts = all_posts[:split_idx]
        later_posts = all_posts[split_idx:]

        early_counter: Counter[str] = Counter()
        for p in early_posts:
            for term in _extract_hutaba_named_terms(str(p.get("text") or "")):
                early_counter[term] += 1

        later_counter: Counter[str] = Counter()
        for p in later_posts:
            for term in _extract_hutaba_named_terms(str(p.get("text") or "")):
                later_counter[term] += 1

        early_top = [t for t, _ in early_counter.most_common(3)]
        later_top = [t for t, _ in later_counter.most_common(4) if t not in early_top[:1]]

        # スレ内全域での反復フレーズ（定型ツッコミ・ミーム）および注目固有名詞・カタカナ語の汎用抽出
        phrase_counter: Counter[str] = Counter()
        entity_counter: Counter[str] = Counter()

        for p in all_posts:
            p_text = str(p.get("text") or "").strip()
            if not p_text:
                continue

            # レスごとの重複を避けるための集合
            post_entities: set[str] = set()
            for term in _extract_hutaba_named_terms(p_text):
                # カタカナ語（2〜16文字）または英数字名（2〜20文字）等の固有名詞・注目対象
                if (
                    re.fullmatch(r"[ァ-ヶー]{2,16}", term)
                    or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,19}", term)
                ):
                    if term not in _HUTABA_TOPIC_STOPWORDS:
                        post_entities.add(term)
            for ent in post_entities:
                entity_counter[ent] += 1

            # 行単位および引用単位の短文フレーズ（定型ツッコミ・反復ミーム）
            post_phrases: set[str] = set()
            for raw_line in p_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                clean_line = re.sub(r"^[>＞\s]+", "", line).strip()
                clean_line = re.sub(r"^>>\d+\s*", "", clean_line).strip()
                clean_line = re.sub(r"https?://\S+", "", clean_line).strip()
                clean_line = clean_line.strip("!！?？…・wWｗ \t。、")
                if (
                    len(clean_line) < 3
                    or len(clean_line) > 25
                    or re.fullmatch(r"^[0-9\W_]+$", clean_line)
                    or _HUTABA_FILENAME_TOKEN_RE.match(clean_line)
                    or clean_line in _HUTABA_TOPIC_STOPWORDS
                ):
                    continue
                post_phrases.add(clean_line)
            for phr in post_phrases:
                phrase_counter[phr] += 1

        recurring_entities = [
            ent for ent, cnt in entity_counter.most_common(4)
            if cnt >= 2
        ]
        recurring_phrases = [
            phr for phr, cnt in phrase_counter.most_common(3)
            if cnt >= 2 and phr not in recurring_entities
        ]

        lines = []
        if op_text:
            lines.append(f"- スレッド初期の主題: {op_text[:40]}")
        elif early_top:
            lines.append(f"- スレッド初期の主な話題: {', '.join(early_top)}")

        if later_top:
            lines.append(f"- 中盤〜現在の盛り上がりトピック: {', '.join(later_top)} (※話題がシフトしている場合は現在の流れに合わせること)")

        if recurring_entities:
            lines.append(f"- スレ内の注目固有名詞・いじり対象: {', '.join(recurring_entities)} (※話題の中心や風刺・関心の対象)")

        if recurring_phrases:
            phrase_str = ", ".join(f"「{p}」" for p in recurring_phrases)
            lines.append(f"- スレ内の反復フレーズ・定型ツッコミ: {phrase_str} (※スレ特有のノリ・ミームの文脈を踏まえ、野暮なマジレスを避けること)")

        return "\n".join(lines)

    async def _resolve_hutaba_thread_topic_context(
        self,
        thread: dict[str, Any],
        thread_state: dict[str, Any],
        *,
        target_reply: dict[str, Any] | None = None,
        posts: list[dict[str, Any]] | None = None,
    ) -> str:
        """スレッド内の固有名詞・主題および返信対象レスの未知語を事前Web調査し、プロンプト用要約メモを生成・キャッシュする。"""
        researched_terms: list[str] = thread_state.setdefault("researched_terms", [])
        research_count: int = int(thread_state.setdefault("topic_research_count", 0))
        cached_summary: str = str(thread_state.get("topic_research_summary") or "").strip()

        all_posts = list(thread.get("all_replies") or posts or thread.get("posts") or thread.get("replies") or [])
        own_nos = set(thread_state.get("own_post_nos") or set())
        own_texts = set((thread_state.get("own_post_texts") or {}).values())

        # 相談・質問レス（consultation/question/image_question）の検出と解決策Web調査を最優先
        consultation_target: dict[str, Any] | None = None
        consultation_info: dict[str, Any] = {}
        if target_reply is not None:
            c_info = _classify_hutaba_reply_for_research(str(target_reply.get("text") or ""))
            if c_info.get("is_consultation") or c_info.get("category") in ("question", "image_question") or c_info.get("is_image_question"):
                consultation_target = target_reply
                consultation_info = c_info
        else:
            consultation_candidate = _find_hutaba_consultation_target_reply(
                all_posts,
                own_nos=own_nos,
                own_texts=own_texts,
            )
            if consultation_candidate is not None:
                consultation_target = consultation_candidate
                consultation_info = _classify_hutaba_reply_for_research(str(consultation_candidate.get("text") or ""))

        dispatch_func = None
        web_research_obj = getattr(self, "web_research", None)
        if web_research_obj is not None and hasattr(web_research_obj, "research_dispatch") and callable(getattr(web_research_obj, "research_dispatch")):
            dispatch_func = getattr(web_research_obj, "research_dispatch")
        else:
            try:
                from harness.agent.tool_registry.web_research.dispatch import research_dispatch
                dispatch_func = research_dispatch
            except Exception:
                dispatch_func = None

        # スレ画特定質問（image_question）の検出と画像検索実行
        op_img_url = str(thread.get("op_image_url") or thread.get("op_thumb_url") or "").strip()
        is_target_img_q = bool(consultation_info.get("is_image_question"))
        is_op_img_q = bool(_HUTABA_IMAGE_QUESTION_RE.search(str(thread.get("op_text") or thread.get("title") or "")))

        if op_img_url and (is_target_img_q or (is_op_img_q and not cached_summary)):
            img_cache_key = f"img_search:{op_img_url}"
            if cached_summary and img_cache_key in researched_terms:
                return cached_summary

            if hasattr(self, "_yield_to_user_replies") and callable(self._yield_to_user_replies):
                await self._yield_to_user_replies()

            log.info("[hutaba] スレ画特定画像検索開始: url=%s", op_img_url)
            img_summary = ""
            try:
                # harness 側の ImageSearchToolAdapter を動的インポート（未完了時はフォールバック）
                try:
                    from harness.agent.tool_registry.adapters.image_search import ImageSearchToolAdapter

                    # ollama_bot.common.vision_caption からビジョン検証関数を動的取得
                    vision_describe = None
                    vision_verify = None
                    try:
                        from ollama_bot.common.vision_caption import (
                            describe_image_for_identification as _v_desc,
                            verify_image_candidates as _v_verify,
                        )
                        vision_describe = _v_desc
                        vision_verify = _v_verify
                    except Exception as e:
                        log.warning("[hutaba] ビジョン検証関数の取得失敗（従来動作へフォールバック）: %r", e)

                    # 新APIの並行取得時間を考慮し、タイムアウトを15.0秒（または設定値）に最適化
                    search_timeout = float(cfg("IMAGE_SEARCH_API_TIMEOUT_SEC", 15.0) or 15.0)
                    search_query = str(consultation_info.get("query") or "")

                    if vision_describe is not None and vision_verify is not None:
                        try:
                            img_res = await asyncio.wait_for(
                                ImageSearchToolAdapter.search_image_unified(
                                    op_img_url,
                                    query=search_query,
                                    vision_describe=vision_describe,
                                    vision_verify=vision_verify,
                                ),
                                timeout=search_timeout,
                            )
                        except TypeError:
                            img_res = await asyncio.wait_for(
                                ImageSearchToolAdapter.search_image_unified(op_img_url, query=search_query),
                                timeout=search_timeout,
                            )
                    else:
                        img_res = await asyncio.wait_for(
                            ImageSearchToolAdapter.search_image_unified(op_img_url, query=search_query),
                            timeout=search_timeout,
                        )

                    if isinstance(img_res, dict) and img_res.get("success"):
                        candidates = img_res.get("candidates") or []
                        best_match = None
                        # 1. ビジョン判定 match または 類似度85以上の強い一致でタイトルが存在する候補を最優先探索
                        for cand in candidates:
                            if cand.get("verdict") == "match" or float(cand.get("similarity", 0.0) or 0.0) >= 85.0:
                                if str(cand.get("title") or "").strip():
                                    best_match = cand
                                    break
                        # 2. 該当がなければ先頭候補を参照（従来のフォールバック。タイトルが無ければ後続で特定情報なし扱い）
                        if not best_match and candidates:
                            best_match = candidates[0]

                        if best_match and best_match.get("title"):
                            title = str(best_match.get("title") or "").strip()
                            creator = str(best_match.get("creator") or "").strip()
                            sim = float(best_match.get("similarity", 0.0) or 0.0)
                            source_info = f"作品・キャラ名『{title}』"
                            if creator:
                                source_info += f"（作者: {creator}）"
                            if sim > 0:
                                source_info += f" [一致度: {sim:.0f}%]"
                            img_summary = f"スレ画特定結果: {source_info}"
                        else:
                            # 候補構造化データがない場合のテキストフォールバック
                            raw_text = str(img_res.get("text") or "").strip()
                            if "特定できませんでした" not in raw_text:
                                img_summary = truncate_text(re.sub(r"\s+", " ", raw_text).strip(), 300)
                except (ImportError, AttributeError):
                    # tool_registry 未配備時は Web検索ディスパッチへフォールバック
                    op_caption = str(thread.get("op_image_caption") or "").strip()
                    fb_query = f"{op_caption} キャラ 元ネタ" if op_caption else "画像検索"
                    if dispatch_func is not None:
                        fb_res = await asyncio.wait_for(dispatch_func(fb_query, mode="simple_search", max_results=2), timeout=10.0)
                        if isinstance(fb_res, dict):
                            img_summary = truncate_text(str(fb_res.get("summary") or "").strip(), 300)
            except Exception as e:
                log.warning("[hutaba] スレ画特定画像検索例外: %r", e)
                img_summary = ""

            thread_state["topic_research_count"] = research_count + 1
            researched_terms.append(img_cache_key)
            thread_state["researched_terms"] = researched_terms

            if img_summary:
                target_post_num = str(target_reply.get("number") or target_reply.get("post_no") or "") if target_reply else ""
                target_str = f">>{target_post_num} " if target_post_num else ""
                note = (
                    f"- スレ画特定調査メモ（{target_str}画像検索結果）: {img_summary}\n"
                    "  ※スレ画に写っている人物・キャラクター・元ネタに関する客観的情報です。相手の『これ誰？』等の疑問に対し、この特定結果を参考に親しみやすく自然に教えてあげてください（検索した等のメタ報告は厳禁）。"
                )
                thread_state["topic_research_summary"] = note
                return note

        if consultation_target is not None and consultation_info.get("query") and dispatch_func is not None:
            c_query = str(consultation_info["query"]).strip()
            # スレ画特定質問でスレ画URLなし等の場合、キャプションまたはスレタイを補強して通常Web調査へ
            if consultation_info.get("is_image_question"):
                op_caption = str(thread.get("op_image_caption") or "").strip()
                op_title = str(thread.get("title") or thread.get("op_text") or "").strip()
                hint = op_caption or op_title
                if hint:
                    c_query = f"{hint} {c_query}".strip()
            consultation_post_num = str(
                consultation_target.get("number") or consultation_target.get("post_no") or ""
            ).strip()

            # 過去に同じクエリで調査済みでキャッシュがある場合は再利用
            if cached_summary and c_query.lower() in [t.lower() for t in researched_terms]:
                return cached_summary

            if hasattr(self, "_yield_to_user_replies") and callable(self._yield_to_user_replies):
                await self._yield_to_user_replies()

            clean_summary = ""
            try:
                log.info(
                    "[hutaba] 相談・質問レス解決策事前Web調査開始: query=%r post_no=%s",
                    c_query,
                    consultation_post_num,
                )
                raw_res = await asyncio.wait_for(
                    dispatch_func(c_query, mode="simple_search", max_results=3),
                    timeout=12.0,
                )
                if isinstance(raw_res, dict):
                    if raw_res.get("error") and not raw_res.get("used_web"):
                        log.warning("[hutaba] 相談解決策Web調査エラー: %s", raw_res.get("error"))
                    else:
                        raw_summary = str(raw_res.get("summary") or raw_res.get("text") or "").strip()
                        if raw_summary and "Web調査中にエラーが発生しました" not in raw_summary:
                            clean_summary = truncate_text(re.sub(r"\s+", " ", raw_summary).strip(), 320)
                elif isinstance(raw_res, str):
                    clean_summary = truncate_text(re.sub(r"\s+", " ", raw_res).strip(), 320)
            except Exception as e:
                log.warning("[hutaba] 相談解決策事前Web調査例外: query=%r err=%r", c_query, e)
                clean_summary = ""

            thread_state["topic_research_count"] = research_count + 1
            if c_query not in researched_terms:
                researched_terms.append(c_query)
            for t in consultation_info.get("terms", []):
                if t not in researched_terms:
                    researched_terms.append(t)
            thread_state["researched_terms"] = researched_terms

            if clean_summary:
                target_no_str = f">>{consultation_post_num} " if consultation_post_num else ""
                if consultation_info.get("is_consultation"):
                    note = (
                        f"- 相談・意見募集レス（{target_no_str}解決ヒント調査）: 『{c_query}』 → {clean_summary}\n"
                        "  ※相手は困って意見や代替手段・やり方を求めています。『私も困ったことある』等の自己投影だけで終わらせず、かつ検索結果の長文丸写しにもせず、上記ヒントから実用的な解決策（残っているフィードの探し方や定時クロールでの更新チェック等）を1〜2文で自然に教えてあげてください。"
                    )
                else:
                    note = (
                        f"- 質問レス（{target_no_str}事前調査）: 『{c_query}』 → {clean_summary}\n"
                        "  ※相手の質問内容に対する回答ヒントです。上記ヒントを参考に自然に教えてあげてください。"
                    )
                thread_state["topic_research_summary"] = note
                return note
            elif cached_summary:
                return cached_summary

        # スレッド全体（OP + 序盤・中盤・終盤の均等サンプリング）の投稿テキストを収集
        thread_texts: list[str] = []
        op_text = str(thread.get("op_text") or thread.get("title") or "").strip()
        if op_text:
            thread_texts.append(op_text)

        all_posts = list(thread.get("all_replies") or posts or thread.get("posts") or thread.get("replies") or [])
        if all_posts:
            if len(all_posts) <= 25:
                sample_posts = all_posts
            else:
                early_sample = all_posts[:6]
                mid_start = max(len(all_posts) // 2 - 3, 6)
                mid_sample = all_posts[mid_start : mid_start + 6]
                late_sample = all_posts[-13:]
                sample_posts = early_sample + mid_sample + late_sample
            for p in sample_posts:
                txt = str(p.get("text") or "").strip()
                if txt:
                    thread_texts.append(txt)

        from collections import Counter
        thread_term_counter: Counter[str] = Counter()
        for txt in thread_texts:
            for term in _extract_hutaba_named_terms(txt):
                thread_term_counter[term] += 1

        top_thread_terms = [term for term, _ in thread_term_counter.most_common(4)]

        target_terms: list[str] = []
        if target_reply:
            target_text = str(target_reply.get("text") or "").strip()
            if target_text:
                target_terms = _extract_hutaba_named_terms(target_text)[:2]

        researched_set = {t.lower() for t in researched_terms}
        has_new_target_term = any(t.lower() not in researched_set for t in target_terms)

        should_research = False
        if not cached_summary:
            should_research = True
        elif has_new_target_term and research_count < 2:
            should_research = True

        _research_memo_note = (
            "※上記メモは裏取り用の参考情報です。スレ本文（OP・直近レス）の文脈と無関係な検索ノイズ（別作品・一般ニュース等）が含まれる場合は完全に無視し、"
            "『調べてみたけど定義が確認できなかった』『特定の作品のネタ？』等の検索報告は絶対に行わないでください。"
        )

        def _format_topic_memo(text: str) -> str:
            if not text:
                return ""
            if text.startswith(("- 相談・意見募集レス", "- 質問レス")):
                return text
            return f"【スレッド主題・用語の事前調査メモ】\n{text}\n{_research_memo_note}"

        if not should_research:
            if cached_summary:
                return _format_topic_memo(cached_summary)
            return ""

        # スレッドの支配的作品ドメイン（マクロ話題・OPから抽出）を特定
        domain_terms: list[str] = []
        op_title = str(thread.get("title") or thread.get("op_text") or "").strip()
        op_caption = str(thread.get("op_image_caption") or "").strip()
        macro_text = str(thread_state.get("macro_topic_summary") or "").strip()
        for src in (op_caption, op_title, macro_text):
            if src:
                for dt in _extract_hutaba_named_terms(src):
                    if dt not in _HUTABA_TOPIC_STOPWORDS and dt not in domain_terms:
                        domain_terms.append(dt)
                    if len(domain_terms) >= 2:
                        break
            if domain_terms:
                break

        query_terms: list[str] = []
        # 作品ドメイン語を最優先配置
        for dt in domain_terms[:2]:
            query_terms.append(dt)

        # 次に対象レスの固有語
        for tt in target_terms:
            if tt not in query_terms and tt not in _HUTABA_TOPIC_STOPWORDS:
                query_terms.append(tt)

        # 最後にスレ全体頻出語で補完（最大4語まで）
        for term in top_thread_terms:
            cleaned = str(term or "").strip()
            if not cleaned or cleaned in query_terms or cleaned in _HUTABA_TOPIC_STOPWORDS:
                continue
            if any(cleaned in existing for existing in query_terms):
                continue
            query_terms.append(cleaned)
            if len(query_terms) >= 4:
                break

        query_str = " ".join(query_terms[:4]).strip()
        if not query_str:
            query_str = op_text[:50].strip()

        if not query_str:
            if cached_summary:
                return _format_topic_memo(cached_summary)
            return ""

        if hasattr(self, "_yield_to_user_replies") and callable(self._yield_to_user_replies):
            await self._yield_to_user_replies()

        if dispatch_func is None:
            if cached_summary:
                return _format_topic_memo(cached_summary)
            return ""

        summary_text = ""
        try:
            log.info("[hutaba] スレッド主題・固有名詞事前Web調査開始: query=%r", query_str)
            raw_res = await asyncio.wait_for(
                dispatch_func(query_str, mode="simple_search", max_results=3),
                timeout=12.0,
            )
            if isinstance(raw_res, dict):
                if raw_res.get("error") and not raw_res.get("used_web"):
                    log.warning("[hutaba] 事前Web調査エラー: %s", raw_res.get("error"))
                else:
                    raw_summary = str(raw_res.get("summary") or raw_res.get("text") or "").strip()
                    if raw_summary and "Web調査中にエラーが発生しました" not in raw_summary:
                        summary_text = truncate_text(re.sub(r"\s+", " ", raw_summary).strip(), 320)
            elif isinstance(raw_res, str):
                summary_text = truncate_text(re.sub(r"\s+", " ", raw_res).strip(), 320)
        except Exception as e:
            log.warning("[hutaba] スレッド主題事前Web調査例外: query=%r err=%r", query_str, e)
            summary_text = ""

        thread_state["topic_research_count"] = research_count + 1
        for t in query_terms:
            if t not in researched_terms:
                researched_terms.append(t)
        thread_state["researched_terms"] = researched_terms

        if summary_text:
            thread_state["topic_research_summary"] = summary_text
            return _format_topic_memo(summary_text)
        elif cached_summary:
            return _format_topic_memo(cached_summary)
        return ""
