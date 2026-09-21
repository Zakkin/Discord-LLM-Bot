"""20の扉ゲームの開始、出題語生成、質問判定、Discord操作を担当するMixin。
（AIエージェント向け説明：このファイルは20の扉ゲームの進行管理、Discordコマンド処理、状態管理、LLMへの出題・判定依頼を担っています。分割の際はこの責務に影響がないよう注意してください。）"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..common.config_helpers import cfg, cfg_str

from ..common.discord_helpers import author_id, channel_id, content, resolve_reference_message
from lib.discord_utils import send_interaction_or_fallback
from ..common.fact_check import search_web
from ..common.ollama_helpers import (
    call_ollama_json,
    extract_first_user_facing_reply,
    sanitize_generated_reply,
    truncate_lines,
    truncate_text,
)
from .ollama_chat_types import MessageRuntime

if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _OllamaChat20DoorsBase = OllamaChatProtocol
else:
    _OllamaChat20DoorsBase = object

log = logging.getLogger("ollama_bot.ollama_chat_.ollama_chat_20doors")


TWENTY_DOORS_SETUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "word": {"type": "string"},
        "category": {"type": "string"},
        "hint_1": {"type": "string"},
        "hint_2": {"type": "string"},
    },
    "required": ["word", "category", "hint_1", "hint_2"],
    "additionalProperties": False,
}

TWENTY_DOORS_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "シークレットワードと質問内容を照らし合わせた短い判定理由",
        },
        "judgement": {
            "type": "string",
            "enum": ["yes", "no", "near", "unknown", "correct"],
        },
    },
    "required": ["reason", "judgement"],
    "additionalProperties": False,
}

TWENTY_DOORS_JUDGE_NO_CORRECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "シークレットワードと質問内容を照らし合わせた短い判定理由",
        },
        "judgement": {
            "type": "string",
            "enum": ["yes", "no", "near", "unknown"],
        },
    },
    "required": ["reason", "judgement"],
    "additionalProperties": False,
}

TWENTY_DOORS_START_PATTERNS = (
    "20の扉",
    "20doors",
    "twenty questions",
    "twentyquestions",
)

TWENTY_DOORS_START_INTENT_PATTERNS = (
    "しよう",
    "やろう",
    "やる",
    "遊ぼう",
    "遊ぶ",
    "開始",
    "スタート",
    "start",
)


@dataclass
class TwentyDoorsState:
    secret_word: str
    category: str
    hint_1: str
    hint_2: str
    secret_word_facts: str = ""
    question_count: int = 0
    max_questions: int = 20
    history: list[dict[str, str]] = field(default_factory=list)
    tracked_message_ids: set[int] = field(default_factory=set)


def _build_twenty_doors_persona_block() -> str:

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


def _format_twenty_doors_seed_memories(memories: list[dict[str, Any]]) -> str:
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


def _clean_twenty_doors_field(text: str, *, max_len: int) -> str:
    cleaned = extract_first_user_facing_reply(str(text or "").strip())
    if not cleaned:
        cleaned = sanitize_generated_reply(str(text or "").strip())
    cleaned = re.sub(r"^(?:word|category|hint_1|hint_2)\b\s*[:：]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" 「」『』\"'")
    return truncate_text(cleaned, max_len).strip()


def _normalize_twenty_doors_text(text: str) -> str:
    normalized = _clean_twenty_doors_field(text, max_len=80).lower()
    normalized = re.sub(r"\s+", "", normalized)
    for ch in ("。", "、", "！", "?", "？", "!", "・", "「", "」", "『", "』", "（", "）", "(", ")", "　"):
        normalized = normalized.replace(ch, "")
    return normalized


def _looks_like_recent_twenty_doors_duplicate(word: str, recent_words: list[str]) -> bool:
    normalized = _normalize_twenty_doors_text(word)
    if not normalized:
        return True
    for prev in recent_words:
        if _normalize_twenty_doors_text(prev) == normalized:
            return True
    return False


def _contains_secret_word_spoiler(secret_word: str, hint_text: str) -> bool:
    normalized_secret = _normalize_twenty_doors_text(secret_word)
    normalized_hint = _normalize_twenty_doors_text(hint_text)
    return bool(normalized_secret and normalized_secret in normalized_hint)


def _extract_guess_candidates(user_text: str) -> list[str]:
    text = sanitize_generated_reply(str(user_text or "").strip())
    if not text:
        return []

    text_lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[str] = [text]
    candidates.extend(text_lines)

    patterns = (
        r"(?:答え|正解)\s*(?:は|って)?\s*(.+)$",
        r"(?:もしかして|つまり|ひょっとして)\s*(.+)$",
        r"^それ(?:って|は)?\s*(.+?)(?:ですか|でしょうか)?[?？]*$",
        r"^(.+?)(?:ですか|でしょうか)[?？]*$",
    )
    for line in text_lines:
        for pattern in patterns:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                candidate = str(match.group(1) or "").strip()
                if candidate:
                    candidates.append(candidate)

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_twenty_doors_text(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(candidate)
    return unique


def _is_correct_twenty_doors_guess(user_text: str, secret_word: str) -> bool:
    normalized_secret = _normalize_twenty_doors_text(secret_word)
    if not normalized_secret:
        return False
    return any(_normalize_twenty_doors_text(candidate) == normalized_secret for candidate in _extract_guess_candidates(user_text))


def _build_twenty_doors_setup_system_prompt() -> str:
    persona_block = _build_twenty_doors_persona_block()
    instructions = (
        "あなたはDiscord bot内で遊ぶ『20の扉』ゲームの出題者です。\n"
        "プレイヤーが20問以内の質問で当てられる、誰でも知っている日本語の一般名詞を1つだけ選んでください。\n"
        "固有名詞、人名、作品タイトル、極端にニッチな用語、下品な題材は避けてください。\n"
        "category は短い大分類にしてください。\n"
        "hint_1 は10問目前後向けの広めのヒント、hint_2 は終盤向けのもう一歩具体的なヒントにしてください。\n"
        "hint_1 / hint_2 はキャラクターの口調を少し出して構いませんが、secret word を直接含めてはいけません。\n"
        "出力はJSONのみで、説明文や前置きは不要です。"
    )
    if not persona_block:
        return instructions
    return f"{instructions}\n\n{persona_block}"


def _build_twenty_doors_setup_user_prompt(
    *,
    seed_memories: list[dict[str, Any]],
    recent_context_lines: list[str],
    recent_words: list[str],
) -> str:
    recent_context_block = "\n".join(recent_context_lines) if recent_context_lines else "(直近の会話なし)"
    recent_words_block = "\n".join(f"- {word}" for word in recent_words) if recent_words else "(直近の出題なし)"
    memory_block = _format_twenty_doors_seed_memories(seed_memories)
    return (
        "以下を参考に、『20の扉』のお題候補を1つ選んでください。\n\n"
        f"【最近の会話】\n{recent_context_block}\n\n"
        f"【チャンネルやギルドの記憶】\n{memory_block}\n\n"
        f"【最近使ったお題】\n{recent_words_block}\n\n"
        "最近の会話や記憶に軽く関連していると嬉しいですが、無理に内輪ネタへ寄せすぎないでください。\n"
        "誰でも想像できる題材を優先し、質問でちゃんと絞り込めるものを選んでください。\n"
        "次のJSON形式だけで返してください（以下の値はプレースホルダーなので、選んだお題に合わせて具体的な値を入れてください）:\n"
        '{"word":"リンゴ","category":"果物","hint_1":"赤くて丸いことが多いです","hint_2":"英語でアップル"}'
    )


def _build_twenty_doors_judge_system_prompt(*, allow_correct: bool = True) -> str:
    judgement_choices = "yes / no / near / unknown / correct" if allow_correct else "yes / no / near / unknown"
    correct_rule = (
        "- プレイヤーの発言が「シークレットワードそのもの（正体）」を完全に言い当てた場合のみ correct とします。\n"
        if allow_correct
        else "- この再判定では correct を絶対に選ばないでください。プレイヤーは正体そのものを言い当てていません。\n"
    )
    return (
        "あなたは『20の扉』の判定専用AIです。\n"
        f"必ずJSONのみで返し、judgement は {judgement_choices} のどれか1つだけにしてください。\n"
        "ルール:\n"
        f"{correct_rule}"
        "- 正体そのものを言っていない発言は絶対に correct にせず、yes / no / near / unknown のいずれかで判断してください。\n"
        "- プレイヤーの発言が「シークレットワードの性質・特徴・分類（例: 哺乳類ですか、赤いですか）」である場合、\n"
        "  その特徴をシークレットワードが実際に持っているか（事実として正しいか）を厳密に照合してください。\n"
        "  事実として当てはまるなら yes、違うなら no としてください。性質を聞かれただけで自動的に yes にしてはいけません。\n"
        "- 質問文だから yes ではありません。事実関係を評価して本当にそれが合っている時だけ yes にしてください。\n"
        "- 少しでも不明なら yes にしないでください。閉じた質問なら no か unknown を優先してください。\n"
        "- 核心にかなり近い推測や絞り込みだが正解ではないときだけ near。\n"
        "- 雑談、意味不明、はい/いいえで返しにくい発言、情報不足は unknown。\n"
        "- near は多用しないこと。\n"
        "出力では、先に reason に短い理由を書き、その中で事実確認を示したうえで、最後に judgement を設定してください。"
    )


def _build_twenty_doors_judge_user_prompt(state: TwentyDoorsState, user_text: str) -> str:
    facts_block = f"【事前調査済み事実データ】\n{state.secret_word_facts}\n" if state.secret_word_facts else ""
    return (
        f"シークレットワード: {state.secret_word}\n"
        f"カテゴリ: {state.category}\n{facts_block}"
        f"プレイヤーの発言: {sanitize_generated_reply(str(user_text or '').strip())}\n\n"
        "まず、プレイヤーが答えの正体そのものを当てようとしているのか、"
        "それとも「性質・特徴・分類などを質問している」のかを区別してください。\n"
        "次に、その発言内容が「シークレットワードの事実として本当に正しいか（yesかnoか）」を"
        "推論し、reason に記述してください。\n"
        "（例えば推論の中で『本はゴム製ではないため、事実に反している。よってnoとなる』のように明確に事実確認を行ってください）\n"
        "その後、ルールに従って最終的な判定結果を judgement に設定してください。"
    )


def _build_twenty_doors_start_message(state: TwentyDoorsState) -> str:
    return (
        "🚪 **20の扉 スタート** 🚪\n\n"
        f"カテゴリは **{state.category}** です。\n"
        "このメッセージに返信して、はい / いいえで答えられる質問か、答えそのものを投げてください。\n"
        f"{state.max_questions}問以内で当てられたらクリアです。ギブアップしたいときは `/20doors_giveup` か「ギブアップ」と返信してください。"
    )


def _build_twenty_doors_giveup_message(secret_word: str) -> str:
    return (
        "🏳️ **ギブアップ。ゲーム終了です。**\n\n"
        f"正解は **{secret_word}** でした。"
    )


def _build_twenty_doors_gameover_message(secret_word: str) -> str:
    return (
        "💀 **ゲームオーバー**\n\n"
        f"20問使い切りました。正解は **{secret_word}** でした。"
    )


def _build_twenty_doors_clear_message(secret_word: str, question_count: int) -> str:
    return (
        "🎉 **大正解です。**\n\n"
        f"正解は **{secret_word}**。{question_count}問目でクリアしました。"
    )


def _build_twenty_doors_reply_message(state: TwentyDoorsState, judgement: str) -> str:
    remaining = max(state.max_questions - state.question_count, 0)
    text = {
        "yes": f"⭕ **YES**\n[残り {remaining} 問]",
        "no": f"❌ **NO**\n[残り {remaining} 問]",
        "near": f"🔥 **NEAR**\n[残り {remaining} 問]",
        "unknown": f"❓ **UNKNOWN**\n[残り {remaining} 問]",
    }.get(judgement, f"❓ **UNKNOWN**\n[残り {remaining} 問]")

    hint_1_at = max(int(cfg("TWENTY_DOORS_HINT_1_AT", 10) or 10), 1)
    hint_2_at = max(int(cfg("TWENTY_DOORS_HINT_2_AT", 15) or 15), hint_1_at + 1)
    if state.question_count == hint_1_at and state.hint_1:
        text += f"\n\n💡 **ヒント**\n{state.hint_1}"
    elif state.question_count == hint_2_at and state.hint_2:
        text += f"\n\n💡 **大ヒント**\n{state.hint_2}"
    return text


def _looks_like_twenty_doors_start_text(text: str) -> bool:
    lowered = str(text or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    if not any(pattern in lowered for pattern in TWENTY_DOORS_START_PATTERNS):
        return False
    if compact in TWENTY_DOORS_START_PATTERNS:
        return True
    return any(pattern in lowered for pattern in TWENTY_DOORS_START_INTENT_PATTERNS)


class OllamaChat20DoorsMixin(_OllamaChat20DoorsBase):
    async def _safe_reply_or_send(self, message: discord.Message, content: str) -> discord.Message | None:
        try:
            return await message.reply(content, mention_author=False)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            if getattr(e, "code", 0) != 50035:
                log.warning("20doors reply HTTP error: %r", e)
        except Exception as e:
            log.warning("20doors reply generic error: %r", e)
            
        try:
            return await message.channel.send(content)
        except Exception as e:
            log.warning("20doors fallback send failed: %r", e)
            return None

    def _get_twenty_doors_state_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_twenty_doors_state_lock", None)
        if lock is not None:
            return lock
        lock = asyncio.Lock()
        setattr(self, "_twenty_doors_state_lock", lock)
        return lock

    def _get_twenty_doors_starting_channels(self) -> set[int]:
        channels = getattr(self, "_twenty_doors_starting_channels", None)
        if isinstance(channels, set):
            return channels
        channels = set()
        setattr(self, "_twenty_doors_starting_channels", channels)
        return channels

    def _is_twenty_doors_busy_channel(self, channel_id_value: int | None) -> bool:
        if channel_id_value is None:
            return False
        if channel_id_value in getattr(self, "twenty_doors_states", {}):
            return True
        return channel_id_value in self._get_twenty_doors_starting_channels()

    def _should_ignore_non_twenty_doors_message(self, runtime: MessageRuntime | None) -> bool:
        if runtime is None:
            return False
        if not self._is_twenty_doors_busy_channel(getattr(runtime, "cid", None)):
            return False
        return not bool(getattr(runtime, "is_twenty_doors_reply", False))

    def _build_twenty_doors_start_block_text(self, channel_id_value: int | None) -> str | None:
        if channel_id_value is None:
            return "チャンネル情報を取得できませんでした。"
        if channel_id_value in getattr(self, "twenty_doors_states", {}):
            return "このチャンネルではすでに20の扉が進行中です。"
        if channel_id_value in self._get_twenty_doors_starting_channels():
            return "このチャンネルでは20の扉の開始準備中です。少し待ってください。"
        if channel_id_value in getattr(self, "umigame_states", {}):
            return "今はウミガメのスープが進行中です。先にそちらを終了してください。"
        return None

    async def _reserve_twenty_doors_start(self, channel_id_value: int) -> str | None:
        async with self._get_twenty_doors_state_lock():
            blocked = self._build_twenty_doors_start_block_text(channel_id_value)
            if blocked:
                return blocked
            self._get_twenty_doors_starting_channels().add(channel_id_value)
            return None

    async def _activate_twenty_doors_state(self, channel_id_value: int, state: TwentyDoorsState) -> None:
        async with self._get_twenty_doors_state_lock():
            self.twenty_doors_states[channel_id_value] = state
            self._get_twenty_doors_starting_channels().discard(channel_id_value)

    async def _release_twenty_doors_start(self, channel_id_value: int) -> None:
        async with self._get_twenty_doors_state_lock():
            self._get_twenty_doors_starting_channels().discard(channel_id_value)

    async def _send_twenty_doors_interaction_message(
        self,
        interaction: discord.Interaction,
        content_text: str,
        *,
        ephemeral: bool = False,
        defer_first: bool = False,
    ) -> bool:
        return await send_interaction_or_fallback(
            interaction,
            content_text,
            ephemeral=ephemeral,
            defer_first=defer_first,
            log_tag="20doors",
        )

    async def _is_twenty_doors_reply_message(self, message: discord.Message) -> bool:
        cid = channel_id(message)
        states = getattr(self, "twenty_doors_states", {})
        if cid is None or cid not in states:
            return False
        if not getattr(message, "reference", None):
            return False

        state = states.get(cid)
        if not isinstance(state, TwentyDoorsState):
            return False
        tracked_ids = set()
        for value in state.tracked_message_ids:
            try:
                tracked_ids.add(int(value))
            except Exception:
                continue
        if not tracked_ids:
            return False

        current = message
        max_depth = max(int(cfg("TWENTY_DOORS_REPLY_CHAIN_MAX_DEPTH", 16) or 16), 1)
        for _ in range(max_depth):
            ref_msg = await resolve_reference_message(current)
            if not ref_msg:
                return False
            ref_id = getattr(ref_msg, "id", None)
            if ref_id is not None and int(ref_id) in tracked_ids:
                return True
            current = ref_msg
        return False

    async def _is_twenty_doors_start_request(self, message: discord.Message) -> bool:
        text = str(content(message) or "")
        if not _looks_like_twenty_doors_start_text(text):
            return False

        bot_user_id = self.bot.user.id if self.bot.user else None
        if bot_user_id is None:
            return False
        if any(getattr(user, "id", None) == bot_user_id for user in (getattr(message, "mentions", None) or [])):
            return True

        ref_msg = await resolve_reference_message(message)
        return bool(ref_msg and author_id(ref_msg) == bot_user_id)

    async def _build_twenty_doors_state(
        self,
        *,
        guild_id: int | None,
        channel_id_value: int,
    ) -> TwentyDoorsState:
        if hasattr(self, "game_pool_manager"):
            pooled_item = await self.game_pool_manager.pop_twenty_doors()
            if pooled_item:
                log.info("20doors: popped from pool")
                return TwentyDoorsState(
                    secret_word=pooled_item["word"],
                    category=pooled_item["category"],
                    hint_1=pooled_item["hint_1"],
                    hint_2=pooled_item["hint_2"],
                    secret_word_facts=pooled_item["facts"],
                    max_questions=max(int(cfg("TWENTY_DOORS_MAX_QUESTIONS", 20) or 20), 1),
                )
        log.info("20doors: pool empty, generating on demand")
        memory_persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "").strip() or "default"
        seed_memories = await self.memory_store.get_random_memories(
            persona=memory_persona,
            guild_id=guild_id,
            channel_id=channel_id_value,
            limit=int(cfg("TWENTY_DOORS_MEMORY_SEED_COUNT", 3) or 3),
            min_score=float(cfg("TWENTY_DOORS_MEMORY_SEED_MIN_SCORE", 0.0) or 0.0),
            exclude_memory_types=["feedback", "training_candidate"],
        )
        if not seed_memories:
            seed_memories = await self.memory_store.get_random_memories(
                persona=memory_persona,
                guild_id=guild_id,
                channel_id=None,
                limit=int(cfg("TWENTY_DOORS_MEMORY_SEED_COUNT", 3) or 3),
                min_score=float(cfg("TWENTY_DOORS_MEMORY_SEED_MIN_SCORE", 0.0) or 0.0),
                exclude_memory_types=["feedback", "training_candidate"],
            )

        recent_context_lines: list[str] = [
            str(item["line"]) for item in self.channel_context_cache.get(channel_id_value, [])
            if isinstance(item, dict) and "line" in item and item["line"]
        ]
        recent_context_lines = list(truncate_lines(
            recent_context_lines,
            max_lines=int(cfg("TWENTY_DOORS_PROMPT_CONTEXT_MAX_LINES", 8) or 8),
            max_chars_per_line=int(cfg("TWENTY_DOORS_PROMPT_CONTEXT_MAX_CHARS_PER_LINE", 180) or 180),
            max_total_chars=int(cfg("TWENTY_DOORS_PROMPT_CONTEXT_MAX_TOTAL_CHARS", 900) or 900),
        ))
        recent_words = list(getattr(self, "_recent_twenty_doors_words", []))
        generation_prompt = _build_twenty_doors_setup_user_prompt(
            seed_memories=seed_memories,
            recent_context_lines=recent_context_lines,
            recent_words=recent_words,
        )

        setup_model = str(
            cfg(
                "TWENTY_DOORS_SETUP_MODEL",
                cfg("OLLAMA_MAIN_MODEL", cfg("OLLAMA_MODEL", "")),
            ) or ""
        ).strip()
        if not setup_model:
            raise RuntimeError("20doors setup model is not configured")

        attempts = max(int(cfg("TWENTY_DOORS_GENERATION_ATTEMPTS", 3) or 3), 1)
        json_retries = max(int(cfg("TWENTY_DOORS_GENERATION_RETRIES", cfg("OLLAMA_JSON_RETRIES", 1)) or 1), 0)
        num_predict = max(int(cfg("TWENTY_DOORS_GENERATION_NUM_PREDICT", 256) or 256), 128)
        last_reject_reason = "unknown"

        for attempt_idx in range(attempts):
            result = await call_ollama_json(
                generation_prompt,
                system_prompt=_build_twenty_doors_setup_system_prompt(),
                schema=TWENTY_DOORS_SETUP_SCHEMA,
                model=setup_model,
                think=False,
                timeout_sec=float(cfg("TWENTY_DOORS_GENERATION_TIMEOUT_SEC", 90.0) or 90.0),
                retries=json_retries,
                temperature=float(cfg("TWENTY_DOORS_GENERATION_TEMPERATURE", 0.7) or 0.7),
                top_p=float(cfg("TWENTY_DOORS_GENERATION_TOP_P", 0.9) or 0.9),
                repeat_penalty=float(cfg("TWENTY_DOORS_GENERATION_REPEAT_PENALTY", 1.03) or 1.03),
                num_predict=num_predict + (attempt_idx * 64),
            )

            secret_word = _clean_twenty_doors_field(str(result.get("word", "") or ""), max_len=32)
            category = _clean_twenty_doors_field(str(result.get("category", "") or ""), max_len=32)
            hint_1 = _clean_twenty_doors_field(str(result.get("hint_1", "") or ""), max_len=140)
            hint_2 = _clean_twenty_doors_field(str(result.get("hint_2", "") or ""), max_len=140)

            if not secret_word or not category or not hint_1 or not hint_2:
                last_reject_reason = "empty_field"
                log.warning("20doors setup rejected: attempt=%s/%s reason=%s", attempt_idx + 1, attempts, last_reject_reason)
                continue
            if _looks_like_recent_twenty_doors_duplicate(secret_word, recent_words):
                last_reject_reason = "duplicate_recent_word"
                log.warning("20doors setup rejected: attempt=%s/%s reason=%s word=%r", attempt_idx + 1, attempts, last_reject_reason, secret_word)
                continue
            if _contains_secret_word_spoiler(secret_word, hint_1) or _contains_secret_word_spoiler(secret_word, hint_2):
                last_reject_reason = "spoiler_hint"
                log.warning("20doors setup rejected: attempt=%s/%s reason=%s word=%r", attempt_idx + 1, attempts, last_reject_reason, secret_word)
                continue

            try:
                facts = await search_web(f"{secret_word} とは", max_results=2)
                facts = truncate_text(facts, 400)
                if facts:
                    log.info("20doors web search pre-fetch for %r: ok", secret_word)
            except Exception as e:
                log.warning("20doors web search failed for %r: %r", secret_word, e)
                facts = ""

            return TwentyDoorsState(
                secret_word=secret_word,
                category=category,
                hint_1=hint_1,
                hint_2=hint_2,
                secret_word_facts=facts,
                max_questions=max(int(cfg("TWENTY_DOORS_MAX_QUESTIONS", 20) or 20), 1),
            )

        raise ValueError(f"failed to generate 20doors state: last_reason={last_reject_reason}")

    async def _generate_twenty_doors_for_pool(self) -> dict[str, Any] | None:
        try:
            memory_persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "").strip() or "default"
            seed_memories = await self.memory_store.get_random_memories(
                persona=memory_persona,
                guild_id=None,
                channel_id=None,
                limit=int(cfg("TWENTY_DOORS_MEMORY_SEED_COUNT", 3) or 3),
                min_score=float(cfg("TWENTY_DOORS_MEMORY_SEED_MIN_SCORE", 0.0) or 0.0),
                exclude_memory_types=["feedback", "training_candidate"],
            )

            recent_words = list(getattr(self, "_recent_twenty_doors_words", []))
            generation_prompt = _build_twenty_doors_setup_user_prompt(
                seed_memories=seed_memories,
                recent_context_lines=[],
                recent_words=recent_words,
            )

            setup_model = str(
                cfg(
                    "TWENTY_DOORS_SETUP_MODEL",
                    cfg("OLLAMA_MAIN_MODEL", cfg("OLLAMA_MODEL", "")),
                ) or ""
            ).strip()
            if not setup_model:
                raise RuntimeError("20doors setup model is not configured")

            attempts = max(int(cfg("TWENTY_DOORS_GENERATION_ATTEMPTS", 3) or 3), 1)
            json_retries = max(int(cfg("TWENTY_DOORS_GENERATION_RETRIES", cfg("OLLAMA_JSON_RETRIES", 1)) or 1), 0)
            num_predict = max(int(cfg("TWENTY_DOORS_GENERATION_NUM_PREDICT", 256) or 256), 128)
            last_reject_reason = "unknown"

            for attempt_idx in range(attempts):
                result = await call_ollama_json(
                    generation_prompt,
                    system_prompt=_build_twenty_doors_setup_system_prompt(),
                    schema=TWENTY_DOORS_SETUP_SCHEMA,
                    model=setup_model,
                    think=False,
                    timeout_sec=float(cfg("TWENTY_DOORS_GENERATION_TIMEOUT_SEC", 90.0) or 90.0),
                    retries=json_retries,
                    temperature=float(cfg("TWENTY_DOORS_GENERATION_TEMPERATURE", 0.7) or 0.7),
                    top_p=float(cfg("TWENTY_DOORS_GENERATION_TOP_P", 0.9) or 0.9),
                    repeat_penalty=float(cfg("TWENTY_DOORS_GENERATION_REPEAT_PENALTY", 1.03) or 1.03),
                    num_predict=num_predict + (attempt_idx * 64),
                )

                secret_word = _clean_twenty_doors_field(str(result.get("word", "") or ""), max_len=32)
                category = _clean_twenty_doors_field(str(result.get("category", "") or ""), max_len=32)
                hint_1 = _clean_twenty_doors_field(str(result.get("hint_1", "") or ""), max_len=140)
                hint_2 = _clean_twenty_doors_field(str(result.get("hint_2", "") or ""), max_len=140)

                if not secret_word or not category or not hint_1 or not hint_2:
                    last_reject_reason = "empty_field"
                    log.warning("20doors setup rejected: attempt=%s/%s reason=%s", attempt_idx + 1, attempts, last_reject_reason)
                    continue
                if _looks_like_recent_twenty_doors_duplicate(secret_word, recent_words):
                    last_reject_reason = "duplicate_recent_word"
                    log.warning("20doors setup rejected: attempt=%s/%s reason=%s word=%r", attempt_idx + 1, attempts, last_reject_reason, secret_word)
                    continue
                if _contains_secret_word_spoiler(secret_word, hint_1) or _contains_secret_word_spoiler(secret_word, hint_2):
                    last_reject_reason = "spoiler_hint"
                    log.warning("20doors setup rejected: attempt=%s/%s reason=%s word=%r", attempt_idx + 1, attempts, last_reject_reason, secret_word)
                    continue

                try:
                    facts = await search_web(f"{secret_word} とは", max_results=2)
                    facts = truncate_text(facts, 400)
                except Exception as e:
                    log.warning("20doors web search failed for %r: %r", secret_word, e)
                    facts = ""

                return {
                    "word": secret_word,
                    "category": category,
                    "hint_1": hint_1,
                    "hint_2": hint_2,
                    "facts": facts,
                }

            log.warning(f"failed to generate 20doors pool state: last_reason={last_reject_reason}")
            return None
        except Exception as e:
            log.error("20doors pool generation failed: %r", e)
            return None

    async def _start_20doors_from_message(self, message: discord.Message) -> bool:
        cid = channel_id(message)
        if cid is None:
            return False
        blocked = await self._reserve_twenty_doors_start(cid)
        if blocked:
            await self._safe_reply_or_send(message, blocked)
            return True

        await self._safe_add_reaction(message, "⏳")
        try:
            state = await self._build_twenty_doors_state(
                guild_id=getattr(message.guild, "id", None),
                channel_id_value=cid,
            )
            sent = await message.channel.send(_build_twenty_doors_start_message(state))
            state.tracked_message_ids.add(int(message.id))
            sent_id = getattr(sent, "id", None)
            if sent_id is not None:
                state.tracked_message_ids.add(int(sent_id))
            await self._activate_twenty_doors_state(cid, state)
            if hasattr(self, "_recent_twenty_doors_words"):
                self._recent_twenty_doors_words.append(state.secret_word)
            return True
        except Exception as e:
            log.exception("20doors setup failed: %s", e)
            await self._safe_reply_or_send(
                message,
                cfg_str("TWENTY_DOORS_GENERATION_FAILED_TEXT", "20の扉のお題生成に失敗しました。少し待ってからもう一度試してください。")
            )

            return True
        finally:
            await self._release_twenty_doors_start(cid)
            await self._safe_remove_own_reaction(message, "⏳")

    @app_commands.command(
        name="20doors",
        description="AIが出題する20の扉ゲームを開始します。"
    )
    async def start_twenty_doors(self, interaction: discord.Interaction) -> None:
        cid = getattr(interaction, "channel_id", None)
        if cid is None:
            await self._send_twenty_doors_interaction_message(
                interaction,
                "チャンネル情報を取得できませんでした。",
                ephemeral=True,
            )
            return
        blocked = await self._reserve_twenty_doors_start(cid)
        if blocked:
            await self._send_twenty_doors_interaction_message(
                interaction,
                blocked,
                ephemeral=True,
            )
            return

        response = getattr(interaction, "response", None)
        try:
            if response is not None:
                await response.defer(thinking=True)
        except Exception:
            pass

        try:
            state = await self._build_twenty_doors_state(
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id_value=cid,
            )
            sent = await interaction.edit_original_response(content=_build_twenty_doors_start_message(state))
            sent_id = getattr(sent, "id", None)
            if sent_id is not None:
                state.tracked_message_ids.add(int(sent_id))
            await self._activate_twenty_doors_state(cid, state)
            if hasattr(self, "_recent_twenty_doors_words"):
                self._recent_twenty_doors_words.append(state.secret_word)
        except Exception as e:
            log.exception("20doors slash setup failed: %s", e)
            fail_text = cfg_str(
                "TWENTY_DOORS_GENERATION_FAILED_TEXT",
                "20の扉のお題生成に失敗しました。少し待ってからもう一度試してください。",
            )

            try:
                await interaction.edit_original_response(content=fail_text)
            except Exception:
                await self._send_twenty_doors_interaction_message(interaction, fail_text)
        finally:
            await self._release_twenty_doors_start(cid)

    @app_commands.command(
        name="20doors_giveup",
        description="進行中の20の扉をギブアップして答えを表示します。"
    )
    async def giveup_twenty_doors(self, interaction: discord.Interaction) -> None:
        cid = getattr(interaction, "channel_id", None)
        if cid is None or cid not in getattr(self, "twenty_doors_states", {}):
            await self._send_twenty_doors_interaction_message(
                interaction,
                "現在進行中の20の扉はありません。",
                ephemeral=True,
            )
            return

        state = self.twenty_doors_states.pop(cid, None)
        if state is None:
            await self._send_twenty_doors_interaction_message(
                interaction,
                "現在進行中の20の扉はありません。",
                ephemeral=True,
            )
            return

        await self._send_twenty_doors_interaction_message(
            interaction,
            _build_twenty_doors_giveup_message(state.secret_word),
            defer_first=True,
        )

    async def _judge_twenty_doors_turn(self, state: TwentyDoorsState, user_text: str) -> str:
        if _is_correct_twenty_doors_guess(user_text, state.secret_word):
            return "correct"

        judge_model = str(
            cfg(
                "TWENTY_DOORS_JUDGE_MODEL",
                cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", ""))),
            ) or ""
        ).strip()
        if not judge_model:
            return "unknown"

        try:
            result = await call_ollama_json(
                _build_twenty_doors_judge_user_prompt(state, user_text),
                system_prompt=_build_twenty_doors_judge_system_prompt(),
                schema=TWENTY_DOORS_JUDGE_SCHEMA,
                model=judge_model,
                think=False,
                timeout_sec=float(cfg("TWENTY_DOORS_JUDGE_TIMEOUT_SEC", 20.0) or 20.0),
                retries=int(cfg("TWENTY_DOORS_JUDGE_RETRIES", 1) or 1),
                temperature=float(cfg("TWENTY_DOORS_JUDGE_TEMPERATURE", 0.0) or 0.0),
                top_p=float(cfg("TWENTY_DOORS_JUDGE_TOP_P", 0.3) or 0.3),
                repeat_penalty=float(cfg("TWENTY_DOORS_JUDGE_REPEAT_PENALTY", 1.0) or 1.0),
                num_predict=int(cfg("TWENTY_DOORS_JUDGE_NUM_PREDICT", 256) or 256),
            )
        except Exception as e:
            log.warning("20doors judge failed; heuristic fallback starts: %r", e)
            return "unknown"

        judgement = str(result.get("judgement", "unknown") or "unknown").strip().lower()
        reason = truncate_text(str(result.get("reason", "") or "").strip(), 120)
        if reason:
            log.info("20doors judge rationale judgement=%s reason=%r", judgement, reason)
        if judgement not in {"yes", "no", "near", "unknown", "correct"}:
            return "unknown"
        if judgement == "correct":
            log.warning("20doors judge returned non-exact correct; retrying without correct option reason=%r", reason)
            return await self._rejudge_twenty_doors_non_exact_turn(state, user_text, judge_model=judge_model)
        return judgement

    async def _rejudge_twenty_doors_non_exact_turn(
        self,
        state: TwentyDoorsState,
        user_text: str,
        *,
        judge_model: str,
    ) -> str:
        try:
            result = await call_ollama_json(
                _build_twenty_doors_judge_user_prompt(state, user_text),
                system_prompt=_build_twenty_doors_judge_system_prompt(allow_correct=False),
                schema=TWENTY_DOORS_JUDGE_NO_CORRECT_SCHEMA,
                model=judge_model,
                think=False,
                timeout_sec=float(cfg("TWENTY_DOORS_JUDGE_TIMEOUT_SEC", 20.0) or 20.0),
                retries=int(cfg("TWENTY_DOORS_JUDGE_RETRIES", 1) or 1),
                temperature=float(cfg("TWENTY_DOORS_JUDGE_TEMPERATURE", 0.0) or 0.0),
                top_p=float(cfg("TWENTY_DOORS_JUDGE_TOP_P", 0.3) or 0.3),
                repeat_penalty=float(cfg("TWENTY_DOORS_JUDGE_REPEAT_PENALTY", 1.0) or 1.0),
                num_predict=int(cfg("TWENTY_DOORS_JUDGE_NUM_PREDICT", 256) or 256),
            )
        except Exception as e:
            log.warning("20doors non-exact correct retry failed: %r", e)
            return "unknown"

        judgement = str(result.get("judgement", "unknown") or "unknown").strip().lower()
        reason = truncate_text(str(result.get("reason", "") or "").strip(), 120)
        if reason:
            log.info("20doors judge retry rationale judgement=%s reason=%r", judgement, reason)
        if judgement not in {"yes", "no", "near", "unknown"}:
            return "unknown"
        return judgement

    async def _handle_active_20doors_message(self, message: discord.Message, runtime: MessageRuntime) -> bool:
        cid = runtime.cid
        if cid is None:
            return False

        states = getattr(self, "twenty_doors_states", {})
        state = states.get(cid)
        if not isinstance(state, TwentyDoorsState):
            return False
        if not bool(getattr(runtime, "is_twenty_doors_reply", False)):
            return False

        user_text = (runtime.effective_user_text or runtime.original_user_text or "").strip()
        if not user_text:
            return True

        if "ギブアップ" in user_text or "降参" in user_text:
            states.pop(cid, None)
            self._append_current_user_message(runtime, message)
            sent = await self._safe_reply_or_send(message, _build_twenty_doors_giveup_message(state.secret_word))
            if sent:
                self._append_sent_message(sent)
            return True

        state.question_count += 1
        try:
            judgement = await self._judge_twenty_doors_turn(state, user_text)
        except Exception as e:
            log.exception("20doors judge failed unexpectedly: %s", e)
            state.question_count = max(state.question_count - 1, 0)
            await self._safe_reply_or_send(message, "判定中にエラーが起きました。もう一度聞いてください。")
            return True

        if judgement == "correct":
            states.pop(cid, None)
            reply_text = _build_twenty_doors_clear_message(state.secret_word, state.question_count)
        else:
            reply_text = _build_twenty_doors_reply_message(state, judgement)

        state.history.append({
            "q": truncate_text(user_text, 160),
            "a": truncate_text(reply_text, 160),
        })
        max_history = max(int(cfg("TWENTY_DOORS_HISTORY_MAX_ITEMS", 20) or 20), 1)
        if len(state.history) > max_history:
            del state.history[:-max_history]

        self._append_current_user_message(runtime, message)
        sent = await self._safe_reply_or_send(message, reply_text)

        tracked_ids = state.tracked_message_ids if judgement != "correct" else set()
        for value in (getattr(message, "id", None), getattr(sent, "id", None)):
            if value is None:
                continue
            try:
                tracked_ids.add(int(value))
            except Exception:
                continue

        if judgement == "near":
            await self._safe_add_reaction(message, "🔥")

        if sent:
            self._append_sent_message(sent)

        if judgement != "correct" and state.question_count >= state.max_questions:
            states.pop(cid, None)
            gameover = await message.channel.send(_build_twenty_doors_gameover_message(state.secret_word))
            self._append_sent_message(gameover)
        return True
