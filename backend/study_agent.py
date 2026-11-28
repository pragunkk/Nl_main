"""
Hierarchical Multi-Agent Learning Architecture
==============================================
- OrchestratorAgent: Inspects the active lesson (title, module, difficulty, topic type,
  transcript/description) and full learner state (visited modules, level, and ALL
  previous student summaries), decides which helper is best suited, allocates tailored
  directives, and coordinates specialized subagents behind the scenes.
- Specialized Subagents (exposed as independent student-facing features):
  1. StudyChatAgent ('chat'): Brief, plain-language tutor chat aware of progress & all summaries.
  2. ConceptCoachSubAgent ('coach'): Builds mental models, prerequisite bridges, and step-by-step intuition.
  3. NotesSubAgent ('notes'): Synthesizes structured editable study notes & takeaways.
  4. FlashcardSubAgent ('flashcards'): Generates active-recall flip-cards.
  5. QuizSubAgent ('quiz'): Generates 3-level adaptive MCQs ('easy', 'medium', 'hard') informed by summaries.
- Powered by Groq Cloud LLM (dynamic account model selection via GROQ_API_KEY in .env),
  with automatic deterministic local corpus fallback so it never fails offline.
"""

import os
import re
import json
import random

_RAW_CORPUS = None


def _load_corpus():
    global _RAW_CORPUS
    if _RAW_CORPUS is not None:
        return _RAW_CORPUS
    corpus_path = os.path.join(os.path.dirname(__file__), 'nlp', 'nlp_resources.json')
    try:
        if os.path.exists(corpus_path):
            with open(corpus_path, 'r', encoding='utf-8') as f:
                _RAW_CORPUS = json.load(f)
        else:
            _RAW_CORPUS = []
    except Exception as e:
        print(f"[MULTI_AGENT] Could not load nlp_resources.json: {e}")
        _RAW_CORPUS = []
    return _RAW_CORPUS


def _clean_md(text: str) -> str:
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\[(\d{2}:\d{2}(?::\d{2})?[^\]]*)\]', '', text)
    return re.sub(r'\s+', ' ', text).strip(' -•:\n\t')


def _format_student_summaries_context(student_summaries: list, max_items: int = 8) -> str:
    """Build a concise, prompt-ready digest of all summaries the student has written."""
    if not student_summaries:
        return "No lesson summaries written yet (brand-new learner)."
    lines = []
    for idx, s in enumerate(student_summaries[-max_items:], start=1):
        title = str(s.get('title', 'Untitled Lesson')).strip()
        text = str(s.get('summary', '')).strip().replace('\n', ' ')
        if len(text) > 220:
            text = text[:220].rstrip() + '...'
        kws = s.get('keywords_found') or []
        kw_str = f" [Key concepts: {', '.join(kws[:4])}]" if kws else ""
        lines.append(f"{idx}. Lesson '{title}': \"{text}\"{kw_str}")
    return "\n".join(lines)


def _extract_module_sections(description: str):
    """Parse markdown description from nlp_resources.json into structured blocks."""
    insights = []
    highlights = []
    glossary = []
    faqs = []
    summary_sentences = []

    if not description:
        return {
            'insights': insights,
            'highlights': highlights,
            'glossary': glossary,
            'faqs': faqs,
            'summary_sentences': summary_sentences,
        }

    for match in re.finditer(r'-\s*(?:\[[0-9:→\s-]+\]\s*)?\*\*([^*\n:]+?)(?::\*\*|\*\*:?)\s*([^\n]+)', description):
        concept = _clean_md(match.group(1))
        detail = _clean_md(match.group(2))
        if len(concept) > 3 and len(detail) > 25:
            insights.append({'concept': concept, 'detail': detail})

    for match in re.finditer(r'^\|\s*([^|\n-]+?)\s*\|\s*([^|\n-]+?)\s*\|$', description, re.MULTILINE):
        term = _clean_md(match.group(1))
        defn = _clean_md(match.group(2))
        if term.lower() in ('term', 'model', 'year', 'timestamp', 'model/concept', 'model/tokenizer', 'prompt variation'):
            continue
        if len(term) >= 2 and len(defn) >= 20 and not re.match(r'^\d{4}$', term):
            glossary.append({'term': term, 'definition': defn})

    for match in re.finditer(r'\*\*Q\d*:\s*([^*\n]+)\*\*\s*\n+\s*(?:A\d*:\s*)?([^\n]+)', description):
        q_text = _clean_md(match.group(1))
        a_text = _clean_md(match.group(2))
        if len(q_text) > 10 and len(a_text) > 20:
            faqs.append({'question': q_text, 'answer': a_text})

    for line in description.splitlines():
        line_str = line.strip()
        if line_str.startswith('- ') and len(line_str) > 35:
            cleaned = _clean_md(line_str[2:])
            if 35 <= len(cleaned) <= 220 and not cleaned.startswith('['):
                highlights.append(cleaned)

    for line in description.splitlines():
        line_str = line.strip()
        if not line_str or line_str.startswith('#') or line_str.startswith('-') or line_str.startswith('|'):
            continue
        for sent in re.split(r'(?<=[.!?])\s+', line_str):
            cleaned = _clean_md(sent)
            if 45 <= len(cleaned) <= 220:
                summary_sentences.append(cleaned)

    return {
        'insights': insights,
        'highlights': highlights,
        'glossary': glossary,
        'faqs': faqs,
        'summary_sentences': summary_sentences,
    }


