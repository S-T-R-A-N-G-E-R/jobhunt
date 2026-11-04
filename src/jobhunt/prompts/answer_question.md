Draft the candidate's answer to one question on a job application form.

Rules:
- Use only facts in CANDIDATE. Never invent experience, numbers, links, names, dates or opinions the candidate hasn't stated.
- If CANDIDATE doesn't contain what the question asks for (for example a URL, a personal preference, a yes/no fact about their history), reply with an empty answer and confidence "low".
- {{length}}
- Plain, specific English in the first person. No clichés.
- confidence: "high" if the answer follows directly from CANDIDATE, "medium" if it needs some judgement, "low" if you're unsure or the answer is empty.

Reply as JSON: {"answer": "...", "confidence": "high" | "medium" | "low"}.

QUESTION: {{question}}

CANDIDATE:
{{candidate}}

JOB: {{title}} at {{company}}
{{job}}
