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

A photo is ALSO stored on the gateway, as the app's chatbot does, so the agent
can save it to a record by the `uploadId` the message names after the photos.

Runs host-side with no API key and no network: both uploads are faked.
"""

import asyncio
import io
import json

import httpx
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
    """Stand in for the Files API, and record what was sent to it.

    The app MCP is switched off here, because `web` loads `.env` at import and
    a configured host would store every test photo on the real gateway."""
    monkeypatch.setattr(web.appmcp_auth, "configured", lambda: False)
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


@pytest.fixture
def fake_gateway(monkeypatch, fake_upload):
    """A configured host whose gateway hands out one uploadId per photo."""
    stored = []

    async def _upload_chat_photo(http, blob, media_type, name):
        stored.append((name, media_type))
        return f"0000000{len(stored)}-0000-4000-8000-000000000000.jpg"

    monkeypatch.setattr(web.appmcp_auth, "configured", lambda: True)
    monkeypatch.setattr(web.appmcp_auth, "upload_chat_photo", _upload_chat_photo)
    return stored


def test_a_photo_is_stored_for_saving_and_named_after_the_pictures(fake_gateway):
    """The same line xas-ai-bot sends, after every attachment, so the agent can
    pass the id to `attach_chat_photo` / `add_damage_photo`. A document is not
    stored: the gateway takes images only."""
    blocks = _content(
        [_upload_file("front.jpg"), _upload_file("terms.pdf"), _upload_file("rear.png")]
    )
    assert [b["type"] for b in blocks] == ["image", "document", "image", "text"]
    assert blocks[-1]["text"] == (
        "Attached photos (use uploadId with attach_chat_photo to save): "
        "00000001-0000-4000-8000-000000000000.jpg, 00000002-0000-4000-8000-000000000000.jpg"
    )
    assert fake_gateway == [("front.jpg", "image/jpeg"), ("rear.png", "image/png")]


def test_the_photo_line_goes_between_the_pictures_and_the_words(monkeypatch, fake_gateway):
    events = []

    async def _send(session_id, events_):
        events.append(events_)

    monkeypatch.setattr(web, "_active", "sesn_test")
    monkeypatch.setattr(
        web.client.beta.sessions.events,
        "send",
        lambda session_id, events: _send(session_id, events),
    )
    asyncio.run(web.message(text="new scratch", files=[_upload_file("s.jpg")]))
    content = events[0][0]["content"]
    assert [b["type"] for b in content] == ["image", "text", "text"]
    assert content[1]["text"].startswith(web.PHOTO_HANDLES)
    assert content[2]["text"] == "new scratch"


def test_a_photo_the_gateway_refuses_fails_the_message_by_name(monkeypatch, fake_upload):
    """Sent without its id, the agent would be asked to save a photo it cannot."""

    async def _refuse(http, blob, media_type, name):
        raise RuntimeError("gateway refused the photo (400): Image is larger than 5 MB")

    monkeypatch.setattr(web.appmcp_auth, "configured", lambda: True)
    monkeypatch.setattr(web.appmcp_auth, "upload_chat_photo", _refuse)
    with pytest.raises(HTTPException) as refused:
        _content([_upload_file("big.jpg")])
    assert refused.value.status_code == 502
    assert "big.jpg" in refused.value.detail and "5 MB" in refused.value.detail
    assert not fake_upload


def test_without_the_app_mcp_a_photo_is_only_looked_at(fake_upload):
    blocks = _content([_upload_file("s.jpg")])
    assert [b["type"] for b in blocks] == ["image"]


def _gateway(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_the_gateway_upload_is_the_apps_call_made_with_the_bearers_user(monkeypatch):
    """Body as the app sends it, and the user token the stored bearer carries —
    a fresh login is `forceLogin` and would end the MCP's session."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"uploadId": "u.jpg", "name": "s.jpg"})

    monkeypatch.setattr(web.appmcp_auth, "_user_token", "tok")

    async def run():
        async with _gateway(handler) as http:
            return await web.appmcp_auth.upload_chat_photo(
                http, b"\xff\xd8\xff", "image/jpeg", "s.jpg"
            )

    assert asyncio.run(run()) == "u.jpg"
    (request,) = seen
    assert str(request.url) == web.appmcp_auth.GATEWAY_UPLOADS
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content) == {
        "data": "/9j/",
        "mimeType": "image/jpeg",
        "name": "s.jpg",
    }


def test_a_gateway_refusal_carries_the_gateways_own_reason(monkeypatch):
    monkeypatch.setattr(web.appmcp_auth, "_user_token", "tok")

    async def run():
        async with _gateway(
            lambda r: httpx.Response(400, json={"message": "Image is larger than 5 MB"})
        ) as http:
            await web.appmcp_auth.upload_chat_photo(http, b"x", "image/jpeg", "s.jpg")

    with pytest.raises(RuntimeError, match="400.*larger than 5 MB"):
        asyncio.run(run())


def test_no_upload_before_the_first_credential_is_minted(monkeypatch):
    monkeypatch.setattr(web.appmcp_auth, "_user_token", None)

    async def run():
        async with _gateway(lambda r: httpx.Response(201, json={"uploadId": "u.jpg"})) as http:
            await web.appmcp_auth.upload_chat_photo(http, b"x", "image/jpeg", "s.jpg")

    with pytest.raises(RuntimeError, match="never minted"):
        asyncio.run(run())


def test_the_scope_lets_the_agent_save_a_chat_photo():
    assert "attachments.write" in web.appmcp_auth.SCOPE.split()


def test_a_rotation_keeps_the_user_token_it_stored(monkeypatch):
    """Set only after the vault took the bearer, so the upload never runs as a
    user the MCP is not."""
    stored = []

    async def _login(http):
        return "fresh"

    class _Credentials:
        async def update(self, **kwargs):
            stored.append(kwargs)

    fake_client = type(
        "C",
        (),
        {"beta": type("B", (), {"vaults": type("V", (), {"credentials": _Credentials()})()})()},
    )()
    monkeypatch.setattr(web.appmcp_auth, "_fetch_user_token", _login)
    monkeypatch.setattr(web.appmcp_auth, "_user_token", None)
    monkeypatch.setenv("MCP_TOKEN_ENC_KEY", "k")
    monkeypatch.setenv("APPMCP_CREDENTIAL_ID", "cred")
    monkeypatch.setenv("APPMCP_VAULT_ID", "vlt")
    asyncio.run(web.appmcp_auth.rotate(fake_client, None))
    assert stored and web.appmcp_auth._user_token == "fresh"


def test_a_missing_licence_is_named_too(monkeypatch):
    """Every /aibot route checks the tenant's ai_assistant licence first, and that
    refusal says why under `error`, not `message` (seen live 2026-10-01)."""
    monkeypatch.setattr(web.appmcp_auth, "_user_token", "tok")
    body = {"error": "License required for feature: ai_assistant"}

    async def run():
        async with _gateway(lambda r: httpx.Response(403, json=body)) as http:
            await web.appmcp_auth.upload_chat_photo(http, b"x", "image/jpeg", "s.jpg")

    with pytest.raises(RuntimeError, match="403.*ai_assistant"):
        asyncio.run(run())