def _find_module_entry(module_or_title: str, nlp_resources: list):
    corpus = _load_corpus()
    target = str(module_or_title or '').strip().lower()

    matched_res = None
    for r in nlp_resources:
        if (
            str(r.get('id', '')).lower() == target
            or r.get('title', '').lower() == target
            or r.get('module', '').lower() == target
        ):
            matched_res = r
            break

    if not matched_res and target:
        for r in nlp_resources:
            if target in r.get('title', '').lower() or target in r.get('module', '').lower():
                matched_res = r
                break

    if not matched_res and nlp_resources:
        matched_res = nlp_resources[0]

    module_name = matched_res.get('module', matched_res.get('title', '')) if matched_res else ''
    title_name = matched_res.get('title', module_name) if matched_res else ''
    description = matched_res.get('description', '') if matched_res else ''
    difficulty = int(matched_res.get('difficulty', 2)) if matched_res else 2

    for idx, row in enumerate(corpus):
        row_mod = str(row.get('module', '')).strip()
        row_name = str(row.get('name', '')).strip()
        if not module_name and target and (target in row_mod.lower() or target in row_name.lower()):
            module_name = row_mod or row_name
            title_name = row_name or row_mod
            description = str(row.get('description', ''))
            difficulty = 2 if idx < 5 else (4 if idx < 10 else (6 if idx < 15 else 7))
            break
        elif module_name and (row_mod.lower() == module_name.lower() or row_name.lower() == title_name.lower()):
            if len(str(row.get('description', ''))) > len(description):
                description = row.get('description', '')
            break

    if not module_name:
        module_name = module_or_title or 'Pre training objectives'
        title_name = module_name

    return {
        'id': str(matched_res.get('id', '1')) if matched_res else '1',
        'title': title_name,
        'module': module_name,
        'difficulty': difficulty,
        'description': description,
    }


def _classify_lesson_nature(title: str, module: str, difficulty: int) -> str:
    combined = f"{title} {module}".lower()
    if 'tutorial' in combined or 'huggingface' in combined or 'implementation' in combined:
        return 'Hands-On Coding & Applied Tutorial'
    if any(k in combined for k in ('math', 'attention', 'qlora', 'lora', 'peft', 'quantization', 'pruning', 'distillation')):
        return 'Architecture & Mathematical Optimization'
    if any(k in combined for k in ('rlhf', 'ppo', 'dpo', 'alignment', 'rag', 'agent', 'deployment')):
        return 'Advanced Post-Training & Systems'
    if difficulty <= 2:
        return 'Foundational NLP Theory'
    return 'Core Transformer & LLM Mechanics'


def _collect_distractor_pool(exclude_module: str, nlp_resources: list):
    distractors = []
    for r in nlp_resources:
        if r.get('module', '').lower() == exclude_module.lower():
            continue
        parsed = _extract_module_sections(r.get('description', ''))
        for g in parsed['glossary'][:3]:
            distractors.append(g['definition'])
        for ins in parsed['insights'][:2]:
            distractors.append(ins['detail'])
        for hl in parsed['highlights'][:2]:
            distractors.append(hl)

    fallback_pool = [
        "It replaces all self-attention layers with static non-contextual K-means word clusters.",
        "It discards the encoder stack entirely and relies exclusively on character-level Markov chains.",
        "It updates only the input tokenizer vocabulary while keeping all projection matrices at zero.",
        "It requires labeled reinforcement rewards for every token prior to initial pre-training.",
        "It compresses token sequences using lossless Huffman coding rather than dense vector projections.",
        "It restricts context windows to a single bigram and prevents gradient flow across layers.",
    ]
    distractors.extend(fallback_pool)
    seen = set()
    filtered = []
    for d in distractors:
        d_clean = d[:155].rstrip() + ('...' if len(d) > 155 else '')
        if d_clean.lower() not in seen and len(d_clean) > 25:
            seen.add(d_clean.lower())
            filtered.append(d_clean)
    return filtered


_CACHED_GROQ_MODEL = None


def _get_llm_model_name(ai_client) -> str:
    global _CACHED_GROQ_MODEL
    if not ai_client:
        return "local-corpus-agent"
    base_url = str(getattr(ai_client, 'base_url', '') or '').lower()
    if "groq" not in base_url:
        return "gpt-3.5-turbo"

    if _CACHED_GROQ_MODEL:
        return _CACHED_GROQ_MODEL

    preferred_order = [
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b",
        "llama-3.1-8b-instant",
        "openai/gpt-oss-20b",
    ]
    try:
        models_resp = ai_client.models.list()
        available = {m.id for m in getattr(models_resp, 'data', [])}
        for candidate in preferred_order:
            if candidate in available:
                _CACHED_GROQ_MODEL = candidate
                print(f"[MULTI_AGENT] Selected available Groq model: {_CACHED_GROQ_MODEL}")
                return _CACHED_GROQ_MODEL
        for m_id in sorted(available):
            low = m_id.lower()
            if not any(skip in low for skip in ("whisper", "guard", "orpheus", "safeguard")):
                _CACHED_GROQ_MODEL = m_id
                print(f"[MULTI_AGENT] Selected fallback Groq model: {_CACHED_GROQ_MODEL}")
                return _CACHED_GROQ_MODEL
    except Exception as e:
        print(f"[MULTI_AGENT] Could not list Groq models, defaulting to openai/gpt-oss-120b: {e}")

    _CACHED_GROQ_MODEL = "openai/gpt-oss-120b"
    return _CACHED_GROQ_MODEL


def _extract_json_block(raw_text: str):
    if not raw_text:
        return None
    cleaned = re.sub(r'```(?:json)?', '', raw_text).strip()
    match = re.search(r'\{[\s\S]*\}', cleaned)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


# ============================================================================
# SUBAGENTS
# ============================================================================

