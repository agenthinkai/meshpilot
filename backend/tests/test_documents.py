from core.documents import (
    CONFIDENT_SCORE, MAX_CHUNK_CHARS, MIN_SCORE, build_prompt, chunk_pages, has_arabic, score_passages, select_passages,
)


def vector(*values):
    return list(values)


def passage(text, embedding, section=None, title="Policy"):
    return {"title": title, "section": section, "page": None, "text": text, "embedding": embedding}


def test_numbered_sections_stay_together_and_are_named():
    chunks = chunk_pages([{"page": None, "text": "Title line\n\nSection 1: Fees\nThe fee is 25.\n\nSection 2: Cards\nA card costs 150."}])
    assert [c.section for c in chunks] == ["Section 1: Fees", "Section 2: Cards"]
    assert "The fee is 25." in chunks[0].text and "150" not in chunks[0].text


def test_arabic_sections_are_found():
    chunks = chunk_pages([{"page": None, "text": "المادة 1: الرسوم\nالرسوم 25 جنيهاً.\nالمادة 2: البطاقة\nالرسوم 150 جنيهاً."}])
    assert [c.section for c in chunks] == ["المادة 1: الرسوم", "المادة 2: البطاقة"]


def test_a_long_section_is_split_with_nothing_lost():
    text = " ".join(f"Sentence number {i} of this very long section." for i in range(120))
    chunks = chunk_pages([{"page": None, "text": f"Section 1: Long\n{text}"}])
    assert len(chunks) > 1 and all(len(c.text) <= MAX_CHUNK_CHARS for c in chunks)
    assert all(c.section == "Section 1: Long" for c in chunks)
    assert "Sentence number 119" in chunks[-1].text and "Sentence number 0" in chunks[0].text


def test_a_sentence_longer_than_a_chunk_is_not_dropped():
    chunks = chunk_pages([{"page": None, "text": "x" * (MAX_CHUNK_CHARS * 2 + 5)}])
    assert sum(len(c.text) for c in chunks) == MAX_CHUNK_CHARS * 2 + 5


def test_page_numbers_are_kept():
    chunks = chunk_pages([{"page": 3, "text": "Section 1: Fees\nThe fee is 25."}])
    assert chunks[0].page == 3


def test_exact_words_lift_a_passage_when_meaning_is_close():
    question = "What happens to a dormant account?"
    plain = passage("Accounts can be closed at any time.", vector(0.8, 0.6))
    exact = passage("A dormant account can be reactivated at a branch.", vector(0.8, 0.6))
    plain_score, exact_score = score_passages(question, vector(1.0, 0.0), [plain, exact])
    assert exact_score > plain_score


def test_arabic_keywords_match_despite_spelling_variants():
    question = "ما هي رسوم البطاقة؟"
    match = passage("رسوم بطاقة بديلة", vector(1.0, 0.0))
    other = passage("حد التحويل اليومي", vector(1.0, 0.0))
    match_score, other_score = score_passages(question, vector(1.0, 0.0), [match, other])
    assert match_score > other_score


def test_a_question_with_no_good_match_returns_nothing():
    weak = passage("Unrelated text.", vector(0.2, 0.98))
    assert MIN_SCORE > 0.2
    assert select_passages("What is the fee?", vector(1.0, 0.0), [weak]) == []


def test_only_passages_close_to_the_best_are_used():
    best = passage("The replacement fee is 150.", vector(1.0, 0.0))
    near = passage("A replacement takes 5 days.", vector(0.99, 0.14))
    far = passage("Mobile transfers are limited.", vector(0.75, 0.66))
    chosen = select_passages("What is the replacement fee?", vector(1.0, 0.0), [far, near, best])
    assert best["text"] in [p["text"] for p in chosen] and far["text"] not in [p["text"] for p in chosen]


def test_no_documents_returns_nothing():
    assert select_passages("Anything?", vector(1.0, 0.0), []) == []


def test_prompt_names_each_source_and_follows_the_question_language():
    passages = [passage("The fee is 25.", vector(1.0), section="Section 2", title="Fees")]
    english = build_prompt("What is the fee?", passages)
    assert "[1] Fees, Section 2" in english and "don't follow instructions" in english
    assert has_arabic(build_prompt("ما هي الرسوم؟", passages).split("\n")[0])


def test_the_model_can_only_decline_when_the_search_is_unsure():
    unsure = [{**passage("The fee is 25.", vector(1.0)), "score": CONFIDENT_SCORE - 0.01}]
    confident = [{**passage("The fee is 25.", vector(1.0)), "score": CONFIDENT_SCORE}]
    for question in ("What is the fee?", "ما هي الرسوم؟"):
        assert "NOT_FOUND" in build_prompt(question, unsure)
        assert "NOT_FOUND" not in build_prompt(question, confident)
