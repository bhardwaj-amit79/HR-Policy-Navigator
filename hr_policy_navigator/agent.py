"""Planner -> Executor -> Critic agent over the HR policy corpus.

Planner   : (LLM, schema-validated; heuristic fallback) classify intent, resolve follow-ups,
            split multi-part questions.
Executor  : tool loop (search_policy / read_section / escalate_to_human) with a step cap,
            duplicate-call loop detection, weak-retrieval reformulation, tool-error retry.
Generator : one of three prompting strategies (structured | selfconsistency | decomposition).
Critic    : validates schema, source ids, per-claim lexical support (numbers must appear in
            evidence); one revision round with expanded context, then drops unsupported claims;
            abstains if nothing grounded remains.
"""
from __future__ import annotations

import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings
from .guardrails import REFUSAL_TEXT, redact_pii, sanitize_chunk, screen_query
from .llm import Budget, BudgetExceeded, JSONFailure, LLMClient, LLMError
from .prompts import ANSWER_SCHEMA, PLAN_SCHEMA, PLANNER_SYSTEM, answer_messages_personalized
from .retrieval import PolicyCorpus
from .state import SessionStore
from .text_utils import tokenize
from .tools import ToolRegistry, build_tools
from .tracing import Tracer

SUPPORT_THRESHOLD = 0.6
WEAK_RETRIEVAL = 0.22


def claim_support(claim: str, evidence_text: str) -> float:
    """Fraction of the claim's content tokens present in the cited evidence; any number in the
    claim that is absent from the evidence caps the score (hallucinated figures)."""
    ct, et = set(tokenize(claim)), set(tokenize(evidence_text))
    if not ct:
        return 1.0
    score = len(ct & et) / len(ct)
    nums = {t for t in ct if any(ch.isdigit() for ch in t)}
    if nums and not nums <= et:
        score = min(score, 0.3)
    return score


def check_claims(claims: List[dict], evidence: Dict[str, dict]) -> Dict[str, Any]:
    bad_ids, unsupported, ok = [], [], []
    for c in claims:
        ids = c.get("sources", [])
        missing = [i for i in ids if i not in evidence]
        if missing:
            bad_ids.append({"claim": c.get("text", ""), "missing": missing})
            continue
        best = max((claim_support(c.get("text", ""), evidence[i]["text"]) for i in ids), default=0.0)
        (ok if best >= SUPPORT_THRESHOLD else unsupported).append({"claim": c.get("text", ""), "support": round(best, 2)})
    return {"supported": ok, "unsupported": unsupported, "invalid_source_ids": bad_ids,
            "all_ok": not unsupported and not bad_ids}