class StudyChatAgent:
    """
    Student-facing Study Chat Tutor.
    Aware of the student's progress history, visited lessons, current level,
    and ALL previous summaries they have written. Answers briefly and in simple words.
    """
    agent_id = "chat"
    name = "Study Chat Tutor"

    @classmethod
    def run(
        cls,
        question: str,
        entry: dict,
        visited_modules: list,
        student_level: int = 1,
        student_summaries: list = None,
        history: list = None,
        ai_client=None,
    ):
        student_summaries = student_summaries or []
        history = history or []
        title_name = entry['title']
        module_name = entry['module']
        description = entry['description']
        summaries_digest = _format_student_summaries_context(student_summaries)
        visited_str = ", ".join(visited_modules) if visited_modules else "None yet"

        if ai_client:
            try:
                model = _get_llm_model_name(ai_client)
                system_prompt = f"""You are a friendly, helpful NLP Study Tutor.
You know the student's full learning status and all summaries they have written so far.

STUDENT STATUS:
- Current Level: Stage {student_level}
- Active Lesson: "{title_name}" (Module: "{module_name}")
- Lessons Visited ({len(visited_modules)}): {visited_str}

STUDENT'S PREVIOUS SUMMARIES:
{summaries_digest}

ACTIVE LESSON REFERENCE:
{description[:2600]}

STRICT RESPONSE RULES:
- Keep answers BRIEF (2–4 short sentences, or up to 3 tiny bullet points).
- Use SIMPLE, everyday words — avoid heavy jargon unless explaining a term directly.
- Personalize your reply using the student's progress and previous summaries when relevant (for example, connect new ideas to lessons they already summarized, or gently point out what they can review next).
- Be encouraging and direct."""

                messages = [{"role": "system", "content": system_prompt}]
                for msg in history[-6:]:
                    role = "user" if msg.get("role") == "user" else "assistant"
                    content = str(msg.get("content", "")).strip()
                    if content:
                        messages.append({"role": role, "content": content})
                messages.append({"role": "user", "content": question})

                completion = ai_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.5,
                    max_tokens=500,
                )
                answer = (completion.choices[0].message.content or "").strip()
                if answer:
                    return {
                        "answer": answer,
                        "source": f"groq-{model}",
                        "summariesUsed": len(student_summaries),
                        "visitedCount": len(visited_modules),
                    }
            except Exception as e:
                print(f"[STUDY_CHAT] LLM fallback: {e}")

        # Simple, helpful local fallback aware of summaries & lesson text
        q_lower = question.lower()
        sections = _extract_module_sections(description)

        if any(k in q_lower for k in ("summary", "summaries", "progress", "learned", "status", "so far")):
            if student_summaries:
                recent = [s.get('title', 'a lesson') for s in student_summaries[-3:]]
                answer = (
                    f"You're at Stage {student_level} with {len(visited_modules)} lessons visited and "
                    f"{len(student_summaries)} summaries written (most recently: {', '.join(recent)}). "
                    f"Right now you're focusing on '{title_name}' — ask me about any concept in it!"
                )
            else:
                answer = (
                    f"You're at Stage {student_level} and just getting started with '{title_name}'. "
                    f"Watch the lesson and write a short summary in your own words to start tracking your progress!"
                )
        else:
            q_words = [w for w in re.findall(r'[a-z]{4,}', q_lower)]
            matched_sentence = None
            for pool in (
                [f"{g['term']}: {g['definition']}" for g in sections['glossary']],
                [f"{i['concept']}: {i['detail']}" for i in sections['insights']],
                sections['highlights'],
                sections['summary_sentences'],
            ):
                for item in pool:
                    if any(w in item.lower() for w in q_words):
                        matched_sentence = item
                        break
                if matched_sentence:
                    break

            if matched_sentence:
                answer = f"In simple terms for '{title_name}': {matched_sentence}"
            elif sections['summary_sentences']:
                answer = f"Here is the main idea of '{title_name}': {sections['summary_sentences'][0]}"
            else:
                answer = f"'{title_name}' focuses on core concepts in {module_name}. Ask me about any specific term or check Smart Notes for a quick breakdown!"

        return {
            "answer": answer,
            "source": "local-corpus-agent",
            "summariesUsed": len(student_summaries),
            "visitedCount": len(visited_modules),
        }


