from ..models import Job, html_to_text, normalize_ws


def api_url(slug: str, region: str | None = None) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def parse(data: dict, company: str) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        locs += [s.get("location", "") for s in j.get("secondaryLocations") or []]
        location = "; ".join(x for x in locs if x)
        comp = j.get("compensation") or {}
        description = normalize_ws(j.get("descriptionPlain") or "") or html_to_text(
            j.get("descriptionHtml")
        )
        jobs.append(
            Job(
                source="ashby",
                company=company,
                external_id=j["id"],
                title=j["title"].strip(),
                location=location,
                is_remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
                department=j.get("department") or j.get("team"),
                url=j["jobUrl"],
                description_text=description,
                compensation_text=comp.get("compensationTierSummary")
                or comp.get("scrapeableCompensationSalarySummary"),
                posted_at=j.get("publishedAt"),
            )
        )
    return jobs
