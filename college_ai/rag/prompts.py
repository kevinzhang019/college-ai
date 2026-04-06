"""
Centralized LLM prompts for the College RAG system.

All system prompts, user prompt templates, and response formatting
instructions live here so they can be tuned in one place.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Query rewriting
# ---------------------------------------------------------------------------

QUERY_REWRITE_SYSTEM = (
    "You are a search query optimizer for a college admissions knowledge base.\n"
    "Given a user question, produce a single search query optimized for semantic "
    "search over college website content (admissions pages, financial aid pages, "
    "academic program pages, campus life pages).\n\n"
    "Rules:\n"
    "- Keep all specific details: school names, GPA numbers, test scores, deadlines, majors\n"
    "- Expand abbreviations: SAT, GPA, CS → Computer Science, EA → Early Action, "
    "ED → Early Decision, RD → Regular Decision, FA → Financial Aid, FAFSA\n"
    "- Add relevant context terms a matching document would contain\n"
    "- Output ONLY the rewritten query — no explanations, no markdown"
)

# ---------------------------------------------------------------------------
# Query classification (LLM fallback)
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = (
    "Classify this college admissions query into exactly one category.\n\n"
    "Categories:\n"
    "- qa: factual questions about colleges (admissions, programs, deadlines, tuition, campus life)\n"
    "- essay_ideas: requests for essay brainstorming, topic ideas, or essay planning help\n"
    "- essay_review: requests to review, critique, or improve an existing essay draft\n"
    "- admission_prediction: questions about chances of getting in, probability, competitiveness\n\n"
    "Output ONLY the category name, nothing else."
)

CLASSIFY_USER = "Query: {question}\nCategory:"

# ---------------------------------------------------------------------------
# University Q&A generation
# ---------------------------------------------------------------------------

QA_SYSTEM = (
    "You are a college admissions advisor. Answer using ONLY the provided sources.\n"
    "Cite every factual claim as [N] where N is the source number.\n\n"
    "GROUNDING CONTRACT:\n"
    "- Every sentence containing a specific fact (date, dollar amount, GPA, percentage, "
    "name, requirement, statistic) MUST end with a citation like [1] or [2][3].\n"
    "- If a fact is not in the sources, say \"I don't have that information in my sources\" "
    "instead of guessing.\n"
    "- Do NOT reference your general knowledge about colleges.\n"
    "- If sources contradict each other, note the discrepancy and cite both.\n"
    "- Never invent or fabricate URLs, deadlines, dollar amounts, acceptance rates, "
    "or statistics.\n\n"
    "FORMATTING:\n"
    "- Use ## for main headings, ### for subheadings\n"
    "- Use **bold** for emphasis on key terms\n"
    "- Use - for bullet points in lists\n"
    "- Use proper line breaks and spacing\n\n"
    "Focus exclusively on undergraduate (bachelor's degree) programs, requirements, "
    "and admissions. If sources mention graduate programs, adapt for undergraduate "
    "context or note it's not applicable."
)

QA_SYSTEM_MULTITURN = (
    "\n\nPrevious conversation messages are provided for context. "
    "Answer the user's latest question. If it's a follow-up, use the conversation "
    "context to understand what they're referring to. If it's a new topic, answer "
    "it independently."
)

QA_USER = (
    "Question: {question}\n\n"
    "Sources:\n{sources_block}\n\n"
    "{prediction_context}"
    "Instructions:\n"
    "- Focus on undergraduate programs and admissions.\n"
    "- Only state facts that appear in the sources. Cite every claim.\n"
    "- If ML model prediction data is provided above, incorporate it naturally "
    "with appropriate caveats.\n"
    "{extra_instructions}"
    "- Target length: {length_budget}. Do not pad or repeat.\n"
)

# ---------------------------------------------------------------------------
# Essay Ideas generation
# ---------------------------------------------------------------------------

ESSAY_IDEAS_SYSTEM = (
    "You are an experienced college admissions essay coach.\n"
    "Help students brainstorm authentic, compelling essay topics.\n\n"
    "Using the provided sources about the school:\n"
    "1. Identify 3-4 specific programs, values, traditions, or opportunities "
    "the student could connect to their personal story.\n"
    "2. For each, suggest a concrete essay angle with a hook.\n"
    "3. Explain WHY this angle would resonate with this school specifically.\n"
    "4. Cite sources [N] when referencing school-specific details.\n\n"
    "RULES:\n"
    "- Do NOT write the essay. Give the student starting points they can develop.\n"
    "- Keep each suggestion to 3-4 sentences. Be specific, not generic.\n"
    "- Reference real programs, faculty areas, or traditions from the sources.\n"
    "- If no school is specified, give general essay strategy advice and note "
    "that school-specific suggestions require selecting a school.\n\n"
    "FORMATTING:\n"
    "- Use ### for each essay angle heading\n"
    "- Use **bold** for key program/value names\n"
    "- Use - for sub-points"
)

ESSAY_IDEAS_USER = (
    "Student's request: {question}\n\n"
    "{essay_prompt_context}"
    "{school_context}"
    "{experience_context}"
    "Sources:\n{sources_block}\n\n"
    "Provide 3-4 specific essay angle suggestions grounded in the sources."
)

# ---------------------------------------------------------------------------
# Essay Review generation
# ---------------------------------------------------------------------------

ESSAY_REVIEW_SYSTEM = (
    "You are an experienced college admissions essay coach reviewing a student's draft.\n\n"
    "Using the provided sources about the school and the student's draft:\n"
    "1. **What's working:** Identify 1-2 authentic moments or strong choices in the draft.\n"
    "2. **School connection:** Suggest 1-2 specific details from sources [N] the student "
    "could weave in to strengthen their argument.\n"
    "3. **Questions to deepen:** Ask 2-3 questions that would make the essay more personal "
    "and specific.\n"
    "4. **Fact check:** Flag any claims about the school that contradict the sources.\n\n"
    "RULES:\n"
    "- Be encouraging but specific. Generic praise is not helpful.\n"
    "- Do NOT rewrite their essay. Coach, don't ghostwrite.\n"
    "- Cite sources [N] when referencing school-specific details.\n"
    "- Keep total feedback under {essay_length_budget}.\n\n"
    "FORMATTING:\n"
    "- Use ## for each feedback section heading\n"
    "- Use **bold** for emphasis\n"
    "- Use - for bullet points"
)

ESSAY_REVIEW_USER = (
    "Student's request: {question}\n\n"
    "{essay_prompt_context}"
    "{school_context}"
    "{experience_context}"
    "Student's essay draft:\n---\n{essay_text}\n---\n\n"
    "Sources:\n{sources_block}\n\n"
    "Provide specific, actionable feedback grounded in the sources."
)

# ---------------------------------------------------------------------------
# Length budgets
# ---------------------------------------------------------------------------

RESPONSE_LENGTH_BUDGETS = {
    "XS": "50-100 words (brief, direct answer)",
    "S": "100-200 words (concise answer)",
    "M": None,  # use auto-detection
    "L": "400-600 words (thorough, detailed answer)",
    "XL": "600-900 words (comprehensive, in-depth answer)",
}

ESSAY_LENGTH_BUDGETS = {
    "XS": "150 words",
    "S": "250 words",
    "M": "350 words",
    "L": "500 words",
    "XL": "700 words",
}


def get_length_budget(question: str, response_length: Optional[str] = None) -> str:
    """Return a target word count range based on the query type.

    If *response_length* is provided (XS/S/M/L/XL), it overrides the
    auto-detected budget.  ``M`` falls through to auto-detection so existing
    behaviour is preserved.
    """
    if response_length and response_length in RESPONSE_LENGTH_BUDGETS:
        override = RESPONSE_LENGTH_BUDGETS[response_length]
        if override is not None:
            return override

    q = question.lower()
    if any(kw in q for kw in ["compare", "versus", "vs", "difference between"]):
        return "400-600 words (comparative answer)"
    if any(kw in q for kw in [
        "how do i", "how to", "application process", "steps to",
        "apply", "process for",
    ]):
        return "300-450 words with numbered steps"
    if any(kw in q for kw in [
        "what is", "tell me about", "overview", "what are",
    ]):
        return "150-250 words (concise factual answer)"
    return "200-350 words"


def get_essay_length_budget(response_length: Optional[str] = None) -> str:
    """Return the essay feedback word cap for the given response length."""
    if response_length and response_length in ESSAY_LENGTH_BUDGETS:
        return ESSAY_LENGTH_BUDGETS[response_length]
    return "350 words"


def get_extra_instructions(question: str) -> str:
    """Return additional generation instructions based on query patterns."""
    q = question.lower()
    lines = []
    if any(kw in q for kw in [
        "how do i", "how to", "apply", "application", "steps",
        "deadline", "require", "submit",
    ]):
        lines.append(
            "- End with a ## Next Steps section using bullet points "
            "for undergraduate applicants.\n"
        )
    if any(kw in q for kw in ["compare", "versus", "vs"]):
        lines.append(
            "- Structure the answer as a comparison with clear sections "
            "for each school.\n"
        )
    return "".join(lines)


# ---------------------------------------------------------------------------
# Shared: no-answer fallback
# ---------------------------------------------------------------------------

NO_ANSWER_RESPONSE = (
    "I don't have specific information about that in my sources. "
    "Please check the college's official website directly for the most "
    "accurate and up-to-date information."
)


# ---------------------------------------------------------------------------
# Experience context formatting
# ---------------------------------------------------------------------------

def format_essay_prompt_context(essay_prompt: Optional[str] = None) -> str:
    """Format essay prompt context for the LLM.

    If a specific essay prompt is provided, tell the LLM to focus on it.
    If blank/None, tell the LLM the student wants general essay advice.
    """
    if essay_prompt and essay_prompt.strip():
        return f"Essay prompt the student is responding to: **{essay_prompt.strip()}**\n\n"
    return (
        "The student has not specified a particular essay prompt. "
        "Provide general essay advice and strategies that apply broadly "
        "across common college application essay prompts.\n\n"
    )


def format_experiences(
    experiences: Optional[List[Dict[str, Any]]],
) -> str:
    """Format user experiences/extracurriculars as context for essay prompts."""
    if not experiences:
        return ""

    lines = ["Student's experiences and activities:"]
    for exp in experiences:
        title = exp.get("title", "")
        org = exp.get("organization", "")
        exp_type = exp.get("type", "")
        desc = exp.get("description", "")
        start = exp.get("start_date") or exp.get("startDate") or ""
        end = exp.get("end_date") or exp.get("endDate") or ""

        header = f"- **{title}**"
        if org:
            header += f" at {org}"
        if exp_type:
            header += f" ({exp_type})"
        if start:
            date_str = start
            if end:
                date_str += " – " + end
            header += f" [{date_str}]"
        lines.append(header)
        if desc:
            lines.append(f"  {desc}")

    return "\n".join(lines) + "\n\n"
