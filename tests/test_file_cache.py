from gepa_researcher.context.file_cache import FileCache


def test_file_cache_distinguishes_same_path_different_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "model.py"
    cache = FileCache(tmp_path / "run")

    source.write_text("def score():\n    return 1\n", encoding="utf-8")
    first = cache.put_file("main", "commit-a", repo, "model.py")

    source.write_text("def score():\n    return 2\n", encoding="utf-8")
    second = cache.put_file("main", "commit-b", repo, "model.py")

    assert first.key.path == second.key.path
    assert first.key.content_hash != second.key.content_hash
    assert cache.get(first.key).content_ref != cache.get(second.key).content_ref
    assert [item.key.commit_sha for item in cache.find_by_path("main", "commit-b", "model.py")] == ["commit-b"]


def test_file_cache_rejects_paths_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    cache = FileCache(tmp_path / "run")

    for path in ("../outside.py", str(outside)):
        try:
            cache.put_file("main", "commit-a", repo, path)
        except ValueError as exc:
            assert "repository" in str(exc) or "relative" in str(exc)
        else:
            raise AssertionError(f"expected path to be rejected: {path}")


def test_file_cache_rejects_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    (repo / "link.py").symlink_to(outside)
    cache = FileCache(tmp_path / "run")

    try:
        cache.put_file("main", "commit-a", repo, "link.py")
    except ValueError as exc:
        assert "escapes repository" in str(exc)
    else:
        raise AssertionError("expected symlink escape to be rejected")
