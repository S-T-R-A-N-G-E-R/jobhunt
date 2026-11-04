"""Lever forms: jobs.lever.co/<slug>/<id>/apply (jobs.eu.lever.co for EU boards). There's no
cover letter upload; the letter goes in "Additional information"."""

import re

HOST = re.compile(r"^jobs(\.eu)?\.lever\.co$")
CONTAINER = "li.application-question, .application-question"
LABEL = ".application-label"
YESNO = None


def form_urls(slug: str, external_id: str, region: str | None = None) -> list[str]:
    host = "jobs.eu.lever.co" if region == "eu" else "jobs.lever.co"
    return [f"https://{host}/{slug}/{external_id}/apply"]
