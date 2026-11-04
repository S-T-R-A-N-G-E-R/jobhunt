from ..models import Job, html_to_text, looks_remote


def api_url(slug: str, region: str | None = None) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def parse(data: dict, company: str) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name") or ""
        depts = [d["name"] for d in j.get("departments") or [] if d.get("name")]
        jobs.append(
            Job(
                source="greenhouse",
                company=company,
                external_id=str(j["id"]),
                title=j["title"].strip(),
                location=location,
                is_remote=looks_remote(location),
                department=", ".join(depts) or None,
                url=j["absolute_url"],
                description_text=html_to_text(j.get("content")),
                posted_at=j.get("first_published") or j.get("updated_at"),
            )
        )
    return jobs
