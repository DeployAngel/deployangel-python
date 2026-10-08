import os

from deployangel.config import Configuration
from deployangel.core import fingerprint, histogram, release, work
from deployangel.core.aggregator import MAX_CHECKPOINTS, OTHER, Aggregator
from deployangel.core.buffer import Buffer

ROOT = "/app"


# Histogram


def test_histogram_uses_the_clouds_buckets():
    # The same values as the Ruby agent's buckets, computed with Ruby.
    assert [histogram.bucket_for(v) for v in (0.5, 1, 1.05, 1.1, 2, 100, 250.5, 1e9)] == [0, 0, 1, 1, 8, 49, 58, 218]
    assert histogram.bucket_for(84) == 47
    assert histogram.bucket_for(10**30) == histogram.MAX_BUCKET


def test_histogram_serializes_sparse_counts_with_the_scheme():
    h = histogram.Histogram()
    for _ in range(3):
        h.record(84)
    h.record(1000)
    assert h.to_protocol() == {"scheme": "log1.1_ms_v1", "counts": {"47": 3, "73": 1}}


# Fingerprint

FRAMES = [
    ("/app/.heroku/python/lib/python3.12/site-packages/django/db/models/base.py", "save"),
    ("/app/shop/services/orders.py", "create_order"),
    ("/app/shop/views.py", "checkout"),
]


def test_fingerprint_uses_the_first_application_frame_without_line_numbers():
    assert fingerprint.top_frame(FRAMES, ROOT) == ("shop/services/orders.py#create_order", True)


def test_fingerprint_is_stable_when_packages_upgrade_or_messages_change():
    before = fingerprint.details("shop.errors.Missing", FRAMES, ROOT, message="id 12 missing")
    upgraded = [("/usr/lib/python3.13/site-packages/django/db/models/base.py", "save"), *FRAMES[1:]]
    after = fingerprint.details("shop.errors.Missing", upgraded, ROOT, message="id 99 missing")
    assert before["fingerprint"] == after["fingerprint"]


def test_fingerprint_differs_by_exception_class_and_application_frame():
    base = fingerprint.details("ValueError", FRAMES, ROOT)["fingerprint"]
    other_class = fingerprint.details("KeyError", FRAMES, ROOT)["fingerprint"]
    other_frame = fingerprint.details("ValueError", [("/app/shop/cart.py", "total")], ROOT)["fingerprint"]
    assert base not in (other_class, other_frame)


def test_fingerprint_falls_back_to_a_version_free_package_frame():
    frames = [("/app/.venv/lib/python3.12/site-packages/redis/client.py", "execute_command")]
    details = fingerprint.details("redis.exceptions.ConnectionError", frames, ROOT)
    assert details["top_frame"] == "redis/client.py#execute_command"
    assert details["app_frame"] is False


def test_fingerprint_does_not_treat_a_sibling_directory_as_the_app():
    assert not fingerprint.is_app_path("/application/shop/views.py", "/app")
    assert fingerprint.is_app_path("/app/shop/views.py", "/app/")


def test_fingerprint_names_exceptions_by_module_except_builtins():
    class PaymentError(Exception):
        pass

    assert fingerprint.exception_class(ValueError()) == "ValueError"
    assert fingerprint.exception_class(PaymentError()) == "test_core.test_fingerprint_names_exceptions_by_module_except_builtins.<locals>.PaymentError"


def test_fingerprint_reads_a_real_traceback_innermost_first():
    def inner():
        raise KeyError("sku-1")

    try:
        inner()
    except KeyError as error:
        frames = fingerprint.locations(error)
    assert frames[0][1] == "inner"
    assert frames[-1][1] == "test_fingerprint_reads_a_real_traceback_innermost_first"


def test_messages_lose_ids_emails_and_quoted_values():
    message = fingerprint.normalize_message("User 42 (pat@example.com) not found: 'abc' 0x7f3a")
    assert message == "User <n> (<email>) not found: <string> <hex>"


