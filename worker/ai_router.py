"""
ai_router.py — Multi-provider LLM router with fallback chain.
Primary: Groq (gpt-oss-20b / qwen/qwen3.6-27b)
Fallback 1: Gemini 3.5 Flash / 3.6 Flash / 3.7 Flash
Fallback 2: Gemma 4 (if available)
"""

import os, json, time, requests

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
ZEN_KEY = os.environ.get("OPENCODE_API_KEY", "")
ZEN_BASE = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1")

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
            "gemini-2.5-flash",        # Older but reliable
            "gemini-3.1-flash-lite",   # Lightweight fallback
        ]
        self.gemma_models = [
            "gemma-4-26b-a4b-it",      # Gemma 4
        ]
        self.zen_models = [
            "big-pickle",              # user pick: steadiest availability
            "nemotron-3-ultra-free",   # fast verdicts
            "mimo-v2.5-free",          # all-rounder
            "deepseek-v4-flash-free",  # reasoning
        ]
        self.gemini_api_base = "https://generativelanguage.googleapis.com/v1"
    
    def call_groq(self, model, messages, max_tokens=1024, temperature=0.7):
        """Call Groq API (OpenAI-compatible)."""
        if not GROQ_KEY:
            return None, "GROQ_KEY not set"
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"], None
            elif r.status_code == 404:
                return None, f"Model {model} not found"
            else:
                return None, f"Groq error {r.status_code}: {r.text[:100]}"
        except requests.exceptions.Timeout:
            return None, "Groq timeout"
        except Exception as e:
            return None, f"Groq exception: {e}"
    
    def call_zen(self, model, messages, max_tokens=1024, temperature=0.7):
        """Call OpenCode Zen (OpenAI-compatible). Needs OPENCODE_API_KEY env."""
        if not ZEN_KEY:
            return None, "ZEN_KEY not set"
        try:
            r = requests.post(
                f"{ZEN_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {ZEN_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"], None
            elif r.status_code == 429:
                return None, "Zen rate limited"
            else:
                return None, f"Zen error {r.status_code}: {r.text[:100]}"
        except requests.exceptions.Timeout:
            return None, "Zen timeout"
        except Exception as e:
            return None, f"Zen exception: {e}"

    def has_provider(self, name):
        if name == "groq":
            return bool(GROQ_KEY)
        if name == "gemini":
            return bool(GEMINI_KEY)
        if name == "zen":
            return bool(ZEN_KEY)
        return False

    def call_gemini(self, model, prompt, max_tokens=1024):
        """Call Gemini API (Google AI Studio)."""
        if not GEMINI_KEY:
            return None, "GEMINI_KEY not set"
        try:
            url = f"{self.gemini_api_base}/{model}:generateContent"
            r = requests.post(
                url,
                params={"key": GEMINI_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": max_tokens,
                    },
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
    
    def analyze(self, prompt, system_prompt="", model_pref=None):
        """
        Route analysis request through fallback chain.
        Returns (analysis_text, model_used, error).
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Track timing for logging
        start = time.time()
        last_error = None

        # Phase 0: OpenCode Zen first (user's main provider)
        if not model_pref or model_pref == "zen":
            for model in self.zen_models:
                text, err = self.call_zen(model, messages)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err
        
        # Phase 1: Try Groq models in order
        if not model_pref or model_pref == "groq":
            for model in self.groq_models:
                text, err = self.call_groq(model, messages)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err
        
        # Phase 2: Try Gemini models in order  
        if not model_pref or model_pref == "gemini":
            for model in self.gemini_models:
                text, err = self.call_gemini(model, prompt)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err
        
        # Phase 3: Try Gemma as last resort
        if not model_pref or model_pref == "gemma":
            for model in self.gemma_models:
                text, err = self.call_gemini(model, prompt)
                if text and not err:
                    elapsed = time.time() - start
                    return text, model, None, elapsed
                last_error = err

        # Phase 4: OpenCode Zen free models (kept as late fallback too)
        if not model_pref:
            for model in self.zen_models:
                text, err = self.call_zen(model, messages)
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