class ConceptCoachSubAgent:
    """Subagent specializing in conceptual intuition, prerequisite bridges, and step-by-step mental models."""
    agent_id = "coach"
    name = "Concept Coach"
    role = "Intuition & Prerequisite Bridge"

    @classmethod
    def run(cls, entry: dict, directive: str, visited_modules: list, student_summaries: list = None, ai_client=None):
        student_summaries = student_summaries or []
        title_name = entry['title']
        module_name = entry['module']
        description = entry['description']
        prev_context = ", ".join(visited_modules[-3:]) if visited_modules else "Foundational Neural Networks"
        summaries_digest = _format_student_summaries_context(student_summaries, max_items=5)

        if ai_client:
            try:
                model = _get_llm_model_name(ai_client)
                prompt = f"""You are the Concept Coach overseen by the Main Orchestrator.
Orchestrator Directive: "{directive}"
Current Lesson: "{title_name}" (Module: "{module_name}", Difficulty: {entry['difficulty']}/7)
Student's Recently Visited Modules: {prev_context}
Student's Previous Summaries:
{summaries_digest}

Lecture Material Context:
{description[:2800]}

Keep explanations clear and easy to understand. Return ONLY valid JSON with this exact schema:
{{
  "intuition": "2-3 sentences explaining the core mental model in plain, simple words.",
  "prerequisiteBridge": "1-2 sentences connecting what the student already summarized/visited ({prev_context}) to {title_name}.",
  "realWorldAnalogy": "A concrete everyday analogy illustrating how this works.",
  "stepByStepBreakdown": [
    "Step 1: ...",
    "Step 2: ...",
    "Step 3: ...",
    "Step 4: ..."
  ]
}}"""
                completion = ai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a specialized NLP Concept Coach that outputs strict JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.4,
                    max_tokens=1500,
                )
                parsed = _extract_json_block(completion.choices[0].message.content)
                if parsed and parsed.get("intuition"):
                    return {
                        "agentId": cls.agent_id,
                        "agentName": cls.name,
                        "directive": directive,
                        "intuition": parsed.get("intuition", ""),
                        "prerequisiteBridge": parsed.get("prerequisiteBridge", ""),
                        "realWorldAnalogy": parsed.get("realWorldAnalogy", ""),
                        "stepByStepBreakdown": (parsed.get("stepByStepBreakdown") or [])[:4],
                        "source": f"groq-{model}",
                    }
            except Exception as e:
                print(f"[COACH_SUBAGENT] LLM fallback: {e}")

        sections = _extract_module_sections(description)
        intuition = " ".join(sections['summary_sentences'][:2]) or f"{title_name} introduces key ideas in {module_name}."
        bridge = (
            f"Building on your progress in {prev_context}, this lesson extends those concepts into {module_name}."
            if visited_modules else
            f"As a starting point in the course, {title_name} sets up the foundations used by later Transformer lessons."
        )
        analogy = (
            sections['faqs'][0]['answer']
            if sections['faqs'] else
            (sections['insights'][0]['detail'] if sections['insights'] else f"Think of {module_name} as organizing raw text tokens into meaningful building blocks.")
        )
        steps = [f"{ins['concept']}: {ins['detail']}" for ins in sections['insights'][:4]]
        if not steps:
            steps = sections['highlights'][:4] or [f"Understand the core idea and goal of {title_name}."]

        return {
            "agentId": cls.agent_id,
            "agentName": cls.name,
            "directive": directive,
            "intuition": intuition,
            "prerequisiteBridge": bridge,
            "realWorldAnalogy": analogy,
            "stepByStepBreakdown": steps,
            "source": "local-corpus-agent",
        }


class NotesSubAgent:
    """Subagent specializing in structured, editable study notes and takeaways."""
    agent_id = "notes"
    name = "Smart Notes"
    role = "Structured Lecture Synthesis"

    @classmethod
    def run(cls, entry: dict, directive: str, student_summaries: list = None, ai_client=None):
        student_summaries = student_summaries or []
        title_name = entry['title']
        module_name = entry['module']
        description = entry['description']
        summaries_digest = _format_student_summaries_context(student_summaries, max_items=5)

        if ai_client:
            try:
                model = _get_llm_model_name(ai_client)
                prompt = f"""You are the Smart Notes writer overseen by the Main Orchestrator.
Orchestrator Directive: "{directive}"
Current Lesson: "{title_name}" (Module: "{module_name}")
Student's Previous Summaries (tailor notes to fill gaps and build on what they wrote):
{summaries_digest}

Lecture Material Context:
{description[:3000]}

Return ONLY valid JSON with this exact schema:
{{
  "overview": "Concise 2-3 sentence plain-language summary of the lecture.",
  "takeaways": [
    "Clear takeaway 1",
    "Clear takeaway 2",
    "Clear takeaway 3",
    "Clear takeaway 4",
    "Clear takeaway 5"
  ],
  "examTip": "1 short sentence highlighting the most important distinction or thing to remember."
}}"""
                completion = ai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a specialized NLP Study Notes writer that outputs strict JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.35,
                    max_tokens=1500,
                )
                parsed = _extract_json_block(completion.choices[0].message.content)
                if parsed and parsed.get("overview") and isinstance(parsed.get("takeaways"), list):
                    takeaways = parsed["takeaways"][:5]
                    exam_tip = parsed.get("examTip", "")
                    note_lines = [f"## {title_name}", "", parsed["overview"], "", "### Key Takeaways"]
                    for t in takeaways:
                        note_lines.append(f"- {t}")
                    if exam_tip:
                        note_lines.extend(["", f"**Key Focus:** {exam_tip}"])
                    return {
                        "agentId": cls.agent_id,
                        "agentName": cls.name,
                        "directive": directive,
                        "module": module_name,
                        "title": f"Study Notes: {title_name}",
                        "lessonTitle": title_name,
                        "overview": parsed["overview"],
                        "takeaways": takeaways,
                        "examTip": exam_tip,
                        "formattedNote": "\n".join(note_lines),
                        "source": f"groq-{model}",
                    }
            except Exception as e:
                print(f"[NOTES_SUBAGENT] LLM fallback: {e}")

        sections = _extract_module_sections(description)
        takeaways = [f"{ins['concept']}: {ins['detail']}" for ins in sections['insights'][:4]]
        if len(takeaways) < 4:
            for hl in sections['highlights']:
                if hl not in takeaways:
                    takeaways.append(hl)
                if len(takeaways) >= 5:
                    break
        if not takeaways and sections['summary_sentences']:
            takeaways = sections['summary_sentences'][:4]

        overview = " ".join(sections['summary_sentences'][:3]) if sections['summary_sentences'] else (
            takeaways[0] if takeaways else f"Core concepts and foundations of {title_name}."
        )
        exam_tip = f"Focus on how {module_name} balances speed, memory, and model quality."

        note_lines = [f"## {title_name}", "", overview, "", "### Key Takeaways"]
        for t in takeaways[:5]:
            note_lines.append(f"- {t}")
        note_lines.extend(["", f"**Key Focus:** {exam_tip}"])

        return {
            "agentId": cls.agent_id,
            "agentName": cls.name,
            "directive": directive,
            "module": module_name,
            "title": f"Study Notes: {title_name}",
            "lessonTitle": title_name,
            "overview": overview,
            "takeaways": takeaways[:5],
            "examTip": exam_tip,
            "formattedNote": "\n".join(note_lines),
            "source": "local-corpus-agent",
        }