def test_messages_keep_unquoted_words():
    assert fingerprint.normalize_message("Payment of 1250.00 failed for Jane Doe") == "Payment of <n> failed for Jane Doe"


def test_messages_replace_redactions_as_whole_words_first():
    redactions = [("acme.lendwell.com", "<host>"), ("acme", "<tenant>")]
    assert fingerprint.normalize_message("Blocked host: ACME.lendwell.com for acme_x, not acmeco", redactions) == \
        "Blocked host: <host> for <tenant>_x, not acmeco"


def test_backtraces_keep_only_application_frames():
    assert fingerprint.app_backtrace(FRAMES, ROOT) == ["shop/services/orders.py#create_order", "shop/views.py#checkout"]


def test_a_formatted_traceback_fingerprints_like_the_exception():
    import traceback

    def charge():
        raise ValueError("card 4242 declined")

    try:
        try:
            charge()
        except ValueError as error:
            raise RuntimeError("payment failed") from error
    except RuntimeError as error:
        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        expected = fingerprint.for_exception(error, os.path.dirname(__file__))

    class_name, frames, message = fingerprint.parse_traceback(text)
    assert (class_name, message) == ("RuntimeError", "payment failed")
    assert fingerprint.details(class_name, frames, os.path.dirname(__file__), message=message) == expected


def test_parse_traceback_needs_a_traceback():
    assert fingerprint.parse_traceback("Work-horse terminated unexpectedly") is None


# Aggregator


def test_aggregator_accumulates_requests_into_the_current_minute(fake_clock):
    aggregator = Aggregator(max_routes=3, clock=fake_clock)
    aggregator.record("GET /products", 200, 84)
    aggregator.record("GET /products", 500, 120, unhandled=True)
    aggregator.record("GET /missing", 404, 5)
    fake_clock.advance(60)

    (period,) = aggregator.drain()
    assert period.started_at == 1790776800
    assert period.requests == 3
    assert period.status_counts == {"500": 1, "404": 1}
    assert period.unhandled_exceptions == 1
    assert period.routes["GET /products"].requests == 2


