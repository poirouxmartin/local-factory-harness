"""One session = one JSON file. The store owns titles and ordering."""
import pytest

from chat_store import ChatNotFound, ChatStore


@pytest.fixture
def store(tmp_path):
    return ChatStore(tmp_path / "chats")


def test_create_returns_a_fresh_session(store):
    s = store.create("qwen3-coder:30b", now=100.0)

    assert s["session_id"].startswith("c_")
    assert s["model"] == "qwen3-coder:30b"
    assert s["title"] == ""
    assert s["messages"] == []
    assert s["created_at"] == 100.0
    assert s["updated_at"] == 100.0


def test_lessons_start_empty_and_survive_a_reload(store, tmp_path):
    assert store.load_lessons() == []
    store.record_lessons([{"pattern": "tool_errors", "count": 3,
                           "suggestion": "read the error", "last_seen": 1.0}])
    assert ChatStore(tmp_path / "chats").load_lessons()[0]["count"] == 3


def test_recording_a_known_pattern_bumps_it_instead_of_piling_up(store):
    lesson = {"pattern": "tool_errors", "count": 2, "suggestion": "read it",
              "last_seen": 1.0}
    store.record_lessons([lesson])
    store.record_lessons([dict(lesson, count=4, last_seen=9.0)])

    kept = store.load_lessons()
    assert len(kept) == 1
    assert kept[0]["count"] == 6 and kept[0]["last_seen"] == 9.0


def test_a_corrupt_lessons_file_is_not_fatal(store):
    # The lessons file is a convenience, never a reason the studio won't boot.
    (store.root / "lessons.json").write_text("{ not json", encoding="utf-8")
    assert store.load_lessons() == []


def test_get_roundtrips_and_unknown_raises(store):
    s = store.create("m")

    assert store.get(s["session_id"])["session_id"] == s["session_id"]
    with pytest.raises(ChatNotFound):
        store.get("c_deadbeef")


def test_append_sets_title_from_first_user_message_only(store):
    sid = store.create("m")["session_id"]

    s = store.append(sid, {"role": "user", "content": "x" * 80, "ts": 1.0})
    assert s["title"] == "x" * 59 + "…"
    s = store.append(sid, {"role": "assistant", "content": "reply", "ts": 2.0})
    s = store.append(sid, {"role": "user", "content": "second", "ts": 3.0})

    assert s["title"] == "x" * 59 + "…"
    assert [m["role"] for m in s["messages"]] == ["user", "assistant", "user"]


def test_the_auto_title_reads_the_first_line_that_says_something(store):
    """Un prompt collé commence par une consigne, pas par une phrase : les 60
    premiers caractères bruts donnaient « # Tâche - [ ] lire le fic »."""
    sid = store.create("m")["session_id"]
    s = store.append(sid, {"role": "user", "ts": 1.0, "content":
                           "\n\n## Contexte\nrelire le parseur SSE"})

    assert s["title"] == "Contexte"


def test_the_auto_title_cuts_on_a_word_and_says_so(store):
    sid = store.create("m")["session_id"]
    s = store.append(sid, {"role": "user", "ts": 1.0, "content":
                           "corriger la fenetre de feedback chronologique du "
                           "banc a codeur avant de rejouer la serie"})

    assert s["title"] == "corriger la fenetre de feedback chronologique du banc a…"
    assert len(s["title"]) <= 60


def test_a_renamed_session_is_never_retitled_behind_the_operators_back(store):
    sid = store.create("m")["session_id"]
    store.set_title(sid, "  Banc  codeur  ")

    s = store.append(sid, {"role": "user", "content": "salut", "ts": 1.0})

    assert s["title"] == "Banc codeur"
    assert s["title_locked"] is True


def test_an_empty_rename_hands_the_name_back_to_the_machine(store):
    sid = store.create("m")["session_id"]
    store.append(sid, {"role": "user", "content": "relire le parseur", "ts": 1.0})
    store.set_title(sid, "à moi")

    s = store.set_title(sid, "")

    assert s["title"] == "relire le parseur"
    assert s["title_locked"] is False


