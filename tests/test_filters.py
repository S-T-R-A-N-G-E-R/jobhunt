from jobhunt.config import Profile
from jobhunt.scoring import hard_filter, title_matches

PROFILE = Profile(
    target_titles=["Senior Backend Engineer", "Staff Engineer"],
    title_exclude_keywords=["intern"],
    locations={"accept": ["Bengaluru", "Remote - India"], "remote_ok": True},
)


def job(title, location="Bengaluru", remote=False):
    return {"title": title, "location": location, "is_remote": int(remote)}


def test_title_matching():
    assert title_matches("Backend Engineer II", PROFILE.target_titles)
    assert title_matches("Back-end Engineer (Payments)", PROFILE.target_titles)
    assert not title_matches("Senior Account Executive", PROFILE.target_titles)
    # Words must appear as a phrase, in order: "AI Engineer" is not "Engineer, Enterprise AI".
    assert title_matches("AI Engineer, Codex", ["AI Engineer"])
    assert not title_matches("Software Engineer, Enterprise AI", ["AI Engineer"])
    assert not title_matches("Applied AI Architect", ["Applied AI Engineer"])


def test_excluded_keyword():
    assert "intern" in hard_filter(job("Backend Engineer Intern"), PROFILE)


def test_locations():
    assert hard_filter(job("Backend Engineer", "Bangalore, Karnataka"), PROFILE) is None
    assert hard_filter(job("Backend Engineer", "Remote, India"), PROFILE) is None
    assert hard_filter(job("Backend Engineer", "Remote, US", remote=True), PROFILE) is None
    assert "location" in hard_filter(job("Backend Engineer", "London"), PROFILE)
    no_remote = PROFILE.model_copy(
        update={
            "locations": PROFILE.locations.model_copy(
                update={"remote_ok": False, "accept": ["Bengaluru"]}
            )
        }
    )
    assert hard_filter(job("Backend Engineer", "Remote, US", remote=True), no_remote)


def test_relocation_ok_lets_any_location_through():
    relocate = PROFILE.model_copy(
        update={"locations": PROFILE.locations.model_copy(update={"relocation_ok": True})}
    )
    assert hard_filter(job("Backend Engineer", "London"), relocate) is None
    assert "title" in hard_filter(job("Account Executive", "London"), relocate)


def test_final_score_guardrails():
    from jobhunt.models import ScoreOutput
    from jobhunt.scoring import final_score

    def out(score, seniority, verdict="apply"):
        return ScoreOutput(
            score=score,
            verdict=verdict,
            reasons=[],
            red_flags=[],
            seniority_match=seniority,
            extracted_comp=None,
            one_line_pitch="",
        )

    assert final_score(out(92, "over"), high_priority=True) == (40, "skip", 97)
    assert final_score(out(98, "match"), high_priority=True) == (100, "apply", 100)
    assert final_score(out(60, "match", verdict="skip"), high_priority=False) == (60, "maybe", 60)


def test_reversed_title_order_matches():
    from jobhunt.scoring import reorder_title

    assert reorder_title("Engineer, Machine Learning (India)") == "Machine Learning Engineer"
    assert reorder_title("Software Engineer, Backend - Remote") == "Backend Software Engineer"
    assert (
        reorder_title("ML Engineer, Search, Ads") == "ML Engineer, Search, Ads"
    )  # 2 commas: as is
    targets = ["Machine Learning Engineer"]
    assert title_matches("Machine Learning Engineer", targets)
    assert title_matches("Engineer, Machine Learning", targets)
    assert title_matches("Senior Engineer, Machine Learning - Remote", targets)
    assert title_matches("Software Engineer, Backend", ["Backend Software Engineer"])
    assert title_matches("Backend Software Engineer", ["Backend Software Engineer"])
    # Reordering must not let unrelated titles through.
    assert not title_matches("Software Engineer, Enterprise AI", ["AI Engineer"])
    assert not title_matches("Engineer, Machine Learning Infrastructure Sales", ["Data Scientist"])
