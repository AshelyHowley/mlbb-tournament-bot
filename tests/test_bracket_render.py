from app.services.bracket import render_bracket


def row(i, stage="r8", title="1/8", a="A", b="B", winner=None):
    return {"id": i, "stage_code": stage, "stage_title": title, "team_a": a, "team_b": b,
            "status": "finished" if winner else "scheduled", "winner": winner}


def test_empty():
    assert "пока не созданы" in render_bracket([])[0]


def test_content_and_grouping():
    pages = render_bracket([row(1, winner="A"), row(2, "final", "Финал", "C", "D")])
    text = "".join(pages)
    assert "1/8" in text and "Финал" in text and "Победитель: <b>A</b>" in text and "Ожидается" in text
    assert text.index("1/8") < text.index("Финал")
    assert text.count("1/8") == 1


def test_html_is_escaped():
    text = "".join(render_bracket([row(1, a="<script>x</script>", b="&co")]))
    assert "<script>" not in text and "&lt;script&gt;" in text and "&amp;co" in text


def test_paginates_under_telegram_limit():
    rows = [row(i, a="A" * 40, b="B" * 40) for i in range(200)]
    pages = render_bracket(rows)
    assert len(pages) > 1 and all(len(p) < 4096 for p in pages)