class FlashcardSubAgent:
    """Subagent specializing in active-recall flashcards and technical terminology."""
    agent_id = "flashcards"
    name = "Flashcards"
    role = "Active Recall & Terminology"

    @classmethod
    def run(cls, entry: dict, directive: str, student_summaries: list = None, ai_client=None):
        student_summaries = student_summaries or []
        title_name = entry['title']
        module_name = entry['module']
        description = entry['description']
        summaries_digest = _format_student_summaries_context(student_summaries, max_items=4)

        if ai_client:
            try:
                model = _get_llm_model_name(ai_client)
                prompt = f"""You are the Flashcard generator overseen by the Main Orchestrator.
Orchestrator Directive: "{directive}"
Current Lesson: "{title_name}" (Module: "{module_name}")
Student's Previous Summaries:
{summaries_digest}

Lecture Material Context:
{description[:2800]}

Generate 6 clear, simple active-recall flashcards for this lesson.
Return ONLY valid JSON with this exact schema:
{{
  "flashcards": [
    {{
      "front": "Term, architecture component, or short recall question",
      "back": "Clear, simple 1-2 sentence definition or answer",
      "tag": "Concept | Architecture | Mechanism | Practice"
    }}
  ]
}}"""
                completion = ai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a specialized NLP Flashcard generator that outputs strict JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.35,
                    max_tokens=1500,
                )
                parsed = _extract_json_block(completion.choices[0].message.content)
                if parsed and isinstance(parsed.get("flashcards"), list) and len(parsed["flashcards"]) > 0:
                    cards = []
                    for c in parsed["flashcards"][:6]:
                        cards.append({
                            "front": str(c.get("front", "")),
                            "back": str(c.get("back", "")),
                            "tag": str(c.get("tag", "Concept")),
                        })
                    return {
                        "agentId": cls.agent_id,
                        "agentName": cls.name,
                        "directive": directive,
                        "flashcards": cards,
                        "source": f"groq-{model}",
                    }
            except Exception as e:
                print(f"[FLASHCARD_SUBAGENT] LLM fallback: {e}")

        sections = _extract_module_sections(description)
        flashcards = []
        for g in sections['glossary'][:5]:
            flashcards.append({'front': g['term'], 'back': g['definition'], 'tag': 'Terminology'})
        for ins in sections['insights'][:4]:
            if len(flashcards) < 6:
                flashcards.append({'front': ins['concept'], 'back': ins['detail'], 'tag': 'Core Concept'})
        for faq in sections['faqs'][:2]:
            if len(flashcards) < 6:
                flashcards.append({'front': faq['question'], 'back': faq['answer'], 'tag': 'Key Q&A'})

        return {
            "agentId": cls.agent_id,
            "agentName": cls.name,
            "directive": directive,
            "flashcards": flashcards[:6],
            "source": "local-corpus-agent",
        }


