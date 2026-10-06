"""RepetitionGuard (July 14 session: 134 KB of "Je vais lancer le test."
x376 streamed for minutes until a human hit Stop)."""
from repetition_guard import RepetitionGuard


def test_repetition_guard_trips_on_a_degenerate_line_loop():
    g = RepetitionGuard()
    looped = "Je vais lancer le test.\nJe vais éditer le script.\n" * 30
    assert g.feed(looped) is True


def test_repetition_guard_lets_normal_prose_and_code_through():
    g = RepetitionGuard()
    text = "\n".join("ligne {} unique avec du contenu".format(i)
                     for i in range(200)) + "\n"
    assert g.feed(text) is False


def test_repetition_guard_needs_a_full_window_before_judging():
    # Short replies must never trip it, even fully repetitive.
    g = RepetitionGuard()
    assert g.feed("Let's go.\n" * 10) is False


def test_repetition_guard_caps_runaway_thinking():
    g = RepetitionGuard()
    unique = "\n".join("pensée {} différente à chaque fois".format(i)
                       for i in range(6000)) + "\n"
    assert len(unique) > RepetitionGuard.THINKING_CAP
    assert g.feed(unique, thinking=True) is True
