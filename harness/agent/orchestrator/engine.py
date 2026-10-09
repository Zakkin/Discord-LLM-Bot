"""AIエージェント・ハーネス オーケストレーター実行エンジン (engine.py)

1-2. オーケストレーター（フロー制御）のコア実装。
ユーザーからのプロンプト受領から最終応答生成までの5段階内部処理フロー：
1. コンテキストの構築 (Context)
2. 推論と計画立案 (Thought)
3. ツール選択と実行 (Action)
4. 結果の観察と評価 (Observation)
5. 最終応答の生成 (Final Response)
を安全に統括・制御する。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..observation import (
    EvaluationVerdict,
    LoopDetector,
    NextStepPhase,
    Observation,
    ObservationEvaluator,
    ObservationFacade,
    ObservationParser,
    ObservationProcessor,
    ObservationRouter,
    ObservationSynthesizer,
    ScratchpadManager,
)
from ..prompt_parser.parser import (
    ParsedToolCall,
    extract_thought_and_action,
    sanitize_generated_reply,
)
from ..test_harness.telemetry import (
    DistributedTracer,
    MetricsCollector,
    SpanKind,
    TelemetryStorage,
)
from ..tool_registry import ToolExecutor, ToolRegistry, get_default_registry
from .llm_client import DefaultLLMClient, LLMClientProtocol
from .types import AgentState, OrchestratorConfig, OrchestratorResult

log = logging.getLogger("harness.agent.orchestrator.engine")


class AgentOrchestrator:
    """5段階フローを自律制御するAIエージェント・オーケストレーター"""

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        tool_registry: ToolRegistry | None = None,
        llm_client: LLMClientProtocol | Any = None,
        tracer: DistributedTracer | None = None,
        metrics: MetricsCollector | None = None,
        storage: TelemetryStorage | None = None,
        observation_facade: ObservationFacade | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or OrchestratorConfig()
        self.tool_registry = tool_registry if tool_registry is not None else get_default_registry()
        self.tool_executor = ToolExecutor()
        if callable(llm_client) and not hasattr(llm_client, "chat"):
            self.llm_client: LLMClientProtocol = DefaultLLMClient(custom_chat_fn=llm_client)
        else:
            self.llm_client = llm_client or DefaultLLMClient()

        # フェーズ4: Observation コンポーネント群 & ファサード
        self.loop_detector = LoopDetector(
            max_identical_calls=2,
            max_consecutive_errors=3,
            max_total_turns=self.config.max_turns,
        )
        self.obs_processor = ObservationProcessor()
        self.obs_evaluator = ObservationEvaluator(loop_detector=self.loop_detector)
        self.obs_synthesizer = ObservationSynthesizer()
        self.obs_router = ObservationRouter()
        self.scratchpad = ScratchpadManager()

        self.observation_facade = observation_facade or ObservationFacade(
            parser=self.obs_processor,
            scratchpad=self.scratchpad,
            loop_detector=self.loop_detector,
            evaluator=self.obs_evaluator,
            router=self.obs_router,
            synthesizer=self.obs_synthesizer,
            max_turns=self.config.max_turns,
        )

        # テストハーネス: テレメトリ基盤
        self.tracer = tracer or DistributedTracer()
        self.metrics = metrics or MetricsCollector()
        self.storage = storage

        self.log = logger or log

    async def run(
        self,
        user_prompt: str,
        system_prompt: str = "",
        context: dict[str, Any] | None = None,
    ) -> OrchestratorResult:
        """ユーザープロンプトを受け取り、5段階フローを実行して最終応答を返す。"""
        start_time = time.perf_counter()
        state = AgentState(user_prompt=user_prompt)
        extra_ctx = dict(context or {})
        self.observation_facade.reset()

        self.log.info(
            "[orchestrator] Starting session task_id=%s max_turns=%d prompt='%s'",
            state.task_id,
            self.config.max_turns,
            user_prompt[:80],
        )

        with self.tracer.span("orchestrator_session", kind=SpanKind.INTERNAL) as session_span:
            session_span.add_event("session_started", {"user_prompt": user_prompt})

            # =================================================================
            # 1. コンテキストの構築（知覚・記憶の引き出し）
            # =================================================================
            with self.tracer.span("phase_1_context", kind=SpanKind.CONTEXT):
                effective_sys_prompt = await self._build_context(system_prompt, user_prompt, extra_ctx)
                state.system_prompt = effective_sys_prompt

                # 初期対話履歴を構築
                if effective_sys_prompt:
                    state.add_message("system", effective_sys_prompt)
                state.add_message("user", user_prompt)

                self.metrics.record_tokens(
                    "context",
                    prompt_tokens=len(effective_sys_prompt.split()) + len(user_prompt.split()),
                    completion_tokens=0,
                )

            # =================================================================
            # 推論・ツール実行・観察の自律ループ (Turns 1..max_turns)
            # =================================================================
            schemas = self.tool_registry.get_schemas() if self.tool_registry else None

            while state.current_turn < self.config.max_turns and not state.is_finished:
                state.current_turn += 1
                turn_idx = state.current_turn
                self.log.info("[orchestrator] --- Beginning Turn #%d ---", turn_idx)

                # -------------------------------------------------------------
                # 2. 推論と計画立案 (Thought)
                # -------------------------------------------------------------
                thought_text = ""
                tool_calls: list[ParsedToolCall] = []
                with self.tracer.span(f"phase_2_thought_turn_{turn_idx}", kind=SpanKind.THOUGHT):
                    self.metrics.start_timer(f"turn_{turn_idx}_thought")

                    llm_response = await self.llm_client.chat(
                        messages=state.messages,
                        tools=schemas,
                        model=self.config.model_name,
                        think=self.config.think,
                        temperature=self.config.temperature,
                        timeout_sec=self.config.timeout_sec,
                    )

                    self.metrics.stop_timer(f"turn_{turn_idx}_thought", phase="thought")

                    # レスポンスのパース
                    raw_reply = ""
                    msg_obj = llm_response.get("message") or {}
                    if isinstance(msg_obj, dict):
                        raw_reply = str(msg_obj.get("content") or "")
                        # Function Callingの tool_calls が直接返ってきた場合のパース
                        if "tool_calls" in msg_obj and isinstance(msg_obj["tool_calls"], list):
                            from ..prompt_parser.parser.actions import ParsedToolCall
                            for tc in msg_obj["tool_calls"]:
                                fn = tc.get("function") or {}
                                tool_calls.append(
                                    ParsedToolCall(
                                        name=str(fn.get("name") or ""),
                                        arguments=fn.get("arguments") or {},
                                        call_id=str(tc.get("id") or ""),
                                    )
                                )

                    # 出力テキストから思考とアクションを抽出
                    parsed_thought, parsed_actions, clean_content = extract_thought_and_action(
                        raw_reply,
                        registry=self.tool_registry,
                    )
                    thought_text = parsed_thought
                    if not tool_calls:
                        tool_calls = parsed_actions

                    self.log.info(
                        "[orchestrator] Thought parsed: calls=%d thought_len=%d",
                        len(tool_calls),
                        len(thought_text),
                    )

                # ツール呼び出しがない場合は、直接フェーズ5へ進む
                if not tool_calls:
                    self.log.info("[orchestrator] No tool calls required. Moving to final response.")
                    state.final_response = clean_content or raw_reply
                    state.is_finished = True
                    break

                # -------------------------------------------------------------
                # 3. ツール選択と実行 (Action)
                # -------------------------------------------------------------
                executed_results: list[Any] = []
                with self.tracer.span(f"phase_3_action_turn_{turn_idx}", kind=SpanKind.ACTION):
                    # アシスタントの呼び出し表明を履歴に追加
                    state.add_message("assistant", raw_reply)

                    for call in tool_calls:
                        tool_def = self.tool_registry.get(call.name) if self.tool_registry else None
                        if tool_def is None:
                            self.log.warning("[orchestrator] Tool '%s' not found in registry", call.name)
                            from ..tool_registry.types import ToolResult
                            tool_res = ToolResult(
                                name=call.name,
                                success=False,
                                error=f"Tool '{call.name}' not found",
                            )
                        else:
                            tc_obj = call.to_tool_call()
                            if tc_obj is None:
                                from ..tool_registry.types import ToolCall
                                tc_obj = ToolCall(name=call.name, arguments=call.arguments, call_id=call.call_id)
                            tool_res = await self.tool_executor.execute(tool_def, tc_obj)

                        executed_results.append((call, tool_res))

                # -------------------------------------------------------------
                # 4. 結果の観察と評価 (Observation)
                # -------------------------------------------------------------
                with self.tracer.span(f"phase_4_observation_turn_{turn_idx}", kind=SpanKind.OBSERVATION):
                    routing_res = self.observation_facade.handle_turn_observations(
                        executed_results=executed_results,
                        messages=state.messages,
                        observations_history=state.observations,
                        user_intent=user_prompt,
                        turn_index=turn_idx,
                        max_turns=self.config.max_turns,
                    )

                    if routing_res.next_phase == NextStepPhase.FATAL_ERROR:
                        self.log.warning("[orchestrator] Fatal error/loop abort: %s", routing_res.error_message)
                        state.error = routing_res.error_message
                        state.is_finished = True
                        break
                    elif routing_res.next_phase == NextStepPhase.PROCEED_TO_FINAL:
                        self.log.info("[orchestrator] All observation goals achieved in turn #%d", turn_idx)
                        break
                    elif routing_res.next_phase == NextStepPhase.RETRY_WITH_THOUGHT:
                        self.log.info("[orchestrator] Looping back to Phase 2 (Thought) with feedback.")

                    # コンテキスト長の安全なトリミング（スクラッチパッドマネージャー）
                    self.scratchpad.trim_history_window(state.messages)

            # =================================================================
            # 5. 最終応答の生成（Final Response）
            # =================================================================
            with self.tracer.span("phase_5_final_response", kind=SpanKind.FINAL_RESPONSE):
                if state.error:
                    self.log.warning("[orchestrator] Halting session with error: %s", state.error)
                    if not state.final_response:
                        state.final_response = f"申し訳ありません。タスクの実行中に修復不能なエラーまたは安全停止が発生しました: {state.error}"
                else:
                    final_text = await self._generate_final_response(state)
                    # サニタイズ
                    cleaned_final = sanitize_generated_reply(final_text)
                    state.final_response = cleaned_final

                    # メモリ保存（Prune/Save）
                    if self.config.auto_store_memory and extra_ctx.get("memory_store"):
                        await self._save_memory(user_prompt, cleaned_final, extra_ctx, state)

            session_span.add_event("session_completed", {"success": state.error is None})

        # テレメトリ永続化
        if self.storage:
            self.storage.persist_spans(self.tracer.get_spans())
            self.storage.persist_metrics_summary(self.metrics.get_summary(), trace_id=self.tracer.trace_id)

        elapsed_total = time.perf_counter() - start_time
        self.log.info(
            "[orchestrator] Session finished in %.2fs turns=%d obs=%d success=%s",
            elapsed_total,
            state.current_turn,
            len(state.observations),
            state.error is None,
        )

        return OrchestratorResult(
            success=state.error is None,
            final_response=state.final_response or "",
            total_turns=state.current_turn,
            observations=state.observations,
            trace_id=self.tracer.trace_id,
            error=state.error,
            telemetry_summary=self.metrics.get_summary(),
            metadata=state.metadata,
        )

    async def _build_context(
        self,
        system_prompt: str,
        user_prompt: str,
        extra_ctx: dict[str, Any],
    ) -> str:
        """システムプロンプト、短期記憶、長期記憶（RAG）を合成"""
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt.strip())

        # ツールカタログの統合（利用可能なツール一覧のガイダンス）
        if self.tool_registry and hasattr(self.tool_registry, "render_catalog"):
            catalog = self.tool_registry.render_catalog()
            if catalog:
                parts.append(f"【利用可能なツールカタログ】\n{catalog}")

        store = extra_ctx.get("memory_store")
        persona = str(extra_ctx.get("persona") or "assistant")
        channel_id = extra_ctx.get("channel_id")
        user_id = extra_ctx.get("user_id")

        # 長期記憶/RAGコンテキストの動的読み込み (Read)
        memories = list(extra_ctx.get("memories") or [])
        if store and user_prompt:
            try:
                if hasattr(store, "search_memories_hybrid"):
                    records = await store.search_memories_hybrid(
                        query=user_prompt,
                        persona=persona,
                        channel_id=channel_id,
                        user_id=user_id,
                        limit=5,
                    )
                    for r in records:
                        c = r.get("content") if isinstance(r, dict) else getattr(r, "content", "")
                        if c and c not in memories:
                            memories.append(c)
                elif hasattr(store, "get_recent_user_memories") and user_id is not None:
                    records = await store.get_recent_user_memories(
                        persona=persona,
                        user_id=user_id,
                        limit=5,
                    )
                    for r in records:
                        c = r.get("content") if isinstance(r, dict) else getattr(r, "content", "")
                        if c and c not in memories:
                            memories.append(c)
            except Exception as e:
                self.log.warning("[orchestrator] Error reading memories from store: %s", e)

        if memories:
            parts.append("【関連する記憶・マニュアル情報】")
            for m in memories:
                parts.append(f"- {m}")

        # 会話履歴・チャンネル要約（短期記憶）の動的読み込み
        history_summary = extra_ctx.get("channel_summary")
        if not history_summary and store and channel_id is not None:
            try:
                if hasattr(store, "get_channel_summary"):
                    s_rec = await store.get_channel_summary(
                        persona=persona,
                        channel_id=channel_id,
                    )
                    if s_rec:
                        history_summary = s_rec.get("summary") if isinstance(s_rec, dict) else getattr(s_rec, "summary", "")
            except Exception as e:
                self.log.warning("[orchestrator] Error reading channel summary from store: %s", e)

        if history_summary:
            parts.append(f"【直前の会話要約】\n{history_summary}")

        return "\n\n".join(parts)

    async def _generate_final_response(self, state: AgentState) -> str:
        """観察結果を元に最終返信テキストを生成"""
        if state.final_response and not state.observations:
            return state.final_response

        # 観察結果（Verified Facts）を合成
        verified_facts = self.obs_synthesizer.synthesize_for_final_response(state.observations)

        if not verified_facts and state.final_response:
            return state.final_response

        final_prompt_messages = list(state.messages)
        if verified_facts:
            final_prompt_messages.append({
                "role": "user",
                "content": f"{verified_facts}\n上記の確定事実に基づき、ユーザーの質問や指示に対して自然で親切な最終回答を日本語で作成してください。",
            })

        res = await self.llm_client.chat(
            messages=final_prompt_messages,
            model=self.config.model_name,
            think=False,
            temperature=self.config.temperature,
            timeout_sec=self.config.timeout_sec,
        )
        msg_obj = res.get("message") or {}
        return str(msg_obj.get("content") or "")

    async def _save_memory(
        self,
        prompt: str,
        reply: str,
        context: dict[str, Any],
        state: AgentState | None = None,
    ) -> None:
        """会話の要約・記憶をメモリに保存（Prune/Save）"""
        store = context.get("memory_store")
        if not store:
            return

        persona = str(context.get("persona") or "assistant")
        guild_id = context.get("guild_id")
        channel_id = context.get("channel_id")
        user_id = context.get("user_id")

        try:
            # 1. Bot発話の記録（オウム返し防止・文脈参照用）
            if hasattr(store, "add_bot_utterance"):
                await store.add_bot_utterance(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    content=reply,
                )

            # 2. 観察結果（確定事実: Verified Facts）の合成と長期記憶への定着
            obs_list = state.observations if state else []
            verified_facts = ""
            if obs_list:
                verified_facts = self.obs_synthesizer.synthesize_for_final_response(obs_list)

            # 長期記憶として残すコンテンツの構築
            memory_content = ""
            if verified_facts:
                memory_content = f"Q: {prompt[:80]} -> 事実: {verified_facts[:200]}"
            elif reply:
                memory_content = f"Q: {prompt[:80]} -> A: {reply[:150]}"

            if memory_content and hasattr(store, "add_memory"):
                await store.add_memory(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    memory_type="fact",
                    content=memory_content,
                    score=0.7,
                )

            # 3. エピソード記憶の記録（ツール実行を伴うタスクの場合）
            if hasattr(store, "add_episode") and obs_list:
                tool_names = [o.tool_name for o in obs_list]
                await store.add_episode(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    topic=prompt[:60],
                    event_summary=f"ツール実行 ({', '.join(tool_names)}): {verified_facts[:100]}",
                    bot_action=reply[:100],
                    outcome="success",
                )

            self.log.info(
                "[orchestrator] Successfully saved interaction memory to MemoryStore (facts=%s, obs=%d)",
                bool(verified_facts),
                len(obs_list),
            )
        except Exception as e:
            self.log.warning("[orchestrator] Failed to save memory: %s", e)