class QuizSubAgent:
    """Subagent specializing in 3-level adaptive multiple-choice quizzes ('easy', 'medium', 'hard')."""
    agent_id = "quiz"
    name = "Concept Quiz"
    role = "Adaptive 3-Level Assessment"

    DIFFICULTY_GUIDANCE = {
        "easy": (
            "EASY level: Ask simple, foundational questions testing basic definitions and core purpose in plain words. "
            "Keep distractors clearly distinct from the right answer."
        ),
        "medium": (
            "MEDIUM level: Ask conceptual understanding questions about how mechanisms work, why design choices matter, "
            "and how concepts connect to the student's previous summaries."
        ),
        "hard": (
            "HARD level: Ask challenging applied, comparative, or edge-case questions testing deep technical trade-offs, "
            "subtle distinctions, and multi-step reasoning with plausible distractors."
        ),
    }

    @classmethod
    def run(
        cls,
        entry: dict,
        directive: str,
        nlp_resources: list,
        ai_client=None,
        num_questions: int = 3,
        difficulty_level: str = "medium",
        student_summaries: list = None,
    ):
        student_summaries = student_summaries or []
        level_key = str(difficulty_level or "medium").strip().lower()
        if level_key not in ("easy", "medium", "hard"):
            level_key = "medium"

        title_name = entry['title']
        module_name = entry['module']
        description = entry['description']
        summaries_digest = _format_student_summaries_context(student_summaries, max_items=5)
        diff_instruction = cls.DIFFICULTY_GUIDANCE[level_key]

        if ai_client:
            try:
                model = _get_llm_model_name(ai_client)
                prompt = f"""You are the Adaptive Quiz generator overseen by the Main Orchestrator.
Orchestrator Directive: "{directive}"
Selected Quiz Difficulty: {level_key.upper()} — {diff_instruction}
Generate {num_questions} multiple-choice quiz questions for "{title_name}" (Module: "{module_name}").

Student's Previous Summaries (adapt questions to reinforce weak areas or build on what they already wrote):
{summaries_digest}

Lecture Material:
{description[:3000]}

Return ONLY valid JSON with this exact structure:
{{
  "questions": [
    {{
      "id": "q1",
      "question": "Clear question calibrated to {level_key.upper()} difficulty?",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correctIndex": 0,
      "explanation": "Brief, plain-language 1-2 sentence explanation of why this option is correct."
    }}
  ]
}}"""
                completion = ai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are an expert NLP Quiz generator outputting strict JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.4,
                    max_tokens=1500,
                )
                parsed = _extract_json_block(completion.choices[0].message.content)
                if parsed and isinstance(parsed.get("questions"), list) and len(parsed["questions"]) > 0:
                    return {
                        "agentId": cls.agent_id,
                        "agentName": cls.name,
                        "directive": directive,
                        "difficulty": level_key,
                        "module": module_name,
                        "title": title_name,
                        "source": f"groq-{model}",
                        "questions": parsed["questions"][:num_questions],
                    }
            except Exception as e:
                print(f"[QUIZ_SUBAGENT] LLM fallback: {e}")

        # Local corpus fallback calibrated by easy / medium / hard
        sections = _extract_module_sections(description)
        distractor_pool = _collect_distractor_pool(module_name, nlp_resources)
        rng = random.Random(f"{module_name}_{level_key}_{len(description)}")
        candidate_qas = []

        if level_key == "easy":
            for g in sections['glossary']:
                ans = g['definition'][:155].rstrip() + ('...' if len(g['definition']) > 155 else '')
                candidate_qas.append({
                    'question': f"[Easy] What does '{g['term']}' mean in '{title_name}'?",
                    'correct': ans,
                    'explanation': f"'{g['term']}' means: {g['definition']}",
                })
            for hl in (sections['highlights'] + sections['summary_sentences'])[:5]:
                ans = hl[:155].rstrip() + ('...' if len(hl) > 155 else '')
                candidate_qas.append({
                    'question': f"[Easy] Which statement is true about '{title_name}'?",
                    'correct': ans,
                    'explanation': f"Covered in '{title_name}': {hl}",
                })
        elif level_key == "hard":
            for faq in sections['faqs']:
                ans = faq['answer'][:155].rstrip() + ('...' if len(faq['answer']) > 155 else '')
                candidate_qas.append({
                    'question': f"[Hard] {faq['question']}",
                    'correct': ans,
                    'explanation': f"In '{title_name}': {faq['answer']}",
                })
            for ins in sections['insights']:
                ans = ins['detail'][:155].rstrip() + ('...' if len(ins['detail']) > 155 else '')
                candidate_qas.append({
                    'question': f"[Hard] Why is '{ins['concept']}' technically important in '{title_name}'?",
                    'correct': ans,
                    'explanation': f"Technical insight ({ins['concept']}): {ins['detail']}",
                })
        else:
            for ins in sections['insights']:
                ans = ins['detail'][:155].rstrip() + ('...' if len(ins['detail']) > 155 else '')
                candidate_qas.append({
                    'question': f"What is a key takeaway regarding '{ins['concept']}' in '{title_name}'?",
                    'correct': ans,
                    'explanation': f"Key insight from '{title_name}' ({ins['concept']}): {ins['detail']}",
                })
            for g in sections['glossary']:
                ans = g['definition'][:155].rstrip() + ('...' if len(g['definition']) > 155 else '')
                candidate_qas.append({
                    'question': f"In '{title_name}', how is '{g['term']}' best described?",
                    'correct': ans,
                    'explanation': f"'{g['term']}' is defined as: {g['definition']}",
                })

        # Fill remaining slots from any available section if needed
        for hl in (sections['highlights'] + sections['summary_sentences']):
            if len(candidate_qas) >= num_questions:
                break
            ans = hl[:155].rstrip() + ('...' if len(hl) > 155 else '')
            candidate_qas.append({
                'question': f"Which statement accurately reflects a concept in '{title_name}'?",
                'correct': ans,
                'explanation': f"Directly covered in '{title_name}': {hl}",
            })

        if not candidate_qas:
            candidate_qas.append({
                'question': f"What is the main focus of '{title_name}'?",
                'correct': f"Understanding {module_name} concepts, methods, and practical usage.",
                'explanation': f"This lesson focuses on {module_name}.",
            })

        selected_qas = candidate_qas[:num_questions]
        questions = []
        for idx, item in enumerate(selected_qas):
            correct_opt = item['correct']
            available_distractors = [d for d in distractor_pool if d.lower() != correct_opt.lower()]
            chosen_distractors = rng.sample(available_distractors, min(3, len(available_distractors)))
            options = [correct_opt] + chosen_distractors
            rng.shuffle(options)
            correct_index = options.index(correct_opt)
            questions.append({
                'id': f"q_{entry['id']}_{level_key}_{idx + 1}",
                'question': item['question'],
                'options': options,
                'correctIndex': correct_index,
                'explanation': item['explanation'],
            })

        return {
            "agentId": cls.agent_id,
            "agentName": cls.name,
            "directive": directive,
            "difficulty": level_key,
            "module": module_name,
            "title": title_name,
            "source": "local-corpus-agent",
            "questions": questions,
        }


# ============================================================================
# MAIN ORCHESTRATOR AGENT (SUMMARY & PROGRESS AWARE)
# ============================================================================