def test_aggregator_leaves_requests_out_of_totals_when_asked(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    aggregator.record("GET /products", 200, 84)
    aggregator.record("GET unmatched", 404, 2, in_totals=False)
    fake_clock.advance(60)

    (period,) = aggregator.drain()
    assert (period.requests, period.status_counts, period.histogram.count()) == (1, {}, 1)
    assert period.routes["GET unmatched"].status_counts == {"404": 1}


def test_aggregator_keeps_the_minute_in_progress_unless_asked(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    aggregator.record("GET /", 200, 1)
    assert aggregator.drain() == []
    assert aggregator.drain(include_current=True)[0].requests == 1


def test_aggregator_reports_idle_minutes_and_never_drains_one_twice(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    fake_clock.advance(180)
    assert [p.requests for p in aggregator.drain()] == [0, 0, 0]
    fake_clock.advance(60)
    assert [p.started_at for p in aggregator.drain()] == [1790776800 + 180]


def test_aggregator_caps_periods_after_a_long_pause(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    fake_clock.advance(3600)
    assert len(aggregator.drain(max_periods=5)) == 5


def test_aggregator_folds_routes_beyond_the_cap(fake_clock):
    aggregator = Aggregator(max_routes=3, clock=fake_clock)
    for route in "abcde":
        aggregator.record(f"GET /{route}", 200, 1)
    fake_clock.advance(60)
    routes = aggregator.drain()[0].routes
    assert list(routes) == ["GET /a", "GET /b", OTHER]
    assert routes[OTHER].requests == 3


def test_aggregator_counts_job_attempts_and_discards(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    aggregator.record_job("shop.tasks.sync", 50, queue_latency_ms=200)
    aggregator.record_job("shop.tasks.sync", 70, failed=True)
    aggregator.record_discard("shop.tasks.sync")
    fake_clock.advance(60)
    period = aggregator.drain()[0]
    assert (period.jobs.processed, period.jobs.failed, period.jobs.discarded) == (2, 1, 1)
    assert (period.jobs.duration.count(), period.jobs.queue_latency.count()) == (2, 1)
    assert period.job_classes["shop.tasks.sync"].processed == 2


def test_aggregator_keeps_a_backtrace_only_the_first_time(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    details = fingerprint.details("ValueError", FRAMES, ROOT)
    aggregator.record_exception(details, source="route:GET /", backtrace=["a.py#x"])
    fake_clock.advance(60)
    aggregator.record_exception(details, handled=True, backtrace=["a.py#x"])
    first, second = aggregator.drain(include_current=True)
    entry = first.exceptions[details["fingerprint"]]
    assert (entry["count"], entry["sources"], entry["backtrace"]) == (1, {"route:GET /": 1}, ["a.py#x"])
    assert second.exceptions[details["fingerprint"]]["handled_count"] == 1
    assert "backtrace" not in second.exceptions[details["fingerprint"]]



def test_aggregator_counts_checkpoints_by_where_they_were_recorded(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    aggregator.record_checkpoint("order.created", 2, work.HTTP)
    aggregator.record_checkpoint("order.created", 1, work.JOB)
    aggregator.record_checkpoint("order.created", 4)
    period = aggregator.drain(include_current=True)[0]
    assert period.checkpoints == {"order.created": [7, 2, 1]}


def test_aggregator_folds_checkpoints_beyond_the_cap_with_where_they_were_recorded(fake_clock):
    aggregator = Aggregator(clock=fake_clock)
    for i in range(MAX_CHECKPOINTS - 1):
        aggregator.record_checkpoint(f"c{i}", 1)
    aggregator.record_checkpoint("late.http", 3, work.HTTP)
    aggregator.record_checkpoint("late.job", 2, work.JOB)
    aggregator.record_checkpoint("late.neither", 1)
    period = aggregator.drain(include_current=True)[0]
    assert len(period.checkpoints) == MAX_CHECKPOINTS
    assert period.checkpoints[OTHER] == [6, 3, 2]


def test_units_of_work_nest_and_the_innermost_wins():
    assert work.current() is None
    request = work.begin(work.HTTP)
    assert work.current() == work.HTTP
    job = work.begin(work.JOB)
    assert work.current() == work.JOB
    work.end(job)
    assert work.current() == work.HTTP
    work.end(request)
    assert work.current() is None


def test_ending_a_unit_of_work_never_raises():
    token = work.begin(work.JOB)
    work.end(token)
    work.end(token)  # already used
    work.end(None)
    assert work.current() is None


def test_units_of_work_are_separate_per_thread():
    import threading

    seen = []
    token = work.begin(work.HTTP)
    try:
        thread = threading.Thread(target=lambda: seen.append(work.current()))
        thread.start()
        thread.join()
    finally:
        work.end(token)
    assert seen == [None]


# Buffer


def test_buffer_drops_the_oldest_when_full():
    buffer = Buffer(2)
    for item in (1, 2, 3):
        buffer.push(item)
    assert (buffer.shift(), buffer.shift(), buffer.dropped) == (2, 3, 1)


# Release


def resolve(env, **config):
    return release.resolve(Configuration(env={}) if not config else _config(**config), env=env, http=lambda uri: None)


def _config(**options):
    config = Configuration(env={})
    config.update(**options)
    return config


def test_release_prefers_configuration():
    assert resolve({"HEROKU_RELEASE_VERSION": "v9"}, revision="ABC1234").to_protocol() == \
        {"version": None, "commit": "abc1234", "source": "config"}


def test_release_reads_heroku_build_commit_before_the_slug_commit():
    r = resolve({"HEROKU_RELEASE_VERSION": "v42", "HEROKU_BUILD_COMMIT": "a" * 40, "HEROKU_SLUG_COMMIT": "b" * 40})
    assert (r.version, r.commit, r.source) == ("v42", "a" * 40, "heroku_dyno_metadata")


def test_release_reads_kamal_render_fly_and_railway():
    assert resolve({"KAMAL_VERSION": "abc1234_uncommitted_x"}).to_protocol() == \
        {"version": "abc1234_uncommitted_x", "commit": "abc1234", "source": "kamal"}
    assert resolve({"RENDER_GIT_COMMIT": "def5678"}).commit == "def5678"
    assert resolve({"FLY_IMAGE_REF": "registry.fly.io/shop:deployment-01H9"}).version == "01H9"
    assert resolve({"RAILWAY_DEPLOYMENT_ID": "dep_1"}).to_protocol() == {"version": "dep_1", "commit": None, "source": "railway"}


def test_release_drops_a_malformed_commit():
    assert resolve({"RENDER_GIT_COMMIT": "not-a-sha"}).unknown


def test_release_reads_a_revision_file(tmp_path):
    (tmp_path / "REVISION").write_text("abcdef1\n")
    assert release.resolve(Configuration(env={}), env={}, root=str(tmp_path)).to_protocol() == \
        {"version": None, "commit": "abcdef1", "source": "revision_file"}


def test_release_reads_ecs_image_tags_and_digests():
    assert release.ecs('{"Image": "123.dkr.ecr/shop:abc1234"}').commit == "abc1234"
    assert release.ecs('{"Image": "shop:v1.4.2"}').version == "v1.4.2"
    assert release.ecs('{"Image": "shop:latest", "ImageID": "sha256:0123456789abcdef"}').version == "sha256:0123456789ab"
    assert release.ecs("not json") is None


def test_release_is_unknown_without_any_source():
    resolved = resolve({})
    assert resolved.to_protocol() == {"version": None, "commit": None, "source": "unknown"}
    # The code fingerprint is tried later, off the boot path.
    assert resolved.pending


def test_a_blank_build_arg_revision_counts_as_unset(tmp_path):
    # `docker build` without --build-arg GIT_SHA leaves DEPLOYANGEL_REVISION="".
    (tmp_path / "REVISION").write_text("abcdef1\n")
    config = Configuration(env={"DEPLOYANGEL_REVISION": "", "DEPLOYANGEL_RELEASE_VERSION": " "})
    assert release.resolve(config, env={}, root=str(tmp_path)).source == "revision_file"


SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def git_checkout(path, head="ref: refs/heads/main\n", refs=None, packed=None):
    git = path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head)
    for name, sha in (refs or {}).items():
        (git / name).parent.mkdir(parents=True, exist_ok=True)
        (git / name).write_text(sha + "\n")
    if packed is not None:
        (git / "packed-refs").write_text(packed)
    return git


def test_release_reads_a_detached_git_head(tmp_path):
    git_checkout(tmp_path, head=SHA.upper() + "\n")
    assert release.resolve(Configuration(env={}), env={}, root=str(tmp_path)).to_protocol() == \
        {"version": None, "commit": SHA, "source": "git_head"}


def test_git_head_follows_a_branch_to_its_ref_file_or_packed_refs(tmp_path):
    loose, packed = tmp_path / "loose", tmp_path / "packed"
    git_checkout(loose, refs={"refs/heads/main": SHA})
    git_checkout(packed, packed=f"# pack-refs with: peeled fully-peeled sorted\n{OTHER_SHA} refs/heads/feature\n"
                                f"^{SHA}\n{SHA} refs/heads/main\n")
    assert release.git_head(str(loose)) == SHA
    assert release.git_head(str(packed)) == SHA


def test_git_head_follows_a_gitdir_file_and_commondir(tmp_path):
    # A worktree: .git is a file naming its own directory inside the main
    # repository, whose branches are in the common directory.
    main = git_checkout(tmp_path / "main", refs={"refs/heads/feature": SHA})
    worktree_git = main / "worktrees" / "feature"
    worktree_git.mkdir(parents=True)
    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (worktree_git / "commondir").write_text("../..\n")
    app = tmp_path / "feature"
    app.mkdir()
    (app / ".git").write_text("gitdir: ../main/.git/worktrees/feature\n")
    assert release.git_head(str(app)) == SHA

    # An absolute gitdir, with branches only in packed-refs.
    (main / "refs" / "heads" / "feature").unlink()
    (main / "packed-refs").write_text(f"{SHA} refs/heads/feature\n")
    (app / ".git").write_text(f"gitdir: {worktree_git}\n")
    assert release.git_head(str(app)) == SHA


def test_git_head_looks_up_to_three_parent_directories(tmp_path):
    git_checkout(tmp_path, refs={"refs/heads/main": SHA})
    near = tmp_path / "a" / "b" / "c"
    too_far = near / "d"
    too_far.mkdir(parents=True)
    assert release.git_head(str(near)) == SHA
    assert release.git_head(str(too_far)) is None


def test_git_head_ignores_unsafe_and_malformed_refs(tmp_path):
    # A commit-shaped file outside the repository is never read.
    (tmp_path / "escape").mkdir()
    (tmp_path / "escape" / "secret").write_text(SHA)
    for name, head in (("escape", "ref: refs/../../secret\n"), ("outside", "ref: HEAD\n"),
                       ("missing", "ref: refs/heads/gone\n"), ("garbage", "not a commit\n")):
        git_checkout(tmp_path / name, head=head)
        assert release.git_head(str(tmp_path / name)) in (None, "not a commit")
        resolved = release.resolve(Configuration(env={}), env={}, root=str(tmp_path / name))
        assert (resolved.source, resolved.pending) == ("unknown", True)
    (tmp_path / "pointer").mkdir()
    (tmp_path / "pointer" / ".git").write_text("nonsense\n")
    assert release.git_head(str(tmp_path / "pointer")) is None
    assert release.git_head(str(tmp_path / "nowhere")) is None


def test_git_head_comes_after_the_revision_file_and_before_ecs(tmp_path):
    git_checkout(tmp_path, refs={"refs/heads/main": SHA})
    ecs = {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4"}
    image = lambda uri: '{"Image": "shop:abcdef1"}'  # noqa: E731
    assert release.resolve(Configuration(env={}), env=ecs, root=str(tmp_path), http=image).source == "git_head"
    (tmp_path / "REVISION").write_text("abcdef2\n")
    assert release.resolve(Configuration(env={}), env=ecs, root=str(tmp_path), http=image).source == "revision_file"


def test_code_fingerprint_is_the_file_manifests_hash():
    manifest = {"hash": "ab" * 32, "count": 3, "truncated": False, "files": {}}
    assert release.code_fingerprint(manifest).to_protocol() == \
        {"version": "code:abababababab", "commit": None, "source": "code_fingerprint"}


def test_code_fingerprint_needs_a_complete_manifest():
    for manifest in (None, {"hash": None, "count": 0, "truncated": False, "files": {}},
                     {"hash": "ab" * 32, "count": 20_000, "truncated": True, "files": {}},
                     {"hash": "ab" * 32, "count": 0, "truncated": False, "files": {}}):
        fingerprinted = release.code_fingerprint(manifest)
        assert (fingerprinted.source, fingerprinted.unknown, fingerprinted.pending) == ("unknown", True, False)


# Configuration


def test_configuration_reports_only_with_a_token_and_in_listed_environments():
    assert not Configuration(env={}).is_active("production")
    assert Configuration(env={"DEPLOYANGEL_TOKEN": "t"}).is_active("production")
    assert not Configuration(env={"DEPLOYANGEL_TOKEN": "t"}).is_active("development")
    assert Configuration(env={"DEPLOYANGEL_TOKEN": "t", "DEPLOYANGEL_ENABLED": "true"}).is_active("development")
    assert not Configuration(env={"DEPLOYANGEL_TOKEN": "t", "DEPLOYANGEL_ENABLED": "false"}).is_active("production")


def test_configuration_ignores_head_with_the_get_route():
    config = _config(ignored_routes=["GET /healthz/"])
    assert config.is_ignored_route("GET /healthz/")
    assert config.is_ignored_route("HEAD /healthz/")
    assert not config.is_ignored_route("POST /healthz/")
