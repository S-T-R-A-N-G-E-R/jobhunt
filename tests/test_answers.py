import pytest

from jobhunt.answers import Answers, as_text, clean, job_country, lookup, pick_option

A = Answers.model_validate(
    {
        "identity": {"full_name": "Alex Q Example", "preferred_name": "Alex",
                     "email": "alex@example.com", "phone": "+91 90000 00000",
                     "city": "Bengaluru", "country": "India",
                     "linkedin": "https://linkedin.com/in/example"},
        "work": {"current_company": "Example Corp", "current_title": "ML Engineer",
                 "years_total": 1, "years_relevant": 1, "notice_period": "30 days",
                 "current_ctc_lpa": 12, "expected_ctc_lpa": 20, "expected_usd": 60000,
                 "earliest_start": "30 days", "authorized_india": True,
                 "needs_sponsorship_elsewhere": True},
        "education": [{"degree": "M.Sc.", "field": "Data Science",
                       "institution": "Example University", "graduation_year": 2026}],
        "standard": {"eeo": {"gender": "Male", "hispanic_latino": "No", "race_ethnicity": "Asian",
                             "veteran": "I am not a protected veteran",
                             "disability": "No, I don't have a disability"}},
        "custom": [{"q": "Are you willing to work in shifts?", "a": "Yes"}],
    }
)  # fmt: skip

BLR, SF = "Bengaluru, Karnataka, India", "San Francisco, CA"


def value(label, kind="text", location=BLR):
    hit = lookup(label, kind, A, location)
    return hit.value if hit else None


@pytest.mark.parametrize(
    "label, expected",
    [
        ("First Name*", "Alex"),
        ("Last Name*", "Q Example"),
        ("Name", "Alex Q Example"),
        ("Full name\n✱", "Alex Q Example"),
        ("Legal Name", "Alex Q Example"),
        ("Preferred Name (if applicable)", "Alex"),
        ("Email*", "alex@example.com"),
        ("Phone Number", "+91 90000 00000"),
        ("LinkedIn Profile", "https://linkedin.com/in/example"),
        ("Current company ✱", "Example Corp"),
        ("Current CTC", "12 LPA"),
        ("Expected CTC", "20 LPA"),
        ("Notice Period", "30 days"),
        ("When is the earliest you would want to start working with us?", "30 days"),
        ("Where are you currently located?", "Bengaluru, India"),
        ("How did you hear about this job?", "Company careers page"),
        ("Total years of experience", "1"),
        ("School", "Example University"),
        ("Are you willing to work in shifts", "Yes"),  # custom, near match
        ("GitHub URL", None),  # not in the bank: left empty, never invented
        ("Why Example Corp?", None),  # free text: drafted elsewhere
    ],
)
def test_lookup(label, expected):
    assert value(label) == expected


def test_lever_paragraph_label_uses_the_final_question():
    label = (
        "As part of our commitment to a diverse workplace, we invite you to disclose your "
        "gender. Information about race, disability or veteran status is never used in any "
        "decision. Please select the option that best describes your gender:✱"
    )
    assert clean(label) == "please select the option that best describes your gender"
    assert value(label, "radio") == "Male"


def test_authorization_depends_on_where_the_job_is():
    sponsor = "Will you now or will you in the future require employment visa sponsorship?"
    authorized = "Are you authorized to work in the country where the job is located?"
    assert value(sponsor, "combobox", BLR) is False
    assert value(sponsor, "combobox", SF) is True
    assert value(authorized, "yesno", BLR) is True
    assert value(authorized, "yesno", SF) is False
    assert value(sponsor, "combobox", "Remote") is None  # unclear: left for you
    assert value("Expected salary", "text", SF) == "USD 60,000"
    assert value("Expected salary", "text", "Remote") is None


def test_skill_specific_experience_is_medium_confidence():
    assert lookup("Years of experience with Kubernetes", "text", A, BLR).confidence == "medium"
    assert lookup("Total years of experience", "text", A, BLR).confidence == "high"


def test_job_country():
    assert job_country("Bangalore") == "india"
    assert job_country("Remote - India") == "india"
    assert job_country("London") == "abroad"
    assert job_country("Remote") is None and job_country(None) is None


@pytest.mark.parametrize(
    "answer, options, expected",
    [
        ("Male", ["Male", "Female", "Decline to self-identify"], "Male"),
        ("Asian", ["White (Not Hispanic or Latino)", "Asian (Not Hispanic or Latino)",
                   "Native Hawaiian or Other Pacific Islander"], "Asian (Not Hispanic or Latino)"),
        ("No, I don't have a disability",
         ["Yes, I have a disability (or previously had a disability)",
          "No, I do not have a disability and have not had one in the past",
          "I do not want to answer"],
         "No, I do not have a disability and have not had one in the past"),
        ("I am not a protected veteran",
         ["I identify as one or more of the classifications of protected veteran",
          "I am not a protected veteran", "I decline to self-identify"],
         "I am not a protected veteran"),
        (True, ["Yes", "No"], "Yes"),
        (False, ["Yes, I will require sponsorship", "No, I will not"], "No, I will not"),
        (True, ["Yes", "Yes, with conditions", "No"], None),  # two yeses: ambiguous
        ("1", ["Below 2 yrs", "3 yrs to 7 yrs", "7+ yrs"], None),  # ranges aren't guessed
        ("Data Science", ["Data Science and AI", "Data Science (Applied)"], None),
        ("Company careers page", ["LinkedIn", "Referral", "Other"], None),
    ],
)  # fmt: skip
def test_pick_option_only_clear_matches(answer, options, expected):
    assert pick_option(answer, options) == expected


def test_as_text():
    assert as_text(True) == "Yes" and as_text(False) == "No" and as_text("x") == "x"
