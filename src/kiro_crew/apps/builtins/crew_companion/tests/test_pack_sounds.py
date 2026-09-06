"""A pack's optional per-state sound cues.

A pack is third-party content, possibly hand-edited, so the rule these tests
hold is the one the rest of the store already lives by: **a bad entry costs that
entry, never the pack.** A sound naming a traversal, a sound too big to play, a
"sound" that is really a PNG -- each is dropped with a warning and the pack's art
still loads.

The second property is that presence and content cannot disagree. The detail
payload reports which states HAVE a cue so the client never has to probe, and the
byte route serves them; both read one function, so a state the payload advertises
is a state the route can answer.
"""

from __future__ import annotations

import base64
import json

import pytest

from kiro_crew.apps.builtins.crew_companion import appearances as ap
from kiro_crew.apps.builtins.crew_companion.pack_transfer import (
    export_bundle,
    import_bundle,
)

#: A structurally recognisable WAV: the sniffer reads the RIFF/WAVE header, and
#: nothing here needs it to be playable.
_WAV = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00frames"
_OGG = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _store(tmp_path) -> ap.AppearanceStore:
    store = ap.AppearanceStore(tmp_path)
    store.load()
    return store


def _write_pack(tmp_path, ident="cue-pack", *, sounds=None, files=None):
    """A minimal pack on disk: one idle frame plus whatever sounds are asked for."""
    pack = tmp_path / ap.PACKS_DIRNAME / ident
    pack.mkdir(parents=True)
    manifest = {
        "meta": {"id": ident, "name": "Cue", "format": "svg"},
        "states": {"idle": "idle.svg"},
    }
    if sounds is not None:
        manifest["sounds"] = sounds
    (pack / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (pack / "idle.svg").write_text("<svg/>", "utf-8")
    for name, content in (files or {}).items():
        (pack / name).write_text(content, "utf-8")
    return pack


class TestReadingSounds:
    def test_a_valid_cue_is_served_with_its_sniffed_type(self, tmp_path):
        _write_pack(tmp_path, sounds={"done": "done.wav"}, files={"done.wav": _b64(_WAV)})
        store = _store(tmp_path)
        assert store.pack_sound("cue-pack", "done") == (_WAV, "audio/wav")

    @pytest.mark.parametrize(
        ("name", "raw", "mime"),
        [("a.mp3", _MP3, "audio/mpeg"), ("a.ogg", _OGG, "audio/ogg")],
    )
    def test_every_accepted_container_is_recognised(self, tmp_path, name, raw, mime):
        _write_pack(tmp_path, sounds={"working": name}, files={name: _b64(raw)})
        store = _store(tmp_path)
        assert store.pack_sound("cue-pack", "working") == (raw, mime)

    def test_the_type_comes_from_the_bytes_not_the_filename(self, tmp_path):
        """A manifest is hand-editable, so a name is not evidence.

        Serving a PNG as ``audio/mpeg`` because the file was called ``.mp3``
        would hand the browser a content type its bytes contradict, on content a
        third party authored.
        """
        _write_pack(tmp_path, sounds={"done": "done.mp3"}, files={"done.mp3": _b64(_PNG)})
        store = _store(tmp_path)
        assert store.pack_sound("cue-pack", "done") is None
        assert store.pack_sounds("cue-pack") == {}

    def test_an_oversize_cue_is_dropped_not_truncated(self, tmp_path):
        big = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (ap.MAX_SOUND_BYTES + 1)
        _write_pack(tmp_path, sounds={"error": "e.wav"}, files={"e.wav": _b64(big)})
        store = _store(tmp_path)
        assert store.pack_sounds("cue-pack") == {}
        assert store.pack_sound("cue-pack", "error") is None

    def test_a_cue_exactly_at_the_ceiling_is_kept(self, tmp_path):
        raw = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (ap.MAX_SOUND_BYTES - 12)
        assert len(raw) == ap.MAX_SOUND_BYTES
        _write_pack(tmp_path, sounds={"error": "e.wav"}, files={"e.wav": _b64(raw)})
        store = _store(tmp_path)
        assert store.pack_sound("cue-pack", "error") == (raw, "audio/wav")

    @pytest.mark.parametrize(
        "filename",
        ["../../escape.wav", "sub/dir.wav", ".hidden.wav", "cue.exe", "cue.svg", 7, None],
    )
    def test_an_unusable_filename_is_dropped(self, tmp_path, filename):
        _write_pack(tmp_path, sounds={"done": filename}, files={"cue.svg": _b64(_WAV)})
        store = _store(tmp_path)
        assert store.pack_sounds("cue-pack") == {}

    def test_a_named_but_absent_file_is_dropped(self, tmp_path):
        _write_pack(tmp_path, sounds={"done": "missing.wav"})
        assert _store(tmp_path).pack_sounds("cue-pack") == {}

    def test_content_that_is_not_base64_is_dropped(self, tmp_path):
        _write_pack(tmp_path, sounds={"done": "d.wav"}, files={"d.wav": "not base64 !!"})
        assert _store(tmp_path).pack_sounds("cue-pack") == {}

    def test_one_bad_cue_does_not_cost_the_good_one(self, tmp_path):
        _write_pack(
            tmp_path,
            sounds={"done": "d.wav", "error": "../evil.wav"},
            files={"d.wav": _b64(_WAV)},
        )
        assert _store(tmp_path).pack_sounds("cue-pack") == {"done": True}

    def test_a_pack_with_no_sounds_section_reads_as_none(self, tmp_path):
        _write_pack(tmp_path)
        assert _store(tmp_path).pack_sounds("cue-pack") == {}

    @pytest.mark.parametrize("junk", ["cue.wav", ["cue.wav"], 7])
    def test_a_junk_sounds_section_is_not_fatal(self, tmp_path, junk):
        pack = tmp_path / ap.PACKS_DIRNAME / "cue-pack"
        pack.mkdir(parents=True)
        (pack / "manifest.json").write_text(
            json.dumps(
                {
                    "meta": {"id": "cue-pack", "name": "Cue"},
                    "states": {"idle": "idle.svg"},
                    "sounds": junk,
                }
            ),
            "utf-8",
        )
        (pack / "idle.svg").write_text("<svg/>", "utf-8")
        store = _store(tmp_path)
        assert store.pack_sounds("cue-pack") == {}
        # The point of "not fatal": the art still loads.
        assert "idle" in (store.pack_detail("cue-pack") or {}).get("animations", {})

    def test_a_state_outside_the_vocabulary_is_ignored(self, tmp_path):
        """Only the three agent lifecycle states are addressable.

        An unknown key cannot grow the set a client has to probe, and there is no
        renderer state for it to fire on.
        """
        _write_pack(tmp_path, sounds={"sleepy": "s.wav"}, files={"s.wav": _b64(_WAV)})
        assert _store(tmp_path).pack_sounds("cue-pack") == {}

    def test_the_builtin_has_no_sounds(self, tmp_path):
        """It ships inside the frontend bundle, so it has no pack directory."""
        store = _store(tmp_path)
        assert store.pack_sounds(ap.DEFAULT_PACK) == {}
        assert store.pack_sound(ap.DEFAULT_PACK, "done") is None

    @pytest.mark.parametrize("state", [None, 7, ["done"], "", "nope"])
    def test_a_junk_state_is_answered_with_nothing(self, tmp_path, state):
        _write_pack(tmp_path, sounds={"done": "d.wav"}, files={"d.wav": _b64(_WAV)})
        assert _store(tmp_path).pack_sound("cue-pack", state) is None


class TestDetailAdvertisesSounds:
    def test_detail_reports_presence_so_the_client_need_not_probe(self, tmp_path):
        _write_pack(
            tmp_path,
            sounds={"done": "d.wav", "working": "w.mp3"},
            files={"d.wav": _b64(_WAV), "w.mp3": _b64(_MP3)},
        )
        detail = _store(tmp_path).pack_detail("cue-pack")
        assert detail is not None
        assert detail["sounds"] == {"working": True, "done": True}

    def test_detail_does_not_inline_the_audio(self, tmp_path):
        """The roster fetches this payload to draw a face.

        Inlining hundreds of KB of base64 audio into it would make every roster
        render pay for cues it may never play.
        """
        _write_pack(tmp_path, sounds={"done": "d.wav"}, files={"d.wav": _b64(_WAV)})
        detail = _store(tmp_path).pack_detail("cue-pack")
        assert detail is not None
        assert _b64(_WAV) not in json.dumps(detail)

    def test_a_pack_without_cues_still_carries_the_key(self, tmp_path):
        _write_pack(tmp_path)
        detail = _store(tmp_path).pack_detail("cue-pack")
        assert detail is not None
        assert detail["sounds"] == {}

    def test_the_builtin_carries_the_key_too(self, tmp_path):
        detail = _store(tmp_path).pack_detail(ap.DEFAULT_PACK)
        assert detail is not None
        assert detail["sounds"] == {}


class TestBundleCarriesSounds:
    def test_export_then_import_keeps_the_cues(self, tmp_path):
        """Export reads the store, not the detail payload.

        The payload reports presence only, so a bundle built from it would carry
        no audio and an export/delete/import round trip would destroy every cue
        -- the same loss the category and sprite-sheet omissions each caused.
        """
        _write_pack(tmp_path, sounds={"done": "d.wav"}, files={"d.wav": _b64(_WAV)})
        store = _store(tmp_path)
        bundle = export_bundle(store, "cue-pack")
        assert bundle is not None
        assert bundle["manifest"]["sounds"] == {"done": "d.wav"}
        assert bundle["files"]["d.wav"] == _b64(_WAV)

        assert store.delete_pack("cue-pack") is True
        assert import_bundle(store, bundle) == {"ok": True, "id": "cue-pack"}
        assert store.pack_sound("cue-pack", "done") == (_WAV, "audio/wav")

    def test_import_refuses_an_oversize_cue_rather_than_dropping_it(self, tmp_path):
        """Refused where the user can see it.

        The reader would skip an unusable cue with a warning nobody reads, so the
        import would "succeed" and the sound would simply never play.
        """
        big = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (ap.MAX_SOUND_BYTES + 1)
        bundle = {
            "kind": "crew-companion-pack",
            "version": 1,
            "id": "loud",
            "manifest": {
                "meta": {"id": "loud", "name": "Loud"},
                "states": {"idle": "idle.svg"},
                "sounds": {"done": "d.wav"},
            },
            "files": {"idle.svg": "<svg/>", "d.wav": _b64(big)},
        }
        result = import_bundle(_store(tmp_path), bundle)
        assert result["ok"] is False
        assert "d.wav" in result["error"]

    def test_import_refuses_a_cue_that_is_not_audio(self, tmp_path):
        bundle = {
            "kind": "crew-companion-pack",
            "version": 1,
            "id": "fake",
            "manifest": {
                "meta": {"id": "fake", "name": "Fake"},
                "states": {"idle": "idle.svg"},
                "sounds": {"done": "d.wav"},
            },
            "files": {"idle.svg": "<svg/>", "d.wav": "not base64 !!"},
        }
        assert import_bundle(_store(tmp_path), bundle)["ok"] is False

    def test_a_sprite_filename_still_refuses_an_audio_suffix(self, tmp_path):
        """The bundle allowlist grew; the sprite-sheet allowlist did not.

        ``save_sprite_pack`` names an IMAGE, so accepting ``sheet.wav`` there
        would store audio and then hand it to the sprite renderer.
        """
        # circular import: this test module imports pack_transfer at module scope
        # for the bundle helpers; the sprite entry point is pulled in here only to
        # keep this one assertion self-contained.
        from kiro_crew.apps.builtins.crew_companion.pack_transfer import save_sprite_pack

        result = save_sprite_pack(
            _store(tmp_path), "spr", {"meta": {"id": "spr"}}, _b64(_PNG), "sheet.wav"
        )
        assert result["ok"] is False


class TestImportGateMatchesTheReader:
    """The import gate has to be the SAME predicate the reader applies.

    Checking base64 and the size ceiling but not the container let valid base64
    of a PNG (or of plain text) import "successfully" and then get dropped by the
    reader's sniff — the exact silent never-plays outcome the gate exists to
    prevent, one step later.
    """

    @staticmethod
    def _bundle(content):
        return {
            "kind": "crew-companion-pack",
            "version": 1,
            "id": "sneaky",
            "manifest": {
                "meta": {"id": "sneaky", "name": "Sneaky"},
                "states": {"idle": "idle.svg"},
                "sounds": {"done": "d.wav"},
            },
            "files": {"idle.svg": "<svg/>", "d.wav": content},
        }

    @pytest.mark.parametrize("raw", [_PNG, b"just some text, base64-clean"])
    def test_valid_base64_that_is_not_audio_is_refused(self, tmp_path, raw):
        result = import_bundle(_store(tmp_path), self._bundle(_b64(raw)))
        assert result["ok"] is False
        assert "not audio" in result["error"]
        assert not _store(tmp_path).pack_exists("sneaky")

    def test_real_audio_still_imports(self, tmp_path):
        store = _store(tmp_path)
        assert import_bundle(store, self._bundle(_b64(_WAV)))["ok"] is True
        assert store.pack_sound("sneaky", "done") == (_WAV, "audio/wav")


class TestABundleMustCarryRealArt:
    """A pack IS its art; the cues are an optional extra on top.

    Once the bundle allowlist grew to accept audio, a non-empty file map stopped
    being the same question as "this bundle has art in it" — so a sound-only
    bundle satisfied the old check and installed a pack with nothing to draw.
    """

    @staticmethod
    def _bundle(files, sounds=None, states=None):
        manifest = {
            "meta": {"id": "quiet", "name": "Quiet"},
            "states": states if states is not None else {},
        }
        if sounds is not None:
            manifest["sounds"] = sounds
        return {
            "kind": "crew-companion-pack",
            "version": 1,
            "id": "quiet",
            "manifest": manifest,
            "files": files,
        }

    def test_a_sound_only_bundle_is_refused(self, tmp_path):
        result = import_bundle(
            _store(tmp_path),
            self._bundle({"d.wav": _b64(_WAV)}, sounds={"done": "d.wav"}),
        )
        assert result["ok"] is False
        assert "no art" in result["error"]
        assert not _store(tmp_path).pack_exists("quiet")

    def test_art_plus_a_cue_still_imports(self, tmp_path):
        store = _store(tmp_path)
        result = import_bundle(
            store,
            self._bundle(
                {"idle.svg": "<svg/>", "d.wav": _b64(_WAV)},
                sounds={"done": "d.wav"},
                states={"idle": "idle.svg"},
            ),
        )
        assert result["ok"] is True, result
        assert store.pack_sound("quiet", "done") == (_WAV, "audio/wav")

    def test_a_manifest_naming_a_cue_it_does_not_carry_is_refused(self, tmp_path):
        """A dangling reference is dropped silently by the reader.

        So the import would report success and the cue would never play — the
        same silent outcome the per-file audio check prevents, from the other
        side.
        """
        result = import_bundle(
            _store(tmp_path),
            self._bundle(
                {"idle.svg": "<svg/>"},
                sounds={"done": "missing.wav"},
                states={"idle": "idle.svg"},
            ),
        )
        assert result["ok"] is False
        assert "does not contain" in result["error"]

    def test_a_cue_for_a_state_no_renderer_has_is_dropped_not_fatal(self, tmp_path):
        """It names nothing the reader will look for either."""
        store = _store(tmp_path)
        result = import_bundle(
            store,
            self._bundle(
                {"idle.svg": "<svg/>"},
                sounds={"sleepy": "missing.wav"},
                states={"idle": "idle.svg"},
            ),
        )
        assert result["ok"] is True, result
        assert store.pack_sounds("quiet") == {}

    def test_a_bundle_with_no_files_at_all_is_still_refused(self, tmp_path):
        assert import_bundle(_store(tmp_path), self._bundle({}))["ok"] is False


class TestACueReferenceCanOnlyNameAFileThatPassedTheSniff:
    """The two import checks compose: per-file sniff, then per-reference presence.

    A manifest cue naming a file whose bytes are NOT audio is refused by the
    per-file sniff before the reference check ever runs, so a reference cannot
    point at non-audio content by construction.
    """

    def test_a_cue_pointing_at_a_png_named_wav_is_refused(self, tmp_path):
        bundle = {
            "kind": "crew-companion-pack",
            "version": 1,
            "id": "sneaky",
            "manifest": {
                "meta": {"id": "sneaky", "name": "Sneaky"},
                "states": {"idle": "idle.svg"},
                "sounds": {"done": "d.wav"},
            },
            "files": {"idle.svg": "<svg/>", "d.wav": _b64(_PNG)},
        }
        result = import_bundle(_store(tmp_path), bundle)
        assert result["ok"] is False
        assert "not audio" in result["error"]


class TestACueMustPointAtAnAudioFile:
    """A manifest cue naming a real ART file is refused, not silently muted."""

    def test_a_cue_naming_the_idle_svg_is_refused(self, tmp_path):
        bundle = {
            "kind": "crew-companion-pack",
            "version": 1,
            "id": "muted",
            "manifest": {
                "meta": {"id": "muted", "name": "Muted"},
                "states": {"idle": "idle.svg"},
                "sounds": {"done": "idle.svg"},
            },
            "files": {"idle.svg": "<svg/>"},
        }
        result = import_bundle(_store(tmp_path), bundle)
        assert result["ok"] is False
        assert "does not contain" in result["error"]
        assert not _store(tmp_path).pack_exists("muted")
