"""AIエージェント・ハーネス 画像検索ツールアダプター (adapters/image_search.py)

画像URLから逆画像検索API（SauceNAO / trace.moe）および自前ビジョン検証を用いて、
写っている人物・キャラクター名・作品名・元ネタ・出典を客観的・正直に特定する ToolDefinition を提供する。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import urllib.parse
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from ..types import ToolCategory, ToolDefinition, ToolParameter, ToolResult

log = logging.getLogger("harness.agent.tool_registry.adapters.image_search")

_IMAGE_URL_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp|gif|bmp)(?:\?.*)?$", re.IGNORECASE)
_DISCORD_CDN_RE = re.compile(
    r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/attachments/[^\s<>\"']+",
    re.IGNORECASE,
)


def _get_cfg(key: str, default: Any = None) -> Any:
    try:
        from ollama_bot.common.config_helpers import cfg

        return cfg(key, default)
    except Exception:
        return default


def extract_image_url_from_context(context: dict[str, Any] | Any = None, **kwargs: Any) -> str:
    """コンテキストや kwargs から最も関連性の高い画像URLを安全に抽出する。"""
    candidates: list[str] = []

    # kwargs からの直接取得
    for key in (
        "image_url",
        "target_image_url",
        "recent_image_url",
        "last_image_url",
        "attachment_url",
        "url",
    ):
        val = kwargs.get(key)
        if val and isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")):
            candidates.append(val.strip())

    # context 辞書からの取得
    if isinstance(context, dict):
        for key in (
            "image_url",
            "target_image_url",
            "recent_image_url",
            "last_image_url",
            "attachment_url",
            "url",
        ):
            val = context.get(key)
            if val and isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")):
                candidates.append(val.strip())

        # attachments / images リスト
        atts = context.get("attachments") or context.get("images")
        if isinstance(atts, (list, tuple)):
            for item in atts:
                if isinstance(item, str) and (item.startswith("http://") or item.startswith("https://")):
                    candidates.append(item.strip())
                elif isinstance(item, dict) and item.get("url"):
                    candidates.append(str(item["url"]).strip())
                elif hasattr(item, "url") and getattr(item, "url"):
                    candidates.append(str(getattr(item, "url")).strip())

        # テキスト内の Discord CDN / 画像URL探索
        for text_key in ("user_text", "prompt", "context_text", "last_message", "message"):
            txt = str(context.get(text_key) or "")
            if not txt:
                continue
            found_discord = _DISCORD_CDN_RE.findall(txt)
            candidates.extend(found_discord)
            for u in re.findall(r"https?://[^\s<>\"']+", txt):
                if _IMAGE_URL_EXT_RE.search(u):
                    candidates.append(u.strip())

    # 画像特有URL（Discord CDNまたは画像拡張子）を最優先
    for cand in candidates:
        if _DISCORD_CDN_RE.search(cand) or _IMAGE_URL_EXT_RE.search(cand):
            return cand

    # それ以外の http/https URL
    for cand in candidates:
        if cand.startswith("http://") or cand.startswith("https://"):
            return cand

    return ""


async def _download_image_as_b64(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_sec: float = 8.0,
) -> str:
    """URLから画像をダウンロードして Base64 文字列を返す。失敗時は空文字。"""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return ""
    try:
        if session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return base64.b64encode(data).decode("utf-8")
        else:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as local_session:
                async with local_session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        log.debug("[image_search] Failed to download image for b64: url='%s', err=%r", url, e)
    return ""


async def fetch_saucenao_candidates(
    image_url: str,
    session: aiohttp.ClientSession,
    api_key: str = "",
    num_results: int = 6,
) -> list[dict[str, Any]]:
    """SauceNAO API を呼び出して正規化済み候補リストを返す。"""
    if not api_key:
        log.debug("[image_search] SAUCENAO_API_KEY is not configured, skipping SauceNAO.")
        return []

    endpoint = "https://saucenao.com/search.php"
    params: dict[str, Any] = {
        "output_type": 2,
        "numres": num_results,
        "url": image_url,
        "api_key": api_key,
    }

    try:
        async with session.get(endpoint, params=params) as resp:
            if resp.status != 200:
                log.warning("[image_search] SauceNAO HTTP error: status=%d", resp.status)
                return []
            data = await resp.json()
    except Exception as e:
        log.warning("[image_search] SauceNAO fetch error: %r", e)
        return []

    results = data.get("results", [])
    if not isinstance(results, list):
        return []

    candidates: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        header = item.get("header", {})
        data_block = item.get("data", {})
        if not isinstance(header, dict) or not isinstance(data_block, dict):
            continue

        try:
            similarity = float(header.get("similarity", 0.0))
        except (ValueError, TypeError):
            similarity = 0.0

        title = (
            data_block.get("title")
            or data_block.get("material")
            or data_block.get("source")
            or data_block.get("jp_name")
            or data_block.get("eng_name")
            or ""
        )
        creator = (
            data_block.get("member_name")
            or data_block.get("author_name")
            or data_block.get("creator")
            or data_block.get("artist")
            or data_block.get("author")
            or ""
        )

        source_url = ""
        ext_urls = data_block.get("ext_urls", [])
        if isinstance(ext_urls, list) and ext_urls:
            pixiv_url = next((u for u in ext_urls if isinstance(u, str) and "pixiv.net" in u), None)
            source_url = pixiv_url if pixiv_url else str(ext_urls[0])
        elif isinstance(ext_urls, str):
            source_url = ext_urls

        thumbnail_url = str(header.get("thumbnail") or "").strip()

        candidates.append({
            "source": "saucenao",
            "title": str(title).strip(),
            "creator": str(creator).strip(),
            "source_url": source_url.strip(),
            "similarity": similarity,
            "thumbnail_url": thumbnail_url,
            "extra": {
                "index_name": header.get("index_name", ""),
                "index_id": header.get("index_id"),
            },
            "verdict": "unverified",
            "confidence": 0,
            "reason": "",
            "verified": False,
        })

    return candidates


async def fetch_tracemoe_candidates(
    image_url: str,
    session: aiohttp.ClientSession,
) -> list[dict[str, Any]]:
    """trace.moe API を呼び出して正規化済み候補リストを返す。"""
    endpoint = f"https://api.trace.moe/search?cutBorders&anilistInfo=1&url={urllib.parse.quote(image_url, safe='')}"

    try:
        async with session.get(endpoint) as resp:
            if resp.status != 200:
                log.warning("[image_search] trace.moe HTTP error: status=%d", resp.status)
                return []
            data = await resp.json()
    except Exception as e:
        log.warning("[image_search] trace.moe fetch error: %r", e)
        return []

    results = data.get("result", [])
    if not isinstance(results, list):
        return []

    candidates: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue

        try:
            similarity = round(float(item.get("similarity", 0.0)) * 100.0, 2)
        except (ValueError, TypeError):
            similarity = 0.0

        anilist = item.get("anilist", {})
        title = ""
        anilist_id: Any = None
        if isinstance(anilist, dict):
            anilist_id = anilist.get("id")
            title_dict = anilist.get("title", {})
            if isinstance(title_dict, dict):
                title = (
                    title_dict.get("native")
                    or title_dict.get("romaji")
                    or title_dict.get("english")
                    or ""
                )
            elif isinstance(title_dict, str):
                title = title_dict
        elif isinstance(anilist, int) and not isinstance(anilist, bool):
            anilist_id = anilist
            title = ""
        elif isinstance(anilist, str):
            title = anilist

        source_url = f"https://anilist.co/anime/{anilist_id}" if anilist_id else ""
        thumbnail_url = str(item.get("image") or "").strip()
        episode = item.get("episode")
        from_sec = item.get("from", 0.0)
        try:
            from_f = float(from_sec)
            m = int(from_f // 60)
            s = int(from_f % 60)
            time_str = f"{m}:{s:02d}"
        except (ValueError, TypeError):
            from_f = 0.0
            time_str = "0:00"

        candidates.append({
            "source": "tracemoe",
            "title": str(title).strip(),
            "creator": "",
            "source_url": source_url,
            "similarity": similarity,
            "thumbnail_url": thumbnail_url,
            "extra": {
                "episode": episode,
                "from_sec": from_f,
                "time_str": time_str,
                "anilist_id": anilist_id,
            },
            "verdict": "unverified",
            "confidence": 0,
            "reason": "",
            "verified": False,
        })

    return candidates


def _build_result_text(
    candidates: list[dict[str, Any]],
    image_url: str,
    lens_url: str,
    clues: list[str] | None = None,
) -> str:
    """正規化・判定済み候補リストから正直で客観的な結果テキストを組み立てる。"""
    best_match = next((c for c in candidates if c.get("verdict") == "match"), None)
    partial_matches = [c for c in candidates if c.get("verdict") == "partial"]

    lines: list[str] = ["### 🔍 画像検索・特定結果"]
    lines.append(f"- **対象画像**: {image_url}")

    if best_match:
        lines.append("\n**【特定結果】**")
        title = best_match.get("title") or "（タイトル不明）"
        lines.append(f"- **作品・タイトル**: {title}")
        if best_match.get("creator"):
            lines.append(f"- **作者・投稿者**: {best_match['creator']}")
        if best_match.get("source_url"):
            lines.append(f"- **出典**: <{best_match['source_url']}>")
        lines.append(f"- **類似度**: {best_match.get('similarity', 0.0):.1f}% ({best_match.get('source', '')})")
        if best_match.get("extra", {}).get("episode") is not None:
            extra = best_match["extra"]
            lines.append(f"- **該当箇所**: 第{extra.get('episode')}話 ({extra.get('time_str', '')})")
        reason = str(best_match.get("reason") or "").strip()
        if not best_match.get("verified", False):
            if reason:
                reason = f"{reason}（ビジョン未検証）"
            else:
                reason = "（ビジョン未検証）"
        if reason:
            lines.append(f"- **判定理由**: {reason}")

    elif partial_matches:
        lines.append("\n**【候補（未確認）】** (※確定ではありません)")
        for idx, cand in enumerate(partial_matches[:3], start=1):
            title = cand.get("title") or "（名称不明）"
            sim = cand.get("similarity", 0.0)
            src = cand.get("source", "")
            creator_str = f" / 作者: {cand['creator']}" if cand.get("creator") else ""
            lines.append(f"{idx}. **{title}** (類似度: {sim:.1f}%, ソース: {src}{creator_str})")
            if cand.get("source_url"):
                lines.append(f"   - 出典: <{cand['source_url']}>")
            if cand.get("reason"):
                lines.append(f"   - 備考: {cand['reason']}")

    else:
        lines.append("\n**特定できませんでした。**")
        lines.append("画像に対応する高精度な一致・候補は見つかりませんでした。")
        if clues:
            lines.append("\n**【未確認の手がかり】**:")
            for clue in clues:
                lines.append(f"- {clue}")

    lines.append(f"\n*(手動確認用 Google Lens: <{lens_url}>)*")
    return "\n".join(lines)


class ImageSearchToolAdapter:
    """画像検索ツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "search_image", timeout_sec: float = 40.0) -> ToolDefinition:
        """search_image ツール定義を作成する（デフォルトタイムアウト: 40秒）"""
        return ToolDefinition(
            name=name,
            description=(
                "画像URLを指定して逆画像検索を行い、写っている人物・キャラクター名・作品名・元ネタ・類似画像・出典Webページを客観的に調査します。"
                "添付画像や直前の画像について『これ誰？』『何のキャラ？』『元ネタは何？』と質問された場合に使用してください。"
            ),
            parameters=[
                ToolParameter(
                    name="image_url",
                    type="string",
                    description="検索対象の画像Web URL（http/https）。省略時は直近の添付画像や文脈上の画像が自動適用されます。",
                    required=False,
                ),
                ToolParameter(
                    name="query",
                    type="string",
                    description="追加の絞り込みキーワードや質問（キャラクター名、推測される特徴など。任意）",
                    required=False,
                ),
            ],
            handler=cls.execute_image_search,
            category=ToolCategory.WEB,
            tags={"web", "search", "image", "research"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    async def execute_image_search(
        cls,
        image_url: str = "",
        query: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """画像検索ツールの実行エントリーポイント"""
        resolved_url = (image_url or "").strip()
        if not resolved_url:
            resolved_url = extract_image_url_from_context(kwargs.get("context"), **kwargs)

        clean_query = str(query or "").strip()
        log.info("[IMAGE_SEARCH] execute_image_search: url='%s', query='%s'", resolved_url, clean_query)

        if not resolved_url:
            # 画像URLが見つからない場合、クエリがあればWeb検索へフォールバック
            if clean_query and not (clean_query.startswith("http://") or clean_query.startswith("https://")):
                from ..web_research import search_web_entries_unified

                entries = await search_web_entries_unified(clean_query, max_results=3)
                if entries:
                    lines = [
                        f"- [{e.get('title', '無題')}](<{e.get('url', '')}>): {e.get('snippet', '')}"
                        for e in entries
                    ]
                    out_text = (
                        f"画像URLが指定されていないため、キーワード '{clean_query}' でWeb検索しました:\n"
                        + "\n".join(lines)
                    )
                    log.info("[IMAGE_SEARCH] execute_image_search finished: success=True, source='web_search'")
                    return ToolResult(
                        name="search_image",
                        success=True,
                        output=out_text,
                        metadata={"fallback": "web_search", "query": clean_query},
                    )
            log.info("[IMAGE_SEARCH] execute_image_search finished: success=False, source='no_image_url'")
            return ToolResult(
                name="search_image",
                success=False,
                output="画像URLが指定されておらず、直前の会話や添付ファイルからも有効な画像URLを特定できませんでした。画像URLを指定するか画像を添付してください。",
                metadata={"error": "no_image_url"},
            )

        res = await cls.search_image_unified(resolved_url, query=clean_query, **kwargs)
        log.info(
            "[IMAGE_SEARCH] execute_image_search finished: success=%s, source='%s'",
            res.get("success"),
            res.get("source"),
        )
        return ToolResult(
            name="search_image",
            success=res.get("success", False),
            output=res.get("text", "検索結果を取得できませんでした。"),
            metadata=res,
        )

    @classmethod
    async def search_image_unified(
        cls,
        image_url: str,
        query: str = "",
        *,
        vision_describe: Optional[Callable[[str], Awaitable[str]]] = None,
        vision_verify: Optional[Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]]] = None,
        timeout_sec: float = 35.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """逆画像検索API（SauceNAO / trace.moe）とビジョン検証による高精度特定"""
        log.info("[IMAGE_SEARCH] Starting unified image search: url='%s', query='%s'", image_url, query)

        # 1. 対象画像URLバリデーション
        clean_url = (image_url or "").strip()
        if not clean_url or not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            return {
                "success": False,
                "source": "invalid_url",
                "text": "有効な画像URLが指定されていません。",
                "candidates": [],
                "lens_url": "",
                "raw_chars": 0,
            }

        encoded_url = urllib.parse.quote(clean_url, safe="")
        lens_url = f"https://lens.google.com/uploadbyurl?url={encoded_url}"

        # 2. 候補収集（SauceNAO & trace.moe 並行呼び出し）
        api_timeout = float(_get_cfg("IMAGE_SEARCH_API_TIMEOUT_SEC", 15.0) or 15.0)
        saucenao_key = str(_get_cfg("SAUCENAO_API_KEY", "") or "").strip()
        max_candidates = int(_get_cfg("IMAGE_SEARCH_MAX_CANDIDATES", 5) or 5)

        candidates: list[dict[str, Any]] = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=api_timeout)) as session:
                tasks = [
                    fetch_saucenao_candidates(clean_url, session, api_key=saucenao_key),
                    fetch_tracemoe_candidates(clean_url, session),
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for item_res in results:
                    if isinstance(item_res, list):
                        candidates.extend(item_res)
                    elif isinstance(item_res, Exception):
                        log.warning("[IMAGE_SEARCH] Candidate fetch error: %r", item_res)
        except Exception as e:
            log.warning("[IMAGE_SEARCH] Session error during candidate fetch: %r", e)

        # 類似度降順ソート＆件数上限
        candidates.sort(key=lambda c: float(c.get("similarity", 0.0)), reverse=True)
        candidates = candidates[:max_candidates]

        # 3. 強い一致の近道判定（および基本判定）
        for cand in candidates:
            cand.setdefault("verified", False)
            sim = float(cand.get("similarity", 0.0))
            src = cand.get("source")
            title = str(cand.get("title") or "").strip()
            if (src == "saucenao" and sim >= 85.0) or (src == "tracemoe" and sim >= 87.0):
                if not title:
                    cand["verdict"] = "partial"
                    cand["confidence"] = int(sim)
                    cand["reason"] = "タイトル不明のため候補扱い"
                else:
                    cand["verdict"] = "match"
                    cand["confidence"] = int(sim)
                    cand["reason"] = "API高類似度一致"
            elif (src == "saucenao" and sim >= 60.0) or (src == "tracemoe" and sim >= 65.0):
                cand["verdict"] = "partial"
                cand["confidence"] = int(sim)
                cand["reason"] = "API中類似度候補"
            else:
                cand["verdict"] = "no"
                cand["confidence"] = int(sim)
                cand["reason"] = "類似度基準未満"

        # 4. 自前ビジョン検証（vision_verify が渡されている場合）
        verify_enabled = bool(_get_cfg("IMAGE_SEARCH_VISION_VERIFY_ENABLED", True))
        verify_top_n = int(_get_cfg("IMAGE_SEARCH_VERIFY_TOP_N", 3) or 3)

        if verify_enabled and vision_verify is None:
            log.warning("[IMAGE_SEARCH] vision_verify not provided by caller; verification skipped")

        if vision_verify is not None and verify_enabled and candidates:
            target_candidates = candidates[:verify_top_n]
            try:
                target_b64 = await _download_image_as_b64(clean_url)
                cand_b64_tasks = [_download_image_as_b64(c.get("thumbnail_url", "")) for c in target_candidates]
                cand_b64s = await asyncio.gather(*cand_b64_tasks, return_exceptions=False)

                if target_b64 and cand_b64s:
                    verify_results = await vision_verify(target_b64, cand_b64s)
                    if isinstance(verify_results, list):
                        for idx, v_res in enumerate(verify_results):
                            if idx < len(target_candidates) and isinstance(v_res, dict):
                                target_candidates[idx]["verdict"] = v_res.get(
                                    "verdict", target_candidates[idx]["verdict"]
                                )
                                target_candidates[idx]["confidence"] = int(
                                    v_res.get("confidence", target_candidates[idx]["confidence"])
                                )
                                target_candidates[idx]["reason"] = str(
                                    v_res.get("reason", target_candidates[idx]["reason"])
                                )[:30]
                                target_candidates[idx]["verified"] = True
            except Exception as e:
                log.warning("[IMAGE_SEARCH] vision_verify failed: %r", e)

            # 未検証 match の格下げ（本丸）:
            # vision_verify が提供され検証が有効な場合、ステップ4の終了時点で、
            # 検証の対象にならなかった候補（上位 IMAGE_SEARCH_VERIFY_TOP_N 件の外側）や
            # 検証結果が書き込まれなかった候補に残っている match 判定は、
            # すべて partial に格下げし、理由を「検証対象外のため候補扱い」とする。
            for cand in candidates:
                if cand.get("verdict") == "match" and not cand.get("verified", False):
                    cand["verdict"] = "partial"
                    cand["reason"] = "検証対象外のため候補扱い"

        # 5. 同定フォールバック（候補なし または 全て no 判定時）
        clues: list[str] = []
        has_any_match = any(c.get("verdict") in ("match", "partial") for c in candidates)

        if not has_any_match:
            desc_text = ""
            if vision_describe is not None:
                try:
                    desc_text = await vision_describe(clean_url)
                except Exception as e:
                    log.warning("[IMAGE_SEARCH] vision_describe failed: %r", e)

            if desc_text:
                clues.append(f"画像の特徴: {desc_text.strip()}")
                try:
                    from ..web_research import search_web_entries_unified

                    search_query = desc_text.splitlines()[0][:80] if desc_text else ""
                    if search_query:
                        entries = await search_web_entries_unified(search_query, max_results=3)
                        for entry in entries:
                            clues.append(f"Web手がかり: [{entry.get('title', '無題')}](<{entry.get('url', '')}>)")
                except Exception as e:
                    log.debug("[IMAGE_SEARCH] fallback web search failed: %r", e)

        # 6. 正直な結果テキスト（text）の組み立て
        result_text = _build_result_text(candidates, clean_url, lens_url, clues=clues)

        # 7. 戻り値
        best_match = next((c for c in candidates if c.get("verdict") == "match"), None)
        partial_matches = [c for c in candidates if c.get("verdict") == "partial"]
        success = True if (best_match or partial_matches) else False

        source_str = "failed"
        if best_match:
            source_str = best_match.get("source", "matched")
        elif partial_matches:
            source_str = "partial_candidates"

        res = {
            "success": success,
            "source": source_str,
            "text": result_text,
            "candidates": candidates,
            "lens_url": lens_url,
            "raw_chars": len(result_text),
        }
        log.info(
            "[IMAGE_SEARCH] Unified image search completed: success=%s, source='%s', candidates=%d",
            success,
            source_str,
            len(candidates),
        )
        return res
