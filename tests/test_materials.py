import json

from jobhunt import config, materials
from jobhunt.answers import Answers

from .conftest import FakeLLM

A = Answers.model_validate(
    {"identity": {"full_name": "Alex Example", "city": "Bengaluru", "country": "India",
                  "phone": "+91 90000 00000", "email": "alex@example.com"},
     "work": {"current_title": "ML Engineer", "current_company": "Example Corp",
              "current_ctc_lpa": 13.5, "authorized_india": True},
     "standard": {"eeo": {"gender": "Male"}},
     "facts": ["Built a document-extraction pipeline at Example Corp"]}
)  # fmt: skip
JOB = {"id": 7, "company": "Fictional Labs", "title": "ML Engineer", "location": "Bengaluru",
       "description_text": "Build document AI."}  # fmt: skip


def letter(words=230):
    half = " ".join(["word"] * (words // 2))
    return json.dumps({"letter": f"{half}\n\n{half}"})


def check(*claims):
    return json.dumps({"unsupported_claims": list(claims)})


def writer(outputs):
    llm = FakeLLM(outputs)
    return materials.Writer(A, config.load_profile(), llm.transport()), llm


def test_letter_passes_check_first_time(home):
    w, llm = writer([letter(), check()])
    m = materials.prepare(JOB, A, w)
    assert llm.chat_calls == 2 and m.claims == []
    assert m.letter.startswith("Dear Hiring Team at Fictional Labs,")
    assert m.letter.rstrip().endswith("Regards,\nAlex Example")
    meta = json.loads((m.dir / "meta.json").read_text())
    assert meta["attempts"] == 1 and meta["letter_model"] == "ollama/gpt-oss:20b"
    # The writing model is Ollama Cloud, called with the API key over https only.
    assert llm.requests[0].url.host == "ollama.com"


def test_unsupported_claim_regenerates_once_then_flags(home):
    w, llm = writer([letter(), check("led a team of 10"), letter(), check("led a team of 10")])
    m = materials.prepare(JOB, A, w)
    assert llm.chat_calls == 4 and m.claims == ["led a team of 10"]
    retry_prompt = json.loads(llm.requests[2].content)["messages"][0]["content"]
    assert "Leave them out: led a team of 10" in retry_prompt
    assert json.loads((m.dir / "meta.json").read_text())["unsupported_claims"]


def test_claim_fixed_on_retry_is_not_flagged(home):
    w, _ = writer([letter(), check("won an award"), letter(), check()])
    assert materials.prepare(JOB, A, w).claims == []


def test_too_short_letter_is_regenerated(home):
    w, llm = writer([letter(40), check(), letter(), check()])
    materials.prepare(JOB, A, w)
    assert "had 40 words" in json.loads(llm.requests[2].content)["messages"][0]["content"]


def test_llm_never_sees_contact_pay_or_eeo(home):
    w, llm = writer([letter(), check()])
    materials.prepare(JOB, A, w)
    prompt = json.loads(llm.requests[0].content)["messages"][0]["content"]
    assert "document-extraction pipeline" in prompt  # facts are used
    for secret in ("+91 90000 00000", "alex@example.com", "13.5", "Male"):
        assert secret not in prompt


def test_materials_are_reused_and_drafts_cached(home):
    w, llm = writer([letter(), check(),
                     json.dumps({"answer": "Because of X.", "confidence": "medium"}),
                     json.dumps({"answer": "", "confidence": "high"})])  # fmt: skip
    m = materials.prepare(JOB, A, w, questions=[("Why us?", "textarea"), ("Blog URL", "text")])
    assert m.drafts["Why us?"]["answer"] == "Because of X."
    assert m.drafts["Blog URL"] == {
        "answer": "",
        "confidence": "low",
        "model": "ollama/gpt-oss:20b",
    }
    calls = llm.chat_calls
    again = materials.prepare(JOB, A, w, questions=[("Why us?", "textarea")])
    assert llm.chat_calls == calls and again.letter == m.letter  # nothing regenerated


def test_cloud_down_falls_back_to_local(home):
    class CloudDown(FakeLLM):
        def __call__(self, request):
            if request.url.host == "ollama.com":
                return __import__("httpx").Response(401, json={"error": "unauthorized"})
            return super().__call__(request)

    llm = CloudDown([letter(), check()])
    m = materials.prepare(JOB, A, materials.Writer(A, config.load_profile(), llm.transport()))
    meta = json.loads((m.dir / "meta.json").read_text())
    assert meta["letter_model"] == "ollama/qwen3.5:4b"


def test_editing_the_letter_clears_flags_and_rerenders(home):
    w, _ = writer([letter(), check("led a team of 10"), letter(), check("led a team of 10")])
    m = materials.prepare(JOB, A, w)
    assert m.claims
    (m.dir / "cover_letter.pdf").write_bytes(b"old pdf")
    m.letter_md.write_text(m.letter.replace("word", "fixed", 1))  # you fix the claim
    again = materials.prepare(JOB, A, w)
    assert again.claims == [] and "fixed" in again.letter
    assert not (m.dir / "cover_letter.pdf").exists()  # re-rendered from your text when pw given
    assert json.loads((m.dir / "meta.json").read_text())["edited"] is True
    assert materials.prepare(JOB, A, w).claims == []  # stays reviewed
