"""歌唱機能（歌詞検索、リズムに合わせた順次送信、中断、重複防止）を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..common.config_helpers import cfg, cfg_int
from ..common.discord_helpers import channel_id
from ..common.http_session import get_shared_http_session
from ..common.fact_check import search_web_entries
from ..common.ollama_helpers import (
    call_ollama_json,
)
from ..common.singing_helpers import (
    compute_phrase_delay,
    compute_phrase_delay_with_bpm,
    extract_base_song_title,
    extract_lyrics_from_generic_html,
    format_singing_line,
    is_dummy_lyrics_line,
    is_matching_song_title,
    parse_jlyric_search_results,
    parse_lyrics_to_phrases,
    parse_musescore_tempo,
    parse_uta_net_lyrics_page,
    parse_uta_net_search_results,
    parse_utaten_search_results,
    sanitize_lyrics_lines,
    strip_think_tags,
)

if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _SingMixinBase = OllamaChatProtocol
else:
    _SingMixinBase = object

log = logging.getLogger("ollama_bot.ollama_chat_.ollama_chat_sing")

LYRICS_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean", "description": "指定された曲が実在し、正確な歌詞を知っている場合のみ true。知らない場合や見つからない場合は必ず false"},
        "title": {"type": "string", "description": "曲名（例: 「もってけ!セーラーふく」）。見つからない場合は空文字"},
        "lyrics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "実際の歌唱フレーズの配列（found: true の場合のみ。見つからない場合は空配列 []）",
        },
    },
    "required": ["found", "title", "lyrics"],
    "additionalProperties": False,
}


class OllamaChatSingMixin(_SingMixinBase):
    """歌唱機能を提供するMixinクラス。"""

    def _init_singing_state(self) -> None:
        if not hasattr(self, "singing_tasks") or self.singing_tasks is None:
            self.singing_tasks: dict[int, asyncio.Task[None]] = {}
        if not hasattr(self, "_singing_lock") or self._singing_lock is None:
            self._singing_lock = asyncio.Lock()

    def is_singing_active_in_channel(self, channel_id_val: int) -> bool:
        """指定チャンネルで歌唱タスクが実行中かどうか判定する。"""
        self._init_singing_state()
        task = self.singing_tasks.get(channel_id_val)
        return task is not None and not task.done()

    async def _handle_sing_request(self, message: discord.Message, song_title: str) -> bool:
        """歌唱リクエストを受け取り、排他チェックを行って歌唱タスクを起動する。"""
        self._init_singing_state()
        cid = channel_id(message)
        if cid is None:
            return False

        async with self._singing_lock:
            if self.is_singing_active_in_channel(cid):
                with contextlib.suppress(Exception):
                    await message.reply("いま歌っている最中だよ！ちょっと待ってね♪", mention_author=False)
                return True

            task = asyncio.create_task(
                self._sing_song_workflow(message.channel, song_title, requester=message.author)
            )
            self.singing_tasks[cid] = task

        return True

    async def _stop_singing_in_channel(self, channel_id_val: int) -> bool:
        """指定チャンネルの歌唱タスクを中断する。"""
        self._init_singing_state()
        async with self._singing_lock:
            task = self.singing_tasks.get(channel_id_val)
            if task is not None and not task.done():
                task.cancel()
                self.singing_tasks.pop(channel_id_val, None)
                log.info("cancelled singing task in channel %s", channel_id_val)
                return True
        return False

    async def _stop_singing_from_message(self, message: discord.Message) -> bool:
        """メッセージからの歌唱停止リクエストを処理する。"""
        if getattr(message.author, "bot", False) is True:
            return False

        cid = getattr(message.channel, "id", None)
        if cid is None:
            return False

        if not self.is_singing_active_in_channel(cid):
            return False

        stopped = await self._stop_singing_in_channel(cid)
        if stopped:
            with contextlib.suppress(Exception):
                await message.reply("歌うのをやめたよ！", mention_author=False)
            return True
        return False

    async def _fetch_uta_net_lyrics(self, song_title: str) -> list[str]:
        """歌ネット（uta-net.com）、うたてん（utaten.com）、J-Lyric等から曲名一致を確認して歌詞を直接取得する。"""
        import aiohttp
        from urllib.parse import quote_plus

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }

        # フル曲名とベース曲名（サブタイトル・バージョン除去）の両方を探索対象にする
        base_title = extract_base_song_title(song_title)
        search_titles: list[str] = [song_title]
        if base_title and base_title != song_title:
            search_titles.append(base_title)

        try:
            session = await get_shared_http_session()

            # 1. 歌ネット (uta-net.com) 直接検索（外部検索エンジンの制限・遅延を回避）
            for st in search_titles:
                found_songs: list[tuple[str, str]] = []
                try:
                    utanet_search_url = f"https://www.uta-net.com/search/?Keyword={quote_plus(st)}&Aselect=2"
                    async with session.get(utanet_search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                        if resp.status == 200:
                            search_html = await resp.text()
                            found_songs = parse_uta_net_search_results(search_html)
                except Exception as e:
                    log.warning("Uta-Net direct search query failed for %r: %r", st, e)

                for cand_title, song_url in found_songs:
                    if is_matching_song_title(song_title, cand_title):
                        log.info("found matched song on Uta-Net direct search: title=%r url=%s", cand_title, song_url)
                        try:
                            async with session.get(song_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as song_resp:
                                if song_resp.status == 200:
                                    lyrics_html = await song_resp.text()
                                    lyrics = parse_uta_net_lyrics_page(lyrics_html)
                                    if not lyrics or len(lyrics) < 4:
                                        lyrics = extract_lyrics_from_generic_html(lyrics_html)
                                    if len(lyrics) >= 4:
                                        log.info("successfully scraped %d lines from Uta-Net for %r", len(lyrics), song_title)
                                        return lyrics
                        except Exception as e:
                            log.warning("Uta-Net song page fetch failed for %s: %r", song_url, e)

            # 2. うたてん (utaten.com) 直接検索
            for st in search_titles:
                found_songs = []
                try:
                    utaten_search_url = f"https://utaten.com/search?title={quote_plus(st)}"
                    async with session.get(utaten_search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                        if resp.status == 200:
                            search_html = await resp.text()
                            found_songs = parse_utaten_search_results(search_html)
                except Exception as e:
                    log.warning("Utaten direct search query failed for %r: %r", st, e)

                for cand_title, song_url in found_songs:
                    if is_matching_song_title(song_title, cand_title):
                        log.info("found matched song on Utaten direct search: title=%r url=%s", cand_title, song_url)
                        try:
                            async with session.get(song_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as song_resp:
                                if song_resp.status == 200:
                                    lyrics_html = await song_resp.text()
                                    lyrics = parse_uta_net_lyrics_page(lyrics_html)
                                    if not lyrics or len(lyrics) < 4:
                                        lyrics = extract_lyrics_from_generic_html(lyrics_html)
                                    if len(lyrics) >= 4:
                                        log.info("successfully scraped %d lines from Utaten for %r", len(lyrics), song_title)
                                        return lyrics
                        except Exception as e:
                            log.warning("Utaten song page fetch failed for %s: %r", song_url, e)

            # 3. J-Lyric (j-lyric.net) 直接検索
            for st in search_titles:
                found_songs = []
                try:
                    jlyric_search_url = f"https://j-lyric.net/index_search.html?ka={quote_plus(st)}"
                    async with session.get(jlyric_search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                        if resp.status == 200:
                            search_html = await resp.text()
                            found_songs = parse_jlyric_search_results(search_html)
                except Exception as e:
                    log.warning("J-Lyric direct search query failed for %r: %r", st, e)

                for cand_title, song_url in found_songs:
                    if is_matching_song_title(song_title, cand_title):
                        log.info("found matched song on J-Lyric direct search: title=%r url=%s", cand_title, song_url)
                        try:
                            async with session.get(song_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as song_resp:
                                if song_resp.status == 200:
                                    lyrics_html = await song_resp.text()
                                    lyrics = parse_uta_net_lyrics_page(lyrics_html)
                                    if not lyrics or len(lyrics) < 4:
                                        lyrics = extract_lyrics_from_generic_html(lyrics_html)
                                    if len(lyrics) >= 4:
                                        log.info("successfully scraped %d lines from J-Lyric for %r", len(lyrics), song_title)
                                        return lyrics
                        except Exception as e:
                            log.warning("J-Lyric song page fetch failed for %s: %r", song_url, e)

            # 4. サイト内直接検索で見つからない場合、全Web検索（Wiki, ニコニコ大百科, ブログ, 歌詞サイト等すべて対象）
            queries: list[str] = [f"{song_title} 歌詞"]
            if base_title and base_title != song_title:
                queries.append(f"{base_title} 歌詞")
            queries.extend([
                f"{song_title} wiki 歌詞",
                f"{song_title}",
            ])
            if base_title and base_title != song_title:
                queries.append(f"{base_title} wiki 歌詞")

            for q in queries:
                try:
                    entries = await asyncio.wait_for(
                        search_web_entries(q, max_results=3),
                        timeout=10.0,
                    )
                except Exception:
                    continue

                if not entries:
                    continue

                for entry in entries:
                    title = entry.get("title", "")
                    body = entry.get("body", "")
                    href = entry.get("href", "")
                    # 曲名が一致しているか確認
                    if not is_matching_song_title(song_title, title) and not is_matching_song_title(song_title, body):
                        continue

                    # 検索結果のURLから直接ページを取得して歌詞を抽出（Wiki, ブログ, 歌詞サイト等）
                    candidate_urls: list[str] = []
                    if href:
                        candidate_urls.append(href)
                    found = re.findall(r'https?://[^\s"\'>]+', f"{title} {body} {href}")
                    for u in found:
                        if u not in candidate_urls:
                            candidate_urls.append(u)

                    for song_url in candidate_urls:
                        # 画像・メディア等のURLはスキップ
                        if any(song_url.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".mp3", ".mp4", ".wav", ".pdf")):
                            continue
                        log.info("found matched web page URL: %s for song=%r", song_url, song_title)
                        try:
                            async with session.get(song_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                                if resp.status == 200:
                                    html = await resp.text()
                                    lyrics = parse_uta_net_lyrics_page(html)
                                    if not lyrics or len(lyrics) < 4:
                                        lyrics = extract_lyrics_from_generic_html(html)
                                    if len(lyrics) >= 4:
                                        log.info("successfully scraped %d lines from web page %s for %r", len(lyrics), song_url, song_title)
                                        return lyrics
                        except Exception as e:
                            log.warning("web page scrape failed for %s: %r", song_url, e)
        except Exception as e:
            log.warning("lyrics direct scraping failed: %r", e)
        return []

    async def _fetch_musescore_tempo(self, song_title: str) -> float | None:
        """MuseScore（musescore.com）から曲名一致を確認してテンポ（BPM）を取得する。"""
        base_title = extract_base_song_title(song_title)
        search_titles: list[str] = [song_title]
        if base_title and base_title != song_title:
            search_titles.append(base_title)

        for st in search_titles:
            try:
                entries = await asyncio.wait_for(
                    search_web_entries(f"site:musescore.com {st} sheet music", max_results=3),
                    timeout=6.0,
                )
                if not entries:
                    entries = await asyncio.wait_for(
                        search_web_entries(f"{st} bpm tempo musescore", max_results=3),
                        timeout=6.0,
                    )

                for entry in entries:
                    title = entry.get("title", "")
                    body = entry.get("body", "")
                    # 曲名が同一であることを確認
                    if not is_matching_song_title(song_title, title) and not is_matching_song_title(song_title, body):
                        continue

                    tempo = parse_musescore_tempo(f"{title} {body}")
                    if tempo is not None:
                        log.info("found matched MuseScore BPM=%.1f for song=%r from %r", tempo, song_title, title)
                        return tempo
            except Exception as e:
                log.warning("musescore tempo fetch failed for %r: %r", st, e)
        return None

    async def _fetch_lyrics_with_search_and_llm(self, song_title: str) -> tuple[list[str], float | None]:
        """Web検索・歌ネット・MuseScore・LLM推論を組み合わせて歌詞とテンポを取得する。"""
        # 1. 歌詞サイトからの直接スクレイピングを最優先で試行（曲名一致確認つき）
        uta_net_lyrics = await self._fetch_uta_net_lyrics(song_title)
        bpm = await self._fetch_musescore_tempo(song_title)

        if uta_net_lyrics:
            return uta_net_lyrics, bpm

        # 2. 歌詞サイト・Webページ直接スクレイピングで見つからない場合、一般的なWeb検索＋LLMで歌詞を抽出
        base_title = extract_base_song_title(song_title)
        search_query = f"{song_title} 歌詞"
        search_results_text = ""
        try:
            entries = await asyncio.wait_for(
                search_web_entries(search_query, max_results=3),
                timeout=10.0,
            )
            if not entries and base_title and base_title != song_title:
                entries = await asyncio.wait_for(
                    search_web_entries(f"{base_title} 歌詞", max_results=3),
                    timeout=10.0,
                )
            if not entries:
                entries = await asyncio.wait_for(
                    search_web_entries(f"{song_title} wiki 歌詞", max_results=3),
                    timeout=10.0,
                )
            if entries:
                snippets = []
                for e in entries:
                    t = e.get("title", "")
                    b = e.get("body", "")
                    snippets.append(f"【{t}】\n{b}")
                search_results_text = "\n\n".join(snippets)
        except Exception as e:
            log.warning("singing web search failed: %r", e)

        system_prompt = (
            "あなたは歌の歌詞を正確に思い出すアシスタントです。指定された曲が実在し、歌詞を知っている場合のみ、正確な歌詞の1番（またはサビを含む主要部分）を行ごとに抽出してください。\n"
            "【ルール】\n"
            "・検索結果がある場合はそれを最優先で参照してください。\n"
            "・【重要・捏造厳禁】あなたがその曲の正確な歌詞を知らない場合や、架空の曲名の場合は、絶対に自分で歌詞を作詞・創作しないでください。必ず found: false にしてください。\n"
            "・lyrics配列には、実際の歌唱フレーズの日本語（例: 「曖昧3センチ そりゃぷにってことかい」）のみを入れてください。番号やプレースホルダーは絶対に出力しないでください。\n"
            "・1行あたり10〜30文字程度の自然な歌唱フレーズにしてください。\n"
            "・全体の行数は8〜16行程度（1番分）に収めてください。\n"
            "・前置きや解説、Markdown記法は出力しないでください。"
        )

        user_prompt = f"曲名: {song_title}\n\n"
        if search_results_text:
            user_prompt += f"検索結果:\n{search_results_text}\n\n"
            user_prompt += "上記を参考に、この実在する曲の正確な歌詞を行ごとのJSON形式で抽出してください（知らない曲は創作せず found: false にしてください）。"
        else:
            user_prompt += "この曲の正確な歌詞（1番）を行ごとのJSON形式で抽出してください（正確な歌詞を知らない場合は創作せず found: false にしてください）。"

        default_model = str(cfg("OLLAMA_MAIN_MODEL", "") or cfg("OLLAMA_MODEL", "") or "").strip()
        model_name = str(cfg("OLLAMA_MODEL", default_model) or default_model).strip()
        num_predict = int(cfg("OLLAMA_SPECIAL_ACTION_NUM_PREDICT", 1024) or 1024)
        timeout_sec = float(cfg("OLLAMA_TIMEOUT_SEC", 60.0) or 60.0)

        # 3. LLMによるJSON抽出を試行
        try:
            resp = await asyncio.wait_for(
                call_ollama_json(
                    user_prompt,
                    system_prompt=system_prompt,
                    schema=LYRICS_EXTRACTION_SCHEMA,
                    model=model_name,
                    think=False,
                    num_predict=num_predict,
                    temperature=0.2,
                ),
                timeout=timeout_sec,
            )
            if isinstance(resp, dict) and resp.get("found") and isinstance(resp.get("lyrics"), list):
                raw_lines = [str(line).strip() for line in resp["lyrics"] if str(line).strip()]
                cleaned = sanitize_lyrics_lines(raw_lines)
                if len(cleaned) >= 3:
                    return cleaned, bpm
        except Exception as e:
            log.warning("singing llm json extraction failed: %r", e)

        # 4. 検索結果がある場合のみ、テキスト生成で歌詞抽出を再試行（知らない曲の自作ポエム生成を防止）
        if search_results_text:
            try:
                text_prompt = (
                    f"曲「{song_title}」の歌詞:\n{search_results_text}\n\n"
                    "上記からこの曲の歌詞（1番またはサビ）のみを1行1フレーズで出力してください。\n"
                    "前置きや解説、タイトルは一切書かず、本物の歌詞本文だけを改行区切りで出力してください。"
                )
                from ..common.ollama_helpers import call_ollama
                raw_text = await asyncio.wait_for(
                    call_ollama(
                        text_prompt,
                        system_prompt="あなたは歌詞を抽出するアシスタントです。余計な前置きを一切書かず、検索結果にある本物の歌詞のみを1行ずつ出力してください。",
                        model=model_name,
                        think=False,
                        num_predict=num_predict,
                        temperature=0.2,
                    ),
                    timeout=max(timeout_sec * 0.7, 30.0),
                )
                if raw_text:
                    stripped_text = strip_think_tags(raw_text)
                    lines = [line.strip() for line in stripped_text.splitlines() if line.strip()]
                    cleaned = sanitize_lyrics_lines(lines)
                    if len(cleaned) >= 3:
                        return cleaned[:16], bpm
            except Exception as e:
                log.warning("singing llm text fallback failed: %r", e)

        return [], bpm

    async def _sing_song_workflow(
        self,
        channel: Any,
        song_title: str,
        *,
        requester: Any = None,
    ) -> None:
        """歌詞取得・リズムメッセージ送信を実行するバックグラウンドワークフロー。"""
        cid = getattr(channel, "id", None)
        try:
            # 準備中のメッセージ送信
            with contextlib.suppress(Exception):
                await channel.send(f"「{song_title}」だね！ちょっと待ってて、歌詞を思い出すから…♪")

            # 歌詞とテンポの調査・取得
            lyrics_lines, bpm = await self._fetch_lyrics_with_search_and_llm(song_title)

            if not lyrics_lines:
                with contextlib.suppress(Exception):
                    await channel.send(f"ごめんね、「{song_title}」の歌詞が思い出せなかったよ…！")
                return

            # フレーズ化とリズム計算（MuseScore等から取得したBPMを反映）
            full_raw_text = "\n".join(lyrics_lines)
            phrases = parse_lyrics_to_phrases(
                full_raw_text,
                bpm=bpm,
                max_phrases=cfg_int("SINGING_MAX_PHRASES", 14),
                default_min_delay=1.2,
                default_max_delay=4.2,
            )

            if not phrases:
                with contextlib.suppress(Exception):
                    await channel.send(f"ごめんね、「{song_title}」の歌詞がうまく組み立てられなかったよ…！")
                return

            # 開始合図
            await asyncio.sleep(0.8)
            tempo_hint = f"（テンポ: BPM {int(bpm)}♪）" if bpm else ""
            with contextlib.suppress(Exception):
                await channel.send(f"「{song_title}」{tempo_hint}、いくよ〜〜っ！♬")

            await asyncio.sleep(1.5)

            # リズムに合わせて順次送信（先頭に # を付与、音符装飾と動的ディレイ）
            for phrase in phrases:
                line_content = format_singing_line(phrase.text)
                if line_content:
                    # 歌唱中のタイピング演出
                    try:
                        async with channel.typing():
                            await asyncio.sleep(0.3)
                    except Exception:
                        pass
                    await channel.send(line_content)
                # フレーズごとのリズム待機
                await asyncio.sleep(max(phrase.delay_sec - 0.3, 0.8))

            # 歌い終わりの余韻と締め
            await asyncio.sleep(1.2)
            with contextlib.suppress(Exception):
                await channel.send("〜♪（おわりっ！聴いてくれてありがと〜✨）")

        except asyncio.CancelledError:
            log.info("singing workflow in channel %s was cancelled", cid)
            raise
        except Exception as e:
            log.exception("singing workflow failed in channel %s: %s", cid, e)
            with contextlib.suppress(Exception):
                await channel.send("歌っている途中でエラーが発生しちゃった…！")
        finally:
            if cid is not None:
                async with self._singing_lock:
                    self.singing_tasks.pop(cid, None)

    # --- スラッシュコマンド ---
    @app_commands.command(
        name="sing",
        description="指定した曲の歌詞を調べてリズムに合わせて歌います。"
    )
    @app_commands.describe(song="歌ってほしい曲名")
    async def sing_command(self, interaction: discord.Interaction, song: str) -> None:
        self._init_singing_state()
        cid = getattr(interaction, "channel_id", None)
        if cid is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return

        async with self._singing_lock:
            if self.is_singing_active_in_channel(cid):
                await interaction.response.send_message("いま歌っている最中だよ！ちょっと待ってね♪", ephemeral=True)
                return

            await interaction.response.send_message(f"「{song}」を歌う準備をするね！", ephemeral=False)
            task = asyncio.create_task(
                self._sing_song_workflow(interaction.channel, song, requester=interaction.user)
            )
            self.singing_tasks[cid] = task

    @app_commands.command(
        name="sing_stop",
        description="現在歌っている曲を中断します。"
    )
    async def sing_stop_command(self, interaction: discord.Interaction) -> None:
        self._init_singing_state()
        cid = getattr(interaction, "channel_id", None)
        if cid is None:
            await interaction.response.send_message("チャンネル情報を取得できませんでした。", ephemeral=True)
            return

        task = self.singing_tasks.get(cid)
        if task is not None and not task.done():
            task.cancel()
            await interaction.response.send_message("歌うのを中断したよ！", ephemeral=False)
        else:
            await interaction.response.send_message("いまは歌っていないよ！", ephemeral=True)
