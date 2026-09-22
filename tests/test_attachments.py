"""What a file dragged into the chat window becomes.

The drop is an ATTACHMENT ON THE MESSAGE, not a mount: the file is uploaded and
referenced from the `user.message` event, so the model SEES the picture in the
turn it answers. Nothing dragged in reaches the sandbox — the sandbox's one
input is the mounted pull, and a file the agent could read with `bash` would be
a second source of facts about the same book.

That leaves two shapes a message can carry, `image` and `document`, and the
tests here pin the three things that decide what a planner gets: which files are
carried at all, that a refusal names the file rather than dropping it quietly,
and that the picture goes in FRONT of the words that describe it.

Runs host-side with no API key and no network: the upload is faked.
"""

import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile

import web


def _upload_file(name: str, blob: bytes = b"x") -> UploadFile:
    return UploadFile(file=io.BytesIO(blob), filename=name)


class _Uploaded:
    def __init__(self, file_id: str) -> None:
        self.id = file_id


@pytest.fixture
def fake_upload(monkeypatch):
    """Stand in for the Files API, and record what was sent to it."""
    sent = []

    async def _upload(filename, blob, media_type):
        sent.append((filename, media_type, blob))
        return _Uploaded(f"file_{len(sent)}")

    monkeypatch.setattr(web, "_upload", _upload)
    return sent


def _content(files):
    return asyncio.run(web._attachment_content(files))


def test_a_photo_travels_as_an_image_the_model_can_look_at(fake_upload):
    blocks = _content([_upload_file("scratch.JPG")])
    assert blocks == [{"type": "image", "source": {"type": "file", "file_id": "file_1"}}]
    assert fake_upload[0][:2] == ("scratch.JPG", "image/jpeg")


def test_a_pdf_keeps_its_type_and_other_text_is_uploaded_as_text():
    assert web.attachment_kind("terms.pdf") == ("application/pdf", "document")
    # A .csv the browser calls text/csv is still plain text, and the block's
    # source type has to match what was uploaded.
    assert web.attachment_kind("readings.csv") == ("text/plain", "document")
    assert web.attachment_kind("notes.md") == ("text/plain", "document")


def test_a_document_carries_its_filename_so_the_model_can_name_it(fake_upload):
    blocks = _content([_upload_file("readings.csv", b"km,fuel\n1500,half\n")])
    assert blocks == [
        {
            "type": "document",
            "source": {"type": "file", "file_id": "file_1"},
            "title": "readings.csv",
        }
    ]


def test_a_file_a_message_cannot_carry_is_refused_by_name(fake_upload):
    """Named, because the alternative is uploading it and saying nothing."""
    with pytest.raises(HTTPException) as refused:
        _content([_upload_file("contract.docx")])
    assert refused.value.status_code == 400
    assert "contract.docx" in refused.value.detail
    assert not fake_upload, "nothing is uploaded before it is known to be carryable"


def test_an_oversize_file_is_refused_before_it_is_uploaded(fake_upload):
    with pytest.raises(HTTPException) as refused:
        _content([_upload_file("huge.png", b"0" * (web.MAX_ATTACHMENT_BYTES + 1))])
    assert "huge.png" in refused.value.detail
    assert not fake_upload


def test_more_files_than_one_message_carries_is_refused(fake_upload):
    files = [_upload_file(f"p{i}.png") for i in range(web.MAX_ATTACHMENTS + 1)]
    with pytest.raises(HTTPException) as refused:
        _content(files)
    assert str(web.MAX_ATTACHMENTS) in refused.value.detail
    assert not fake_upload


def test_the_picture_goes_in_front_of_the_words(monkeypatch, fake_upload):
    """The text is ABOUT the photo, so the model reads a block it has seen."""
    events = []

    async def _send(session_id, events_):
        events.append((session_id, events_))

    monkeypatch.setattr(web, "_active", "sesn_test")
    monkeypatch.setattr(
        web.client.beta.sessions.events,
        "send",
        lambda session_id, events: _send(session_id, events),
    )
    asyncio.run(web.message(text="  scratch on the rear door  ", files=[_upload_file("s.png")]))

    ((session_id, sent),) = events
    assert session_id == "sesn_test"
    assert sent[0]["type"] == "user.message"
    assert [block["type"] for block in sent[0]["content"]] == ["image", "text"]
    assert sent[0]["content"][1]["text"] == "scratch on the rear door"


def test_an_empty_turn_is_not_a_turn(monkeypatch):
    monkeypatch.setattr(web, "_active", "sesn_test")
    with pytest.raises(HTTPException) as refused:
        asyncio.run(web.message(text="   ", files=[]))
    assert refused.value.status_code == 400


def test_a_replayed_attachment_is_named_while_this_process_remembers_it():
    """The event carries a file id and no name, and an uploaded input cannot be
    read back — so the name lives here or nowhere. After a restart the transcript
    says what kind of thing it was, which is the honest fallback."""

    class _Block:
        def __init__(self, kind, file_id):
            self.type = kind
            self.source = type("S", (), {"file_id": file_id})()

    web._attachment_names["file_known"] = "scratch.jpg"
    labels = web._attachment_labels(
        [_Block("image", "file_known"), _Block("document", "file_forgotten"), _Block("text", None)]
    )
    assert labels == ["scratch.jpg", "document"]