class OrchestratorAgent:
    """
    Main Hierarchical Orchestrator Agent.
    Always receives the latest student status (visited modules, level, AND all
    previous summaries written across lessons), decides which study tool is best
    suited right now, and issues tailored directives to each subagent.
    """

    @classmethod
    def plan_allocation(
        cls,
        entry: dict,
        visited_modules: list,
        student_level: int = 1,
        student_summaries: list = None,
        ai_client=None,
    ):
        student_summaries = student_summaries or []
        title = entry['title']
        module = entry['module']
        difficulty = entry['difficulty']
        nature = _classify_lesson_nature(title, module, difficulty)
        is_visited = any(
            str(m).lower() in (title.lower(), module.lower(), str(entry['id']).lower())
            for m in (visited_modules or [])
        )
        has_summarized_this = any(
            title.lower() in str(s.get('title', '')).lower() or module.lower() in str(s.get('title', '')).lower()
            for s in student_summaries
        )
        summaries_digest = _format_student_summaries_context(student_summaries, max_items=6)

        # Recommend default quiz difficulty from student progress
        suggested_quiz_level = "easy" if len(student_summaries) == 0 else ("hard" if (has_summarized_this and student_level >= 3) else "medium")

        if has_summarized_this or is_visited:
            default_primary = "quiz"
            default_sequence = ["quiz", "flashcards", "notes", "coach"]
            default_reason = (
                f"You've already covered '{title}' ({len(student_summaries)} total summaries written). "
                f"A {suggested_quiz_level.title()} Concept Quiz is the best way to check your retention right now."
            )
        elif "Hands-On" in nature:
            default_primary = "notes"
            default_sequence = ["notes", "coach", "flashcards", "quiz"]
            default_reason = (
                f"'{title}' is a hands-on lesson. Start with Smart Notes to capture key steps, "
                f"then customize them with your own notes."
            )
        elif difficulty >= 5 or "Mathematical" in nature or "Advanced" in nature:
            default_primary = "coach"
            default_sequence = ["coach", "notes", "flashcards", "quiz"]
            default_reason = (
                f"'{title}' is an advanced topic (Difficulty {difficulty}/7). Start with Concept Coach "
                f"for simple intuition connected to your {len(student_summaries)} previous summaries."
            )
        else:
            default_primary = "notes"
            default_sequence = ["notes", "flashcards", "coach", "quiz"]
            default_reason = (
                f"'{title}' introduces core ideas in {nature}. Smart Notes will give you a quick, "
                f"editable study brief before you test yourself."
            )

        default_directives = {
            "coach": f"Build simple intuition for '{title}' connected to the student's {len(student_summaries)} past summaries.",
            "notes": f"Write clear, concise study notes for '{title}' building on the student's current progress (Stage {student_level}).",
            "flashcards": f"Create 6 quick recall flashcards for key terms in '{title}'.",
            "quiz": f"Create {suggested_quiz_level}-level questions for '{title}' adapted to the student's {len(student_summaries)} summaries.",
        }

        if ai_client:
            try:
                model = _get_llm_model_name(ai_client)
                prompt = f"""You are the Main Orchestrator of an NLP Learning Platform overseeing 4 study tools:
1. "coach" (Concept Coach): Simple mental models, analogies, and bridges from past lessons.
2. "notes" (Smart Notes): Structured, editable overview and key takeaways.
3. "flashcards" (Flashcards): Active-recall terminology flip-cards.
4. "quiz" (Concept Quiz): Adaptive Easy / Medium / Hard multiple-choice checks.

LATEST STUDENT STATUS:
- Active Lesson: "{title}" (Module: "{module}", Difficulty: {difficulty}/7, Type: "{nature}")
- Already Visited This Lesson: {is_visited}
- Already Summarized This Lesson: {has_summarized_this}
- Total Visited Modules: {len(visited_modules)} (Stage {student_level})

ALL PREVIOUS STUDENT SUMMARIES:
{summaries_digest}

Decide which tool should be recommended first, recommend a quiz difficulty ("easy" | "medium" | "hard"), explain why in 1-2 short, simple sentences, and give each tool a tailored 1-sentence directive.

Return ONLY valid JSON:
{{
  "primaryAgent": "coach | notes | flashcards | quiz",
  "suggestedQuizDifficulty": "easy | medium | hard",
  "recommendedSequence": ["primary_id", "second_id", "third_id", "fourth_id"],
  "reasoning": "1-2 short, simple sentences explaining what to use next for '{title}'.",
  "directives": {{
    "coach": "Tailored directive...",
    "notes": "Tailored directive...",
    "flashcards": "Tailored directive...",
    "quiz": "Tailored directive..."
  }}
}}"""
                completion = ai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are the Main Orchestrator. Output strict JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,
                    max_tokens=1200,
                )
                parsed = _extract_json_block(completion.choices[0].message.content)
                valid_ids = {"coach", "notes", "flashcards", "quiz"}
                if parsed and parsed.get("primaryAgent") in valid_ids:
                    primary = parsed["primaryAgent"]
                    seq = [a for a in (parsed.get("recommendedSequence") or []) if a in valid_ids]
                    for v in default_sequence:
                        if v not in seq:
                            seq.append(v)
                    directives = {
                        k: (parsed.get("directives") or {}).get(k) or default_directives[k]
                        for k in ("coach", "notes", "flashcards", "quiz")
                    }
                    rec_diff = parsed.get("suggestedQuizDifficulty", suggested_quiz_level)
                    if rec_diff not in ("easy", "medium", "hard"):
                        rec_diff = suggested_quiz_level
                    return {
                        "lessonProfile": {
                            "id": entry["id"],
                            "title": title,
                            "module": module,
                            "difficulty": difficulty,
                            "nature": nature,
                            "isVisited": is_visited,
                            "summariesCount": len(student_summaries),
                        },
                        "primaryAgent": primary,
                        "suggestedQuizDifficulty": rec_diff,
                        "recommendedSequence": seq,
                        "reasoning": parsed.get("reasoning") or default_reason,
                        "directives": directives,
                        "source": f"groq-{model}",
                    }
            except Exception as e:
                print(f"[ORCHESTRATOR] LLM routing fallback: {e}")

        return {
            "lessonProfile": {
                "id": entry["id"],
                "title": title,
                "module": module,
                "difficulty": difficulty,
                "nature": nature,
                "isVisited": is_visited,
                "summariesCount": len(student_summaries),
            },
            "primaryAgent": default_primary,
            "suggestedQuizDifficulty": suggested_quiz_level,
            "recommendedSequence": default_sequence,
            "reasoning": default_reason,
            "directives": default_directives,
            "source": "local-orchestrator",
        }

    @classmethod
    def orchestrate(
        cls,
        module_or_title: str,
        nlp_resources: list,
        visited_modules: list = None,
        student_level: int = 1,
        ai_client=None,
        active_subagent: str = None,
        student_summaries: list = None,
        quiz_difficulty: str = None,
    ):
        """
        Run the Main Orchestrator with the student's latest status & summaries.
        If active_subagent is provided ('coach', 'notes', 'flashcards', 'quiz'),
        executes that subagent directly for fast independent-feature UX;
        otherwise executes all 4 subagents.
        """
        visited_modules = visited_modules or []
        student_summaries = student_summaries or []
        entry = _find_module_entry(module_or_title, nlp_resources)
        plan = cls.plan_allocation(
            entry,
            visited_modules,
            student_level,
            student_summaries=student_summaries,
            ai_client=ai_client,
        )

        target_agent = active_subagent if active_subagent in ("coach", "notes", "flashcards", "quiz") else plan["primaryAgent"]
        directives = plan["directives"]
        q_diff = quiz_difficulty if quiz_difficulty in ("easy", "medium", "hard") else plan["suggestedQuizDifficulty"]

        outputs = {}
        if not active_subagent or active_subagent == "coach":
            outputs["coach"] = ConceptCoachSubAgent.run(
                entry, directives["coach"], visited_modules, student_summaries=student_summaries, ai_client=ai_client
            )
        if not active_subagent or active_subagent == "notes":
            outputs["notes"] = NotesSubAgent.run(
                entry, directives["notes"], student_summaries=student_summaries, ai_client=ai_client
            )
        if not active_subagent or active_subagent == "flashcards":
            outputs["flashcards"] = FlashcardSubAgent.run(
                entry, directives["flashcards"], student_summaries=student_summaries, ai_client=ai_client
            )
        if not active_subagent or active_subagent == "quiz":
            outputs["quiz"] = QuizSubAgent.run(
                entry,
                directives["quiz"],
                nlp_resources,
                ai_client=ai_client,
                num_questions=3,
                difficulty_level=q_diff,
                student_summaries=student_summaries,
            )

        subagents_summary = [
            {
                "id": sid,
                "name": sname,
                "role": srole,
                "directive": directives[sid],
                "status": "primary" if plan["primaryAgent"] == sid else "allocated",
                "source": outputs[sid]["source"] if sid in outputs else plan["source"],
            }
            for sid, sname, srole in (
                ("coach", ConceptCoachSubAgent.name, ConceptCoachSubAgent.role),
                ("notes", NotesSubAgent.name, NotesSubAgent.role),
                ("flashcards", FlashcardSubAgent.name, FlashcardSubAgent.role),
                ("quiz", QuizSubAgent.name, QuizSubAgent.role),
            )
        ]

        return {
            "orchestrator": {
                "lessonProfile": plan["lessonProfile"],
                "primaryAgent": plan["primaryAgent"],
                "suggestedQuizDifficulty": plan["suggestedQuizDifficulty"],
                "activeAgent": target_agent,
                "recommendedSequence": plan["recommendedSequence"],
                "reasoning": plan["reasoning"],
                "source": plan["source"],
                "subagents": subagents_summary,
            },
            "outputs": outputs,
        }


