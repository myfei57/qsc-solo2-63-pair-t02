"""Minimal pytest-compatible runner for environments without pytest installed.

It implements just the surface the shipped tests use: ``pytest.fixture`` and
``pytest.raises`` as context managers, function-based fixtures with fixture
injection by name (including the built-in ``tmp_path``), and plain ``test_*``
discovery. It is a test-time tool only; the production package keeps zero
third-party dependencies.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import sys
import tempfile
import traceback
import types
from pathlib import Path


class _Capture:
    def __init__(self) -> None:
        self._out = io.StringIO()
        self._err = io.StringIO()

    @contextlib.contextmanager
    def active(self):
        with contextlib.redirect_stdout(self._out), contextlib.redirect_stderr(self._err):
            yield

    def readouterr(self):
        out = self._out.getvalue()
        err = self._err.getvalue()
        self._out.seek(0)
        self._out.truncate(0)
        self._err.seek(0)
        self._err.truncate(0)
        return types.SimpleNamespace(out=out, err=err)


class _Raises:
    def __init__(self, expected: type[BaseException]) -> None:
        self.expected = expected
        self.value: BaseException | None = None

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            raise AssertionError(f"DID NOT RAISE {self.expected.__name__}")
        if not issubclass(exc_type, self.expected):
            return False
        self.value = exc
        return True


class _Fixture:
    def __init__(self, func):
        self.func = func

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


def fixture(*args, **kwargs):
    if args and callable(args[0]):
        return _Fixture(args[0])

    def wrap(func):
        return _Fixture(func)

    return wrap


def _install_fake_pytest() -> types.ModuleType:
    module = types.ModuleType("pytest")
    module.fixture = fixture
    module.raises = _Raises
    sys.modules["pytest"] = module
    return module


def _load_module(package_root: Path, path: Path):
    rel = path.relative_to(package_root).with_suffix("")
    module_name = ".".join(rel.parts)
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _collect_fixtures(module) -> dict:
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, _Fixture)
    }


def _run_module(path: Path, package_root: Path) -> tuple[int, int, list]:
    module = _load_module(package_root, path)
    fixtures = _collect_fixtures(module)
    conftest_path = package_root / "tests" / "conftest.py"
    if conftest_path.exists():
        fixtures.update(_collect_fixtures(_load_module(package_root, conftest_path)))

    passed = 0
    failed = 0
    failures = []

    for name, func in vars(module).items():
        if not (name.startswith("test_") and callable(func)):
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                capture = _Capture()
                values: dict[str, object] = {}

                def resolve(fixture_name: str, chain: tuple[str, ...] = ()) -> object:
                    if fixture_name == "tmp_path":
                        return Path(tmp)
                    if fixture_name == "capsys":
                        return capture
                    if fixture_name in values:
                        return values[fixture_name]
                    if fixture_name not in fixtures:
                        raise AssertionError(f"unknown fixture {fixture_name!r}")
                    if fixture_name in chain:
                        raise AssertionError(f"fixture cycle {chain + (fixture_name,)}")
                    fx = fixtures[fixture_name]
                    kwargs = {
                        key: resolve(key, chain + (fixture_name,))
                        for key in inspect.signature(fx.func).parameters
                    }
                    values[fixture_name] = fx.func(**kwargs)
                    return values[fixture_name]

                with capture.active():
                    kwargs = {
                        key: resolve(key) for key in inspect.signature(func).parameters
                    }
                    func(**kwargs)
            passed += 1
        except BaseException:  # noqa: BLE001 - report every test failure
            failed += 1
            failures.append((f"{path.name}::{name}", traceback.format_exc()))
    return passed, failed, failures


def main(argv: list[str]) -> int:
    _install_fake_pytest()
    package_root = Path(__file__).resolve().parents[1]
    roots = argv or [package_root / "tests"]
    paths: list[Path] = []
    for root in roots:
        root_path = Path(root).resolve()
        if root_path.is_file():
            paths.append(root_path)
        else:
            paths.extend(sorted(root_path.glob("test_*.py")))

    total_passed = 0
    total_failed = 0
    all_failures: list = []
    for path in paths:
        passed, failed, failures = _run_module(path, package_root)
        total_passed += passed
        total_failed += failed
        all_failures.extend(failures)
        print(f"{'ok' if not failed else 'FAIL'}: {path.name} ({passed} passed, {failed} failed)")
    for name, tb in all_failures:
        print("\n" + "=" * 70)
        print("FAIL", name)
        print(tb)
    print(f"\n{total_passed} passed, {total_failed} failed across {len(paths)} modules")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
