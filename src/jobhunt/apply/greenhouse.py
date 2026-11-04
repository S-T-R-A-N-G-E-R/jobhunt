"""Greenhouse forms: job-boards.greenhouse.io, or its embed when the company's own careers site
wraps the form in an iframe (the hosted page then redirects away)."""

import re

HOST = re.compile(r"(^|\.)greenhouse\.io$")
CONTAINER = ".field-wrapper, .field, fieldset"
LABEL = ".label, legend, label"  # the upload label is a div.label; the hidden "Attach" a label
YESNO = None


def form_urls(slug: str, external_id: str, region: str | None = None) -> list[str]:
    return [
        f"https://job-boards.greenhouse.io/{slug}/jobs/{external_id}",
        f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={external_id}",
    ]
