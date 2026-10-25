import pytest

from gitfixture import GitFixture


@pytest.fixture
def make_repo(tmp_path):
    """Returns a factory for fixture repositories under the test's temporary directory."""
    counter = iter(range(1000))

    def factory(name: str = "repo", **kwargs) -> GitFixture:
        return GitFixture(tmp_path / f"{name}-{next(counter)}", **kwargs)

    return factory
