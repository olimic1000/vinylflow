"""Which numbering scheme a settings.json gets, across the change of default.

``sequential_disc_per_lp`` became the default for new installs, so an install from before
it must stay on ``vinyl``: its next record would otherwise sort apart from its library.
"""

import json

import pytest

from config import DEFAULT_TRACK_NUMBERING, Config

SEQ = "sequential_disc_per_lp"
VINYL = "vinyl"


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACK_NUMBERING", raising=False)
    return tmp_path / "config" / "settings.json"


def load(settings_path):
    # The .env path doesn't exist: only settings.json and the environment count here.
    return Config(env_path=settings_path.parent / "no.env", settings_path=settings_path)


def write(settings_path, content):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(content if isinstance(content, str) else json.dumps(content))


def saved(settings_path):
    return json.loads(settings_path.read_text())


def test_new_install_gets_the_default():
    assert DEFAULT_TRACK_NUMBERING == SEQ


def test_no_settings_file_gets_the_default_and_writes_nothing(settings_path):
    assert load(settings_path).track_numbering == SEQ
    assert not settings_path.exists()


def test_settings_from_before_the_default_changed_stay_on_vinyl(settings_path):
    write(settings_path, {"DISCOGS_USER_TOKEN": "x"})
    assert load(settings_path).track_numbering == VINYL
    assert saved(settings_path) == {"DISCOGS_USER_TOKEN": "x", "TRACK_NUMBERING": VINYL}


@pytest.mark.parametrize("save", [
    lambda config: config.save_token("x"),
    lambda config: config.save_output_dir("/music"),
])
def test_a_new_install_keeps_the_default_after_its_first_save(settings_path, save):
    """The first save creates settings.json; the next start must not take it for an old one."""
    save(load(settings_path))
    assert load(settings_path).track_numbering == SEQ


def test_choosing_vinyl_on_a_new_install_is_kept(settings_path):
    load(settings_path).save_track_numbering(VINYL)
    assert saved(settings_path) == {"TRACK_NUMBERING": VINYL}
    assert load(settings_path).track_numbering == VINYL


@pytest.mark.parametrize("scheme", [SEQ, VINYL])
def test_a_saved_choice_is_left_alone(settings_path, scheme):
    write(settings_path, {"DISCOGS_USER_TOKEN": "x", "TRACK_NUMBERING": scheme})
    before = settings_path.read_text()
    assert load(settings_path).track_numbering == scheme
    assert settings_path.read_text() == before


def test_the_environment_is_not_overruled_by_the_migration(settings_path, monkeypatch):
    """settings.json beats the environment, so writing vinyl there would hide this variable."""
    monkeypatch.setenv("TRACK_NUMBERING", SEQ)
    write(settings_path, {"DISCOGS_USER_TOKEN": "x"})
    assert load(settings_path).track_numbering == SEQ
    assert saved(settings_path) == {"DISCOGS_USER_TOKEN": "x"}


def test_a_corrupt_settings_file_is_not_overwritten_at_startup(settings_path):
    write(settings_path, '{"DISCOGS_USER_TOKEN": "x",')
    assert load(settings_path).track_numbering == SEQ
    assert settings_path.read_text() == '{"DISCOGS_USER_TOKEN": "x",'


def test_reload_keeps_the_migrated_scheme(settings_path):
    write(settings_path, {"DISCOGS_USER_TOKEN": "x"})
    config = load(settings_path)
    config.reload()
    assert config.track_numbering == VINYL


@pytest.mark.parametrize("source", ["settings", "environment"])
def test_an_unknown_scheme_warns_and_keeps_acting_as_vinyl(settings_path, monkeypatch, capsys, source):
    if source == "settings":
        write(settings_path, {"TRACK_NUMBERING": "Vinyl"})
    else:
        monkeypatch.setenv("TRACK_NUMBERING", "Vinyl")
    assert load(settings_path).track_numbering == VINYL
    assert "TRACK_NUMBERING" in capsys.readouterr().out


def test_an_empty_environment_variable_counts_as_unset(settings_path, monkeypatch):
    monkeypatch.setenv("TRACK_NUMBERING", "")
    assert load(settings_path).track_numbering == SEQ
