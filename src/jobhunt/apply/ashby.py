"""Ashby forms: jobs.ashbyhq.com/<slug>/<id>/application. Each question is a [data-field-path]
block. Yes/No questions are two buttons. The "autofill from resume" upload at the top sits
outside every block, so it's never touched."""

import re

HOST = re.compile(r"^jobs\.ashbyhq\.com$")
CONTAINER = "[data-field-path]"
LABEL = ".ashby-application-form-question-title"
YESNO = ".ashby-application-form-input-yesno"


def form_urls(slug: str, external_id: str, region: str | None = None) -> list[str]:
    return [f"https://jobs.ashbyhq.com/{slug}/{external_id}/application"]