# ============================================================================
# FEATURE-SPECIFIC & BACKWARD-COMPATIBLE WRAPPERS
# ============================================================================

def generate_module_quiz(
    module_or_title: str,
    nlp_resources: list,
    ai_client=None,
    num_questions: int = 3,
    difficulty: str = "medium",
    student_summaries: list = None,
    visited_modules: list = None,
    student_level: int = 1,
):
    entry = _find_module_entry(module_or_title, nlp_resources)
    student_summaries = student_summaries or []
    directive = (
        f"Assess '{entry['title']}' at {difficulty.upper()} difficulty, tailored to the student's "
        f"Stage {student_level} progress and {len(student_summaries)} past summaries."
    )
    return QuizSubAgent.run(
        entry,
        directive,
        nlp_resources,
        ai_client=ai_client,
        num_questions=num_questions,
        difficulty_level=difficulty,
        student_summaries=student_summaries,
    )


def generate_study_notes(
    module_or_title: str,
    nlp_resources: list,
    ai_client=None,
    student_summaries: list = None,
    student_level: int = 1,
):
    entry = _find_module_entry(module_or_title, nlp_resources)
    student_summaries = student_summaries or []
    notes_res = NotesSubAgent.run(
        entry,
        f"Synthesize clear, editable study notes for '{entry['title']}' tailored to Stage {student_level}.",
        student_summaries=student_summaries,
        ai_client=ai_client,
    )
    cards_res = FlashcardSubAgent.run(
        entry,
        f"Extract key flashcards for '{entry['title']}'.",
        student_summaries=student_summaries,
        ai_client=ai_client,
    )
    return {
        "module": notes_res["module"],
        "title": notes_res["title"],
        "lessonTitle": notes_res["lessonTitle"],
        "overview": notes_res["overview"],
        "takeaways": notes_res["takeaways"],
        "examTip": notes_res.get("examTip", ""),
        "flashcards": cards_res["flashcards"],
        "formattedNote": notes_res["formattedNote"],
        "source": notes_res["source"],
    }
