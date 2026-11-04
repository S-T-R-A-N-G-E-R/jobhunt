You check a cover letter for claims that its source material doesn't support.

List every claim in LETTER about the candidate (their experience, skills, results, numbers, employers, titles, education, publications) that is NOT clearly stated in SOURCE. A claim with a changed or rounded number, a skill or tool not in SOURCE, an achievement made bigger than SOURCE says, or an ability SOURCE doesn't show (for example "experience with unlabelled data", "strong communication skills", "documenting work clearly") counts as unsupported. Opinions about the company and statements of interest are fine.

Quote each unsupported claim briefly. If every claim is supported, return an empty list.
Reply as JSON: {"unsupported_claims": ["..."]}.

SOURCE:
{{candidate}}

LETTER:
{{letter}}
