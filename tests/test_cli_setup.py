"""deployangel setup and token, ported from the Ruby gem's specs: redeem a
one-time code, save the tokens privately, and never print them."""

import io
import json
import os
import stat

from deployangel.cli import CLI, credentials
from deployangel.cli.client import NotFound


class Redeemer:
    """Answers a setup code like the server: the app and its two tokens, once."""

    def __init__(self):
        self.codes = []

    def redeem_setup_code(self, code):
        self.codes.append(code)
        if code != "da_setup_good":
            raise NotFound("That setup code is invalid, already used, or expired.")
        return {"app": {"id": 7, "name": "shop", "environment": "production"},
                "app_token": "da_live_apptoken", "api_token": "da_live_apitoken"}


def cli(root, *argv, client=None, env=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    command = CLI(list(argv), env=env or {}, stdout=stdout, stderr=stderr, client=client, root=str(root))
    return command, stdout, stderr


def test_redeems_a_code_and_saves_both_tokens_privately_without_printing_either(tmp_path):
    command, stdout, stderr = cli(tmp_path, "setup", "--code=da_setup_good", client=Redeemer())
    assert command.run() == 0

    file = tmp_path / ".deployangel" / "credentials.json"
    data = json.loads(file.read_text())
    assert data["app_token"] == "da_live_apptoken" and data["api_token"] == "da_live_apitoken"
    assert data["app"]["name"] == "shop"
    assert stat.S_IMODE(os.stat(file).st_mode) == 0o600
    assert (tmp_path / ".deployangel" / ".gitignore").read_text() == "*\n"
    assert "Set up DeployAngel for shop (production)" in stdout.getvalue()
    assert "not shown here" in stdout.getvalue()
    assert "da_live_" not in stdout.getvalue() + stderr.getvalue()


def test_uses_the_saved_token_unless_the_environment_names_one(tmp_path):
    cli(tmp_path, "setup", "--code=da_setup_good", client=Redeemer())[0].run()

    assert cli(tmp_path)[0].client().token == "da_live_apitoken"
    assert cli(tmp_path, env={"DEPLOYANGEL_API_TOKEN": "da_live_env"})[0].client().token == "da_live_env"
    nested = tmp_path / "app" / "models"
    nested.mkdir(parents=True)
    assert credentials.read(str(nested))["api_token"] == "da_live_apitoken"


def test_prints_a_saved_token_with_nothing_around_it_for_piping(tmp_path):
    cli(tmp_path, "setup", "--code=da_setup_good", client=Redeemer())[0].run()
    command, stdout, _ = cli(tmp_path, "token", "app")
    assert command.run() == 0
    assert stdout.getvalue() == "da_live_apptoken"


def test_says_whats_wrong_with_a_bad_code_a_missing_code_or_nothing_saved(tmp_path):
    command, _, stderr = cli(tmp_path, "setup", "--code=da_setup_used", client=Redeemer())
    assert command.run() == 4
    assert "invalid, already used, or expired" in stderr.getvalue()
    assert not (tmp_path / ".deployangel").exists()

    assert cli(tmp_path, "setup", client=Redeemer())[0].run() == 5
    command, _, stderr = cli(tmp_path, "token", "app")
    assert command.run() == 5
    assert "run deployangel setup --code=<code> first" in stderr.getvalue()
    assert cli(tmp_path, "token", "nope")[0].run() == 5
