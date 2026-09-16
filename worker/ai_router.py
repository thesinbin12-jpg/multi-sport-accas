"""
ai_router.py — Multi-provider LLM router with fallback chain.
Primary: Groq (gpt-oss-20b / qwen/qwen3.6-27b)
Fallback 1: Gemini 3.5 Flash / 3.6 Flash / 3.7 Flash
Fallback 2: Gemma 4 (if available)
"""

import os, json, time, requests

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_BASE = "https://openrouter.ai/api/v1"
NIM_KEY = os.environ.get("NVIDIA_API_KEY", "")
NIM_BASE = "https://integrate.api.nvidia.com/v1"
NIM_MODELS = [m.strip() for m in os.environ.get("NIM_MODELS", "mistralai/mistral-nemotron,meta/muse-glimmer-30b,moonshotai/kimi-k3,nvidia/nemotron-3-super-120b-a12b,nvidia/nemotron-3.5-lightning-30b-a3b,deepseek-ai/deepseek-v4-flash-0731").split(",") if m.strip()]

# Flap cooldown: models that time out / 429 / 5xx sit out 10 min so one hung
# model (e.g. kimi 90s) can't stall every call. In-process, never raises.
_COOLDOWN: dict = {}
_COOLDOWN_SECS = 600


def _cool(model):
    try:
        _COOLDOWN[str(model)] = time.time() + _COOLDOWN_SECS
    except Exception:
        pass


def _hot(model):
    try:
        return time.time() < float(_COOLDOWN.get(str(model), 0))
    except Exception:
        return False
# NOTE: deepseek-v4-flash is a reasoning model (needs big max_tokens, slower) ->
# last in chain as extra fallback. deepseek-v4-pro hangs (150s timeout, no bytes)
# -> deliberately NOT listed (2026-09-13).