class Copilot:
    def __init__(self, corpus: PolicyCorpus, settings: Optional[Settings] = None, llm: Optional[LLMClient] = None,
                 tracer: Optional[Tracer] = None, store: Optional[SessionStore] = None):
        self.s = settings or Settings()
        self.corpus = corpus
        if llm is None and self.s.has_model:
            llm = LLMClient(self.s.base_url, self.s.api_key, self.s.model, self.s.max_tokens, self.s.constrained_mode, self.s.json_retries, no_think=self.s.no_think)
        self.llm = llm
        self.tracer = tracer or Tracer(self.s.trace_dir, {"public_key": self.s.langfuse_public_key,
                                                          "secret_key": self.s.langfuse_secret_key, "host": self.s.langfuse_host})
        self.store = store or SessionStore(self.s.state_dir)
        self.tools: ToolRegistry = build_tools(corpus, Path(self.s.state_dir) / "handoffs.jsonl", check_claims)

    # ------------------------------------------------------------------ public API
    def run(self, query: str, session_id: str = "default", strategy: Optional[str] = None,
            user_profile: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        """Run a query. user_profile may carry {user_id, role, tenure, team_id, name}."""
        strategy = strategy or self.s.strategy
        profile = user_profile or {}
        trace = self.tracer.start(
            "query",
            query=redact_pii(query)[0],
            strategy=strategy,
            session=session_id,
            user_id=profile.get("user_id", ""),
            user_role=profile.get("role", ""),
            user_tenure=profile.get("tenure", ""),
        )
        t0 = time.perf_counter()
        try:
            result = self._run(query, session_id, strategy, trace, profile)
        except BudgetExceeded as e:
            result = self._result("error_budget", f"I ran out of my processing budget for this question ({e}). Please ask a narrower question.")
        except LLMError as e:
            result = self._result("error_model", f"The language-model endpoint failed: {e}. Please retry.")
        except Exception as e:  # noqa: BLE001 - last-resort guard, never crash the UI
            result = self._result("error_internal", f"Unexpected error: {type(e).__name__}.")
            result["failure_class"] = "tool_call_error"
        c = trace.data["counters"]
        cost = (c["prompt_tokens"] * self.s.price_in + c["completion_tokens"] * self.s.price_out) / 1e6
        result.update(trace_id=trace.id, latency_ms=round((time.perf_counter() - t0) * 1000, 1), tool_calls=c["tool_calls"],
                      llm_calls=c["llm_calls"], tokens=c["prompt_tokens"] + c["completion_tokens"], cost_usd=round(cost, 6),
                      json_repairs=c["json_repairs"], tool_errors=c["tool_errors"])
        trace.end(status=result["status"], answer=result["answer"][:500], citations=[x["path"] for x in result.get("citations", [])])
        return result

    # ---------------------------------------------------------------- orchestration
    def _run(self, raw_query: str, sid: str, strategy: str, trace, profile: Dict[str, Any] = None) -> Dict[str, Any]:
        profile = profile or {}
        query, pii = redact_pii(raw_query.strip()[:1000])
        if pii:
            trace.data["meta"]["pii_redacted"] = pii
        if not query:
            return self._result("abstained", "Please type a question about the HR policies.")
        session = self.store.load(sid)

        with trace.span("guard", "screen_query") as sp:
            scr = screen_query(query)
            sp["attrs"]["category"] = scr.category
        if not scr.allowed:
            if scr.handoff:
                self._call("escalate_to_human", {
                    "topic": query[:200], "reason": scr.category,
                    "session_id": sid, "user_id": profile.get("user_id", ""),
                    "user_role": profile.get("role", ""), "team_id": profile.get("team_id", ""),
                }, trace)
            res = self._result("refused", REFUSAL_TEXT[scr.category], refusal_category=scr.category)
            self._remember(sid, query, res)
            return res
        if self.llm is None:
            return self._result("error_model", "No model endpoint configured (set OPENAI_BASE_URL and OPENAI_API_KEY).")

        budget = Budget(self.s.max_query_tokens, self.s.max_llm_calls)
        llm = self.llm.bind(trace, budget)

        if strategy == "baseline":  # single prompt: no planner, no critic, no few-shot
            plan = {"intent": "policy_lookup", "rewritten_query": query, "sub_queries": []}
        else:
            plan = self._plan(llm, query, session, trace, force_decompose=(strategy == "decomposition"))
        if plan["intent"] in ("consequential_action", "out_of_scope", "needs_clarification"):
            cat = {"consequential_action": "consequential_action", "out_of_scope": "out_of_scope"}.get(plan["intent"])
            if cat == "consequential_action":
                self._call("escalate_to_human", {
                    "topic": query[:200], "reason": "planner:consequential_action",
                    "session_id": sid, "user_id": profile.get("user_id", ""),
                    "user_role": profile.get("role", ""), "team_id": profile.get("team_id", ""),
                }, trace)
            text = REFUSAL_TEXT[cat] if cat else "Could you add a bit more detail (which policy or situation do you mean)?"
            res = self._result("refused" if cat else "abstained", text, refusal_category=cat or "clarify")
            self._remember(sid, query, res)
            return res

        queries = plan["sub_queries"] if (plan["intent"] == "multi_part" and plan["sub_queries"]) else [plan["rewritten_query"]]
        evidence, exec_info = self._execute(queries, trace)
        if not evidence:
            res = self._result("abstained", REFUSAL_TEXT["out_of_scope"], failure_class="retrieval_miss", **exec_info)
            self._remember(sid, query, res)
            return res

        with trace.span("generate", strategy) as sp:
            ans = self._generate(llm, strategy, plan["rewritten_query"], evidence, queries, trace, profile)
            sp["attrs"]["answerable"] = ans.get("answerable")

        if strategy == "baseline":
            ans, verdict = ans, {"supported": [], "unsupported": [], "invalid_source_ids": [], "all_ok": True, "revised": False}
        else:
            ans, verdict = self._critic(llm, plan["rewritten_query"], ans, evidence, trace, profile=profile)
        res = self._finalize(ans, verdict, evidence, exec_info)
        self._remember(sid, query, res)
        return res

    # --------------------------------------------------------------------- planner
    def _plan(self, llm, query, session, trace, force_decompose=False) -> Dict[str, Any]:
        hist = SessionStore.history_text(session)
        user = (f"Conversation so far:\n{hist}\n\n" if hist else "") + f"Latest user message: {query}"
        if force_decompose:
            user += "\nAlways list sub_queries (1-3) covering distinct facts needed."
        with trace.span("planner", "plan") as sp:
            try:
                plan = llm.json_call(PLANNER_SYSTEM, user, PLAN_SCHEMA, stage="planner", max_tokens=250)
            except (JSONFailure, LLMError) as e:  # degrade: heuristic plan keeps the system answering
                sp["status"], sp["error"] = "fallback", str(e)[:200]
                trace.count("tool_errors")
                plan = {"intent": "policy_lookup", "rewritten_query": query, "sub_queries": []}
            plan["rewritten_query"] = plan.get("rewritten_query") or query
            plan["sub_queries"] = [q for q in plan.get("sub_queries", []) if q.strip()][:3]
            if plan["intent"] == "multi_part" and len(plan["sub_queries"]) < 2:
                plan["intent"] = "policy_lookup"
            if force_decompose and len(plan["sub_queries"]) >= 2:
                plan["intent"] = "multi_part"
            sp["attrs"]["plan"] = plan
        return plan

    # -------------------------------------------------------------------- executor
    def _call(self, name: str, args: dict, trace):
        with trace.span("tool", name, args={k: (v if len(str(v)) < 120 else str(v)[:120]) for k, v in args.items()}) as sp:
            res = self.tools.call(name, args)
            sp["attrs"]["ok"], sp["attrs"]["ms"] = res.ok, res.ms
            if not res.ok:
                sp["attrs"]["error"] = res.error
                trace.count("tool_errors")
        trace.count("tool_calls")
        return res

    def _execute(self, queries: List[str], trace) -> Tuple["OrderedDict[str, dict]", Dict[str, Any]]:
        evidence: "OrderedDict[str, dict]" = OrderedDict()
        seen, steps, flagged = set(), 0, 0
        pending = list(queries)
        info = {"loop_detected": False, "steps": 0, "injection_flagged": 0}
        while pending and steps < self.s.max_steps:
            q = pending.pop(0)
            key = " ".join(tokenize(q))
            if key in seen:  # identical (post-normalisation) call: loop guard
                info["loop_detected"] = True
                trace.data["spans"].append({"kind": "guard", "name": "duplicate_tool_call", "attrs": {"query": q}, "status": "skipped"})
                continue
            seen.add(key)
            res = self._call("search_policy", {"query": q, "top_k": self.s.top_k}, trace)
            steps += 1
            if not res.ok:  # one simplified retry on tool failure
                pending.insert(0, " ".join(tokenize(q)) or q)
                continue
            hits = res.data
            if (not hits or hits[0]["score"] < WEAK_RETRIEVAL) and steps < self.s.max_steps:
                simple = " ".join(dict.fromkeys(tokenize(q)))
                if simple and " ".join(tokenize(simple)) not in seen:
                    pending.insert(0, simple)  # reformulate once when retrieval looks weak
            for h in hits:
                if any(v["chunk_id"] == h["chunk_id"] for v in evidence.values()) or len(evidence) >= 8:
                    continue
                text, bad = sanitize_chunk(h["text"])
                flagged += bad
                evidence[f"S{len(evidence) + 1}"] = {**h, "text": text}
        info.update(steps=steps, injection_flagged=flagged)
        if flagged:
            trace.data["meta"]["injection_in_documents"] = flagged
        return evidence, info

    # ------------------------------------------------------------------- generator
    def _answer_once(self, llm, strategy, question, evidence, temperature=0.1, sub="",
                     role: Optional[str] = None, tenure: Optional[str] = None) -> Dict[str, Any]:
        msgs = answer_messages_personalized(strategy, question, evidence, self.s.context_chars, sub, role=role, tenure=tenure)
        return self._json_with_shots(llm, msgs[0]["content"], msgs[1:-1], msgs[-1]["content"], temperature)

    def _json_with_shots(self, llm, system, shots, user, temperature):
        import jsonschema
        from .llm import extract_json
        messages = [{"role": "system", "content": system + "\nRespond with a single JSON object only. No prose."}, *shots,
                    {"role": "user", "content": user}]
        err = ""
        for attempt in range(llm.json_retries + 1):
            raw = llm.chat(messages, temperature=temperature if attempt == 0 else 0.0, schema=ANSWER_SCHEMA,
                           stage="answer" if attempt == 0 else f"answer:repair{attempt}")
            try:
                obj = extract_json(raw)
                jsonschema.validate(obj, ANSWER_SCHEMA)
                return obj
            except (JSONFailure, jsonschema.ValidationError) as e:
                err = e.message if isinstance(e, jsonschema.ValidationError) else str(e)
                if llm.trace:
                    llm.trace.count("json_repairs")
                messages = messages + [{"role": "assistant", "content": raw[:1500]},
                                       {"role": "user", "content": f"Invalid: {err[:250]}. Return ONLY the corrected JSON."}]
        raise JSONFailure(f"answer JSON invalid after repairs: {err}")

    def _generate(self, llm, strategy, question, evidence, queries, trace, profile: Dict[str, Any] = None) -> Dict[str, Any]:
        role = (profile or {}).get("role")
        tenure = (profile or {}).get("tenure")
        if strategy == "selfconsistency":
            cands = []
            for _ in range(3):
                try:
                    cands.append(self._answer_once(llm, strategy, question, evidence, temperature=0.7, role=role, tenure=tenure))
                except (JSONFailure, BudgetExceeded):
                    continue
            if not cands:
                raise JSONFailure("no valid self-consistency sample")
            return self._vote(cands, evidence)
        if strategy == "decomposition" and len(queries) > 1:
            notes = []
            for q in queries:
                sub_ev = OrderedDict((k, v) for k, v in evidence.items())
                try:
                    a = self._answer_once(llm, "structured", q, sub_ev, role=role, tenure=tenure)
                    notes.append(f"- {q}: {a['answer']} (cited {', '.join(s for c in a['claims'] for s in c['sources'])})")
                except JSONFailure:
                    notes.append(f"- {q}: (could not answer)")
            return self._answer_once(llm, "structured", question, evidence, sub="\n".join(notes), role=role, tenure=tenure)
        return self._answer_once(llm, strategy, question, evidence, role=role, tenure=tenure)

    @staticmethod
    def _vote(cands: List[dict], evidence) -> dict:
        maj = Counter(c["answerable"] for c in cands).most_common(1)[0][0]
        pool = [c for c in cands if c["answerable"] == maj]
        def score(c):
            sup = [max((claim_support(x["text"], evidence[i]["text"]) for i in x["sources"] if i in evidence), default=0) for x in c["claims"]]
            agree = sum(1 for o in pool if {s for x in o["claims"] for s in x["sources"]} == {s for x in c["claims"] for s in x["sources"]})
            return (agree, sum(sup) / len(sup) if sup else 0)
        return max(pool, key=score)

    # ---------------------------------------------------------------------- critic
    def _critic(self, llm, question, ans, evidence, trace, profile: Dict[str, Any] = None):
        role = (profile or {}).get("role")
        tenure = (profile or {}).get("tenure")
        with trace.span("critic", "check") as sp:
            verdict = check_claims(ans.get("claims", []), evidence)
            if ans.get("answerable") and not ans.get("claims"):
                verdict["all_ok"] = False
                verdict["no_claims"] = True
            sp["attrs"].update(unsupported=len(verdict["unsupported"]), bad_ids=len(verdict["invalid_source_ids"]))
        verdict["revised"] = False
        if verdict["all_ok"] or not ans.get("answerable"):
            return ans, verdict
        # one revision: expand context via read_section on the best-cited doc, ask for a corrected answer
        try:
            top_path = next(iter(evidence.values()))["path"]
            exp = self._call("read_section", {"path": top_path}, trace)
            ev2 = OrderedDict(evidence)
            if exp.ok and len(ev2) < 9:
                ev2[f"S{len(ev2) + 1}"] = {"path": top_path, "section": "(expanded)", "chunk_id": top_path + "#exp",
                                         "text": sanitize_chunk(exp.data)[0][: self.s.context_chars * 2], "score": 0, "stages": {}}
            bad = "; ".join(x["claim"][:80] for x in verdict["unsupported"] + verdict["invalid_source_ids"])
            q2 = f"{question}\n(Critic: these claims were NOT supported by the cited sources: {bad}. Remove or fix them; use only listed source ids.)"
            ans2 = self._answer_once(llm, "structured", q2, ev2, role=role, tenure=tenure)
            v2 = check_claims(ans2.get("claims", []), ev2)
            v2["revised"] = True
            if len(v2["unsupported"]) + len(v2["invalid_source_ids"]) <= len(verdict["unsupported"]) + len(verdict["invalid_source_ids"]):
                evidence.update(ev2)
                ans, verdict = ans2, v2
        except (JSONFailure, LLMError, BudgetExceeded):
            pass
        # final guard: keep only claims that pass
        if not verdict["all_ok"]:
            keep = {x["claim"] for x in verdict["supported"]}
            ans = dict(ans, claims=[c for c in ans.get("claims", []) if c.get("text") in keep])
            verdict["dropped"] = len(verdict["unsupported"]) + len(verdict["invalid_source_ids"])
            if not ans["claims"]:
                ans = dict(ans, answerable=False, answer="The documents do not say.", confidence="low")
        return ans, verdict

    # -------------------------------------------------------------------- assembly
    def _finalize(self, ans, verdict, evidence, exec_info) -> Dict[str, Any]:
        if not ans.get("answerable"):
            fc = "hallucination" if (verdict.get("dropped") or verdict["unsupported"]) else ""
            return self._result("abstained", "I couldn't find this in the policy documents I have access to. "
                                "Please check with People Ops for an authoritative answer.", failure_class=fc, verdict=verdict,
                                retrieved=[v["path"] for v in evidence.values()], **exec_info)
        used = list(OrderedDict.fromkeys(s for c in ans["claims"] for s in c["sources"]))
        cites = [{"id": sid, "path": evidence[sid]["path"], "section": evidence[sid]["section"],
                  "snippet": evidence[sid]["text"][:300]} for sid in used if sid in evidence]
        body = ans["answer"].strip()
        if verdict.get("dropped"):
            body += "\n\n(Note: some details were removed because I could not verify them in the documents.)"
        return self._result("success", body, citations=cites, claims=ans["claims"], confidence=ans.get("confidence", "medium"),
                            verdict=verdict, retrieved=[v["path"] for v in evidence.values()], **exec_info)

    @staticmethod
    def _result(status: str, answer: str, **extra) -> Dict[str, Any]:
        base = {"status": status, "answer": answer, "citations": [], "claims": [], "confidence": "low", "failure_class": ""}
        base.update(extra)
        return base

    def _remember(self, sid: str, query: str, res: Dict[str, Any]) -> None:
        try:
            self.store.append(sid, {"query": query, "answer": res["answer"][:500], "status": res["status"],
                                    "sources": [c["path"] for c in res.get("citations", [])]})
        except OSError:
            pass
