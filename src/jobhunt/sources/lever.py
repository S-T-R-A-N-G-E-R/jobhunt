from datetime import UTC, datetime

from ..models import Job, html_to_text, looks_remote, normalize_ws


def api_url(slug: str, region: str | None = None) -> str:
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    return f"https://{host}/v0/postings/{slug}?mode=json"


def _comp(j: dict) -> str | None:
    s = j.get("salaryRange")
    if not s or s.get("min") is None:
        return normalize_ws(j.get("salaryDescriptionPlain") or "") or None
    interval = (s.get("interval") or "").replace("-", " ")
    return f"{s.get('currency', '')} {s['min']:,}–{s.get('max', s['min']):,} {interval}".strip()


def parse(data: list, company: str) -> list[Job]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        sections = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            sections.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        sections.append(j.get("additionalPlain") or "")
        created = j.get("createdAt")
        jobs.append(
            Job(
                source="lever",
                company=company,
                external_id=j["id"],
                title=j["text"].strip(),
                location=location,
                is_remote=j.get("workplaceType") == "remote" or looks_remote(location),
                department=cats.get("team") or cats.get("department"),
                url=j["hostedUrl"],
                description_text=normalize_ws("\n\n".join(s for s in sections if s)),
                compensation_text=_comp(j),
                posted_at=(
                    datetime.fromtimestamp(created / 1000, UTC).isoformat() if created else None
                ),
            )
        )
    return jobs