def test_the_listing_carries_the_workspace_for_grouping(store):
    sid = store.create("m", kind="agent", workspace="C:/ws/lucena")["session_id"]

    assert store.list()[0]["workspace"] == "C:/ws/lucena"
    assert store.create("m")["session_id"] != sid  # a plain chat has none
    assert store.list()[0]["workspace"] is None


def test_list_summarizes_newest_first(store):
    a = store.create("m", now=1.0)["session_id"]
    b = store.create("m", now=2.0)["session_id"]
    store.append(a, {"role": "user", "content": "hello", "ts": 3.0}, now=3.0)

    out = store.list()

    assert [s["session_id"] for s in out] == [a, b]
    assert out[0]["title"] == "hello"
    assert out[0]["messages"] == 1
    assert "content" not in str(out[1])  # summaries carry no message bodies


def test_set_model_and_delete(store):
    sid = store.create("old")["session_id"]

    assert store.set_model(sid, "new")["model"] == "new"

    store.delete(sid)
    with pytest.raises(ChatNotFound):
        store.get(sid)
    with pytest.raises(ChatNotFound):
        store.delete(sid)


def test_create_agent_session_carries_kind_mode_workspace(store):
    s = store.create("m:1", kind="agent", mode="approve", workspace="C:/ws")
    got = store.get(s["session_id"])
    assert got["kind"] == "agent" and got["mode"] == "approve"
    assert got["workspace"] == "C:/ws" and got["pending_calls"] == []
    assert store.list()[0]["kind"] == "agent"


def test_create_agent_session_defaults_and_carries_toolset(store):
    s = store.create("m:1", kind="agent", mode="approve", workspace=".")
    assert store.get(s["session_id"])["toolset"] == "base"

    s = store.create("m:1", kind="agent", mode="approve", workspace=".",
                     toolset="dev")
    assert store.get(s["session_id"])["toolset"] == "dev"


def test_plain_session_has_no_agent_fields(store):
    s = store.create("m:1")
    got = store.get(s["session_id"])
    assert "mode" not in got and "workspace" not in got
    assert store.list()[0]["kind"] is None


def test_set_mode_and_pending_calls(store):
    sid = store.create("m:1", kind="agent", mode="approve", workspace=".")["session_id"]
    store.set_mode(sid, "auto")
    call = {"name": "write_file", "arguments": {"path": "a", "content": "b"}}
    store.set_pending_calls(sid, [call])
    got = store.get(sid)
    assert got["mode"] == "auto" and got["pending_calls"] == [call]


def test_set_summary_and_calibration_persist(store):
    sid = store.create("m:1")["session_id"]
    summary = {"content": "resume", "covers_until": 4, "model": "m:1", "ts": 1.0}
    store.set_summary(sid, summary)
    store.set_calibration(sid, {"chars": 3000, "tokens": 900})
    s = store.get(sid)
    assert s["summary"] == summary
    assert s["calibration"] == {"chars": 3000, "tokens": 900}


def test_mark_stalled_flags_the_last_assistant_message(tmp_path):
    store = ChatStore(tmp_path)
    sid = store.create("m:1", kind="agent", mode="auto")["session_id"]
    store.append(sid, {"role": "assistant", "content": "premier"})
    store.append(sid, {"role": "user", "content": "continue"})
    store.append(sid, {"role": "assistant", "content": "je n'ai pas les outils"})

    store.mark_stalled(sid)

    messages = store.get(sid)["messages"]
    assert messages[2].get("stalled") is True
    assert not messages[0].get("stalled")      # only the last one
    assert messages[2]["content"] == "je n'ai pas les outils"   # text kept


def test_mark_stalled_is_harmless_without_an_assistant_message(tmp_path):
    store = ChatStore(tmp_path)
    sid = store.create("m:1", kind="agent", mode="auto")["session_id"]
    store.append(sid, {"role": "user", "content": "salut"})

    store.mark_stalled(sid)

    assert not any(m.get("stalled") for m in store.get(sid)["messages"])