class AIRouter:
    def __init__(self):
        self.groq_models = [
            "openai/gpt-oss-20b",     # Primary — good reasoning
            "qwen/qwen3.6-27b",        # Fallback — strong multi-language
            "groq/compound",           # Groq's compound model
        ]
        self.gemini_models = [
            "gemini-3.5-flash",        # Gemini 3.5 — fast, good quality
            "gemini-3.6-flash",        # Gemini 3.6 — newer
            "gemini-3.7-flash",        # Gemini 3.7 — newest flash
        ]
        self.gemma_models = [
            "gemma-4-26b-a4b-it",      # Gemma 4 via AI Studio (free)
            "gemma-4-31b-it",          # Gemma 4 larger, also free
        ]
        self.or_models = [
            "nvidia/nemotron-3.5-lightning:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "google/gemma-4-26b-a4b-it:free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        ]
        self.gemini_api_base = "https://generativelanguage.googleapis.com/v1"
    
    def call_groq(self, model, messages, max_tokens=1024, temperature=0.7, json_mode=False):
        """Call Groq API (OpenAI-compatible). json_mode forces response_format."""
        if not GROQ_KEY:
            return None, "GROQ_KEY not set"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            if json_mode and r.status_code == 400 and "response_format" in r.text:
                payload.pop("response_format", None)  # old model rejects it; retry plain
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                    json=payload, timeout=30)
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"], None
            elif r.status_code == 404:
                return None, f"Model {model} not found"
            elif r.status_code == 429:
                _cool(model)
                return None, f"Groq rate limited: {model}"
            else:
                return None, f"Groq error {r.status_code}: {r.text[:100]}"
        except requests.exceptions.Timeout:
            _cool(model)
            return None, "Groq timeout"
        except Exception as e:
            return None, f"Groq exception: {e}"
    
    def has_provider(self, name):
        if name == "groq":
            return bool(GROQ_KEY)
        if name == "gemini":
            return bool(GEMINI_KEY)
        if name == "orouter":
            return bool(OR_KEY)
        if name == "nim":
            return bool(NIM_KEY)
        return False

    def call_nim(self, model, messages, max_tokens=1024, temperature=0.7, json_mode=False):
        if not NIM_KEY:
            return None, "NIM_KEY not set"
        if "deepseek" in str(model):
            max_tokens = max(max_tokens, 2048)  # reasoning models think first; 1024 starves the answer
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = requests.post(
                f"{NIM_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {NIM_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=25,
            )
            if json_mode and r.status_code == 400 and "response_format" in r.text:
                payload.pop("response_format", None)
                r = requests.post(
                    f"{NIM_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {NIM_KEY}", "Content-Type": "application/json"},
                    json=payload, timeout=25)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"], None
            if r.status_code == 429:
                _cool(model)
                return None, "NIM rate limited"
            if r.status_code == 404:
                return None, "Model not entitled: " + str(model)
            if r.status_code >= 500:
                _cool(model)
            return None, "NIM error %s: %s" % (r.status_code, r.text[:100])
        except requests.exceptions.Timeout:
            _cool(model)
            return None, "NIM timeout (25s fast-fail)"
        except Exception as e:
            return None, "NIM exception: " + str(e)

    def call_openrouter(self, model, messages, max_tokens=1024, temperature=0.7, json_mode=False):
        """Call OpenRouter (OpenAI-compatible, :free models need only a key)."""
        if not OR_KEY:
            return None, "OR_KEY not set"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = requests.post(
                f"{OR_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OR_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://accas-roan.vercel.app",
                    "X-Title": "multi-sport-accas",
                },
                json=payload,
                timeout=45,
            )
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"], None
            elif r.status_code == 429:
                return None, "OpenRouter rate limited"
            else:
                return None, f"OpenRouter error {r.status_code}: {r.text[:100]}"
        except requests.exceptions.Timeout:
            return None, "OpenRouter timeout"
        except Exception as e:
            return None, f"OpenRouter exception: {e}"

    def call_gemini(self, model, prompt, max_tokens=1024, json_mode=False):
        """Call Gemini API (Google AI Studio)."""
        if not GEMINI_KEY:
            return None, "GEMINI_KEY not set"
        gcfg = {"maxOutputTokens": max_tokens}
        if json_mode:
            gcfg["responseMimeType"] = "application/json"
        try:
            url = f"{self.gemini_api_base}/{model}:generateContent"
            r = requests.post(
                url,
                params={"key": GEMINI_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": gcfg,
                },
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return text, None
            elif r.status_code == 404:
                return None, f"Model {model} not found"
            elif r.status_code == 429:
                return None, "Gemini rate limited (quota exceeded)"
            else:
                return None, f"Gemini error {r.status_code}: {r.text[:100]}"
        except requests.exceptions.Timeout:
            return None, "Gemini timeout"
        except Exception as e:
            return None, f"Gemini exception: {e}"
    
    def analyze(self, prompt, system_prompt="", model_pref=None, json_mode=False):
        """
        Route analysis request through fallback chain.
        Returns (analysis_text, model_used, error).
        json_mode forces API-level JSON (response_format/responseMimeType) so
        reasoning models emit valid JSON in content instead of <think> prose.
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Track timing for logging
        _json_mode = json_mode
        start = time.time()
        last_error = None

        # NOTE 2026-09-11: Zen free tier is locked to the OpenCode client
        # (MissingSessionID server-side); paid models need a payment method.
        # Zen stays LAST until the workspace can serve API calls.
        
        # Phase 1: Try Groq models in order (flapping models sit out 10min)
        if not model_pref or model_pref == "groq":
            _gq = [m for m in self.groq_models if not _hot(m)] or list(self.groq_models)
            for model in _gq:
                text, err = self.call_groq(model, messages, json_mode=_json_mode)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err
        
        # Phase 2: Try Gemini models in order  
        if not model_pref or model_pref == "gemini":
            for model in self.gemini_models:
                text, err = self.call_gemini(model, prompt, json_mode=_json_mode)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err
        
        # Phase 3: Try Gemma as last resort
        if not model_pref or model_pref == "gemma":
            for model in self.gemma_models:
                text, err = self.call_gemini(model, prompt, json_mode=_json_mode)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err

        # NOTE 2026-09-11: OpenCode Zen removed — free tier locked to the
        # OpenCode client (MissingSessionID server-side even with session
        # header; quota exhausted), paid needs billing. Kept out of chain.
        # Phase 5: NVIDIA NIM (tested winners; needs key; models via NIM_MODELS env)
        # Flapping models sit out 10min so one hang can't stall every call.
        if not model_pref or model_pref == "nim":
            _nm = [m for m in NIM_MODELS if not _hot(m)] or list(NIM_MODELS)
            for model in _nm:
                text, err = self.call_nim(model, messages, json_mode=_json_mode)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err

        # Phase 4: OpenRouter :free models (server-side friendly, needs key)
        if not model_pref or model_pref == "orouter":
            for model in self.or_models:
                text, err = self.call_openrouter(model, messages, json_mode=_json_mode)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err
        
        # All failed
        elapsed = time.time() - start
        return None, None, last_error or "All models failed", elapsed


# Singleton instance
router = AIRouter()


def analyze_leg(leg_data, league_context="", news_snippets=""):
    """
    Analyze a single accumulator leg using the AI router.
    Returns probability assessment text.
    """
    system = """You are an accumulator leg analyst. 
Given match data, odds, form, H2H, news, and stats — give a concise probability 
assessment for each outcome. Be specific with percentages. Mention key factors 
(form, H2H, injuries, motivation, league context). 
Keep it to 3-5 sentences. Focus on value: where does the market price differ from 
what the data suggests?"""
    
    prompt = f"""Analyze this accumulator leg for probability:

Match: {leg_data.get('home_team', '?')} vs {leg_data.get('away_team', '?')}
League: {leg_data.get('league', '?')}
Sport: {leg_data.get('sport', '?')}
Commence: {leg_data.get('commence_time', '?')}

Odds (from multiple bookmakers):
"""
    # Add odds from multiple bookmakers
    bookmakers = leg_data.get("bookmakers", {})
    for bk_name, bk_data in list(bookmakers.items())[:3]:
        prompt += f"\n  {bk_name}: "
        markets = bk_data.get("markets", [])
        for m in markets[:2]:
            outcomes = m.get("outcomes", [])
            outcome_strs = [f"{o.get('name','?')} @ {o.get('price','?')}" for o in outcomes[:3]]
            prompt += f"{m.get('name','?')} — {', '.join(outcome_strs)} | "
    
    prompt += f"""
League context: {league_context}
News: {news_snippets}
Form/H2H/Stats: {leg_data.get('form', 'N/A')}

Give me:
1. Win probability for home team (%)
2. Draw probability (%)
3. Away team win probability (%)
4. Key factors supporting your assessment
5. Is there value here vs the market odds?
"""
    
    text, model, err, elapsed = router.analyze(prompt, system_prompt=system)
    
    if err:
        return f"ERROR: {err} (model chain: groq→gemini→gemma, elapsed={elapsed:.1f}s)", model, err
    
    return text, model, None


def build_accas_analysis(matches_data, max_legs=6):
    """
    Analyze multiple potential legs and select the best ones for an accumulator.
    Uses the AI router to rank legs by value.
    """
    system = """You are an accumulator builder. Given a list of potential legs with 
their probability assessments, select the best N legs (N=max_legs) for a high-yield 
accumulator. Consider: individual leg probability, combined odds, correlation between 
legs (avoid correlated legs from same league/team), and overall expected value.
Return a JSON array of selected leg indices with reasoning."""
    
    # Build summary of all legs
    leg_summaries = []
    for i, m in enumerate(matches_data):
        leg_summaries.append(f"Leg {i+1}: {m.get('home_team','?')} vs {m.get('away_team','?')} | "
                           f"{m.get('league','?')} | Odds: {m.get('best_odds','?')} | "
                           f"Assessment: {m.get('assessment','N/A')[:100]}")
    
    prompt = f"""Build a high-yield accumulator from these {len(leg_summaries)} potential legs.
Select exactly {max_legs} legs. Consider value, correlation, and combined odds.

{chr(10).join(leg_summaries)}

Return JSON: {{"selected": [leg_indices...], "reasoning": "explanation", "combined_odds": estimated_odds}}"""
    
    text, model, err, elapsed = router.analyze(prompt, system_prompt=system)
    
    if err:
        return {"error": err}, model, err
    
    # Try to parse JSON from response
    try:
        # Extract JSON from markdown code blocks if present
        import re
        json_match = re.search(r'\{[^}]+\"selected\"[^}]+\}', text)
        if json_match:
            return json.loads(json_match.group(0)), model, None
        # Try direct parse
        return json.loads(text), model, None
    except json.JSONDecodeError:
        return {"raw_response": text, "parse Warning": "Could not parse JSON, using raw"}, model, None


if __name__ == "__main__":
    # Quick test
    text, model, err, elapsed = router.analyze(
        "Say which model you are and your max context window.",
        system_prompt="Be brief, 1 sentence."
    )
    print(f"Model: {model}")
    print(f"Response: {text}")
    print(f"Elapsed: {elapsed:.2f}s")
    if err:
        print(f"Error: {err}")
